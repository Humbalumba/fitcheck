"""Shopping suggestions: parsing, store-search selection, ranking and the endpoint -- all offline.

Fixtures (tests/fixtures/suggest): REAL recorded Gemini plan responses (<mode>.plan.json, gemini-3-flash-preview),
REAL recorded store-search hits (<mode>.store_hits.json) and the photos of the products that came back
(<mode>.products.json + images/). No network, no Gemini calls.
"""
import json
import sqlite3
from pathlib import Path

import pytest

FX = Path(__file__).resolve().parent / "fixtures" / "suggest"


def _plan(mode):
    return json.loads((FX / f"{mode}.plan.json").read_text())


# ------------------------------------------------------------------ parsing (grounded-response text)
GROUNDED_TEXT = """Sure! Here are some options I found — café picks included:
```json
[
  {"name": "Sage Rib Tank [1]", "brand": "Everlane", "retailer": "Everlane", "price": "$34.00",
   "product_url": "https://www.everlane.com/products/womens-rib-tank-sage", "image_url": null,
   "category": "Tops", "subcategory": "tank top", "color": "Sage", "material": "cotton", "pattern": "solid",
   "formality": 2, "style_tags": ["minimal"], "gender_presentation": "womens",},
  {"name": "Linen Tank", "brand": "Oak + Fort", "retailer": "Oak + Fort", "price": 19.99,
   "product_url": "<https://oakandfort.com/products/linen-tank>", "category": "top", "color": "beige"},
  {"brand": "no name -> dropped"},
]
```
Let me know if you want more."""


def test_parse_products_robust():
    from app.suggest import normalize_category, parse_products
    ps = parse_products(GROUNDED_TEXT)
    assert [p["name"] for p in ps] == ["Sage Rib Tank", "Linen Tank"]  # citation marker stripped, nameless dropped
    assert ps[0]["price"] == 34.0 and ps[1]["price"] == 19.99
    assert ps[0]["color"] == "sage" and ps[0]["image_url"] is None
    assert ps[1]["product_url"] == "https://oakandfort.com/products/linen-tank"
    assert normalize_category(ps[0]) == "top"
    wrapped = parse_products('{"products": [{"name": "X", "price": "12", "product_url": "https://a.com/p"}]}')
    assert wrapped[0]["name"] == "X" and wrapped[0]["price"] == 12.0
    assert parse_products("no json here") == [] and parse_products("") == []


def test_grounding_urls_follow_utf8_byte_offsets():
    from app.suggest import grounding_urls_for, parse_products
    text = GROUNDED_TEXT
    ps = parse_products(text)
    s, e = ps[1]["_span"]
    bs, be = len(text[:s].encode()), len(text[:e].encode())  # 'é' / '—' make char != byte offsets
    grounded = {"text": text, "chunks": [{"uri": "https://redirect/0", "title": "everlane.com"},
                                         {"uri": "https://redirect/1", "title": "oakandfort.com"}],
                "supports": [{"start": bs + 2, "end": be - 2, "chunks": [1]}]}
    assert grounding_urls_for(ps[1], grounded) == ["https://redirect/1"]
    # retailer-domain fallback when no support overlaps
    assert grounding_urls_for(ps[0], {**grounded, "supports": []}) == ["https://redirect/0"]


# ------------------------------------------------------------------ Gemini plan + store search (recorded)
def test_real_plan_fixture_matches_schema():
    from app.suggest import _plan_schema
    for mode in ("alternatives", "pairings"):
        plan = _plan_schema().model_validate({"queries": _plan(mode)["queries"]})
        assert len(plan.queries) >= 6
        cats = {q.category.value for q in plan.queries}
        assert cats == {"top"} if mode == "alternatives" else cats <= {"top", "outerwear", "shoes"}


@pytest.fixture
def recorded_store_search(monkeypatch):
    from app import suggest
    hits = {m: json.loads((FX / f"{m}.store_hits.json").read_text()) for m in ("alternatives", "pairings")}
    allhits = {q: v for m in hits.values() for q, v in m.items()}
    monkeypatch.setattr(suggest, "search_store", lambda domain, query, limit=8: allhits.get(query, {}).get(domain, []))


def _cand(color="black", cat="top", sub="tank top", price=28):
    return {"id": "c1", "category": cat, "attributes": {"primary_color": color, "subcategory": sub,
                                                        "gender_presentation": "womens", "price": price}}


def test_shop_search_alternatives(recorded_store_search):
    from app.suggest import shop_search
    prods, rejected = shop_search(_plan("alternatives"), _cand(), "alternatives", {"value": {"price": 28}})
    assert 4 <= len(prods) <= 8
    assert len({p["product_url"] for p in prods}) == len(prods)  # distinct products
    for p in prods:
        assert p["category"] == "top" and p["product_url"].startswith("https://") and "/products/" in p["product_url"]
        assert p["color"] != "black"  # never the skipped item's color
        assert 8.4 <= p["price"] <= 56  # 0.3x .. 2x the $28 candidate
        assert p["gender_presentation"] != "mens"
        assert p["image_url"].startswith("https://cdn.shopify.com/")


def test_shop_search_pairings(recorded_store_search):
    from app.suggest import pairing_slots, shop_search
    cand = _cand("white", "bottom", "trousers", 45)
    prods, _ = shop_search(_plan("pairings"), cand, "pairings", {"value": {"price": 45}})
    assert prods and all(p["category"] in pairing_slots(cand) for p in prods)
    assert all(p["price"] <= 120 for p in prods)


def test_score_hit_rules():
    from app.suggest import _score_hit
    q = {"query": "sage rib tank", "category": "top", "subcategory": "tank top", "color": "green", "material": "rib"}
    good = {"title": "Rib Tank - Sage", "price": "30.00", "available": True, "tags": ["female"]}
    assert _score_hit(good, q, "womens", (5, 60))[0] >= 5
    assert _score_hit({**good, "available": False}, q, "womens", (5, 60))[1] == "sold out"
    assert _score_hit({**good, "title": "Men's Rib Tank - Sage", "tags": []}, q, "womens", (5, 60))[1] == "mens item"
    assert "dress" in _score_hit({**good, "title": "Rib Tank Dress - Sage"}, q, "womens", (5, 60))[1]
    assert _score_hit({**good, "title": "Rib Tank - Black"}, q, "womens", (5, 60), avoid_color="black")[0] == -1
    assert _score_hit({**good, "price": "99"}, q, "womens", (5, 60))[0] == -1


# ------------------------------------------------------------------ full pipeline + ranking (offline products)
def _offline_fetch(products, grounded):
    from app.pipeline import load_image
    out = []
    for p in products:
        p = dict(p)
        p["image"] = load_image((FX / "images" / p["fixture_image"]).read_bytes())
        p["image_source_url"], p["link_ok"] = "fixture", True
        p["link_status"] = p.get("link_status") or 200
        out.append(p)
    return out


@pytest.fixture
def offline_suggest(monkeypatch, tmp_path):
    """Replay recorded products (+ an injected near-duplicate black cami) through the real pipeline."""
    from app import suggest
    for mode in ("alternatives", "pairings"):
        d = json.loads((FX / f"{mode}.products.json").read_text())
        if mode == "alternatives":
            d["products"].append({"name": "Black Cami (near-duplicate)", "brand": "Test", "retailer": "Test",
                                  "price": 25.0, "product_url": "https://example.com/p", "category": "top",
                                  "subcategory": "tank top", "color": "black", "material": "jersey",
                                  "pattern": "solid", "formality": 1, "gender_presentation": "womens",
                                  "_span": [0, 0], "fixture_image": "near_dup_black_cami.jpg"})
        (tmp_path / f"{mode}.products.json").write_text(json.dumps(d))
    monkeypatch.setenv("FITCHECK_SUGGEST_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setattr(suggest, "fetch_products", _offline_fetch)


def _evaluate(client, key, price, candidates):
    r = client.post("/api/evaluate", json={"item_id": candidates[key], "price": price})
    assert r.status_code == 200, r.text
    return r.json()


def test_skip_alternatives_offline(client, candidates, offline_suggest):
    ev = _evaluate(client, "near_dup_top", 28, candidates)
    assert ev["verdict"]["decision"] == "SKIP"
    n0 = client.get("/api/health").json()["closet_size"]
    r = client.post(f"/api/evaluations/{ev['evaluation_id']}/suggestions")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "alternatives" and d["title"] == "Better picks instead" and d["cached"] is False
    assert d["fixture"] is True and 1 <= len(d["suggestions"]) <= 3
    for s in d["suggestions"]:
        assert s["redundancy"]["level"] != "near_duplicate"
        assert s["category"] == "top" and s["item"]["status"] == "suggestion"
        assert (s["total_new_outfits"] > ev["total_new_outfits"]
                or s["value"]["value_score"] > ev["value"]["value_score"])
        assert "outfit" in s["reason"] and "$" in s["reason"]
        assert s["image_url"].startswith("/media/suggest/") and s["product_url"].startswith("https://")
        assert s["outfits"] and all(any(i["id"] == s["id"] for i in o["items"]) for o in s["outfits"])
        sus = s["sustainability"]
        assert sus["supported"] and 0 <= sus["score"] <= 100 and sus["garment_type"] == "tshirt"
    names = [s["name"] for s in d["suggestions"]]
    assert "Black Cami (near-duplicate)" not in names
    assert any(x["name"] == "Black Cami (near-duplicate)" for x in d["rejected"])
    # ranking: BUY verdicts first, then more outfits
    keys = [(s["verdict"]["decision"] == "BUY", s["total_new_outfits"]) for s in d["suggestions"]]
    assert keys == sorted(keys, reverse=True)
    # suggestions never enter the closet / FAISS
    assert client.get("/api/health").json()["closet_size"] == n0
    ids = {i["id"] for i in client.get("/api/closet/items").json()["items"]}
    assert not ids & {s["id"] for s in d["suggestions"]}
    from app.vectors import closet_index
    assert not set(closet_index.id_map.values()) & {s["id"] for s in d["suggestions"]}


def test_buy_pairings_offline_and_cache(client, candidates, offline_suggest, monkeypatch):
    from app import suggest
    ev = _evaluate(client, "bottom_womens", 45, candidates)
    assert ev["verdict"]["decision"] == "BUY"
    d = client.post(f"/api/evaluations/{ev['evaluation_id']}/suggestions").json()
    assert d["mode"] == "pairings" and d["title"] == "Pairs well with this"
    assert 1 <= len(d["suggestions"]) <= 3
    cid = ev["item"]["id"]
    for s in d["suggestions"]:
        assert s["verdict"]["decision"] == "BUY" and s["category"] in ("top", "outerwear", "shoes")
        assert s["outfits_with_candidate"] >= 1 and "with the white trousers" in s["reason"]
        assert any(cid in [i["id"] for i in o["items"]] for o in s["outfits"])
        assert s["outfits"][0]["items"] and cid in [i["id"] for i in s["outfits"][0]["items"]]  # with-candidate first
    keys = [s["outfits_with_candidate"] for s in d["suggestions"]]
    assert keys == sorted(keys, reverse=True)
    # cached: re-opening must not recompute (i.e. no Gemini call)
    monkeypatch.setattr(suggest, "compute", lambda *a, **k: pytest.fail("recomputed instead of using the cache"))
    again = client.post(f"/api/evaluations/{ev['evaluation_id']}/suggestions").json()
    assert again["cached"] is True and [s["id"] for s in again["suggestions"]] == [s["id"] for s in d["suggestions"]]
    assert client.get(f"/api/evaluations/{ev['evaluation_id']}/suggestions").json()["cached"] is True


def test_quota_exhausted_is_503_and_not_cached(client, candidates, monkeypatch):
    from app import gemini, suggest

    def boom(*a, **k):
        raise gemini.GeminiUnavailable("Gemini is out of free quota or busy right now (quota resets 3 AM ET)")
    monkeypatch.setattr(suggest, "find_products", boom)
    ev = _evaluate(client, "dress_womens", 22, candidates)
    r = client.post(f"/api/evaluations/{ev['evaluation_id']}/suggestions")
    assert r.status_code == 503 and "quota" in r.json()["detail"]
    assert client.get(f"/api/evaluations/{ev['evaluation_id']}/suggestions").status_code == 404
    assert client.post("/api/evaluations/ev_nope/suggestions").status_code == 404


def test_grounding_tier_429_falls_back_without_marking_models_exhausted(monkeypatch):
    from app import gemini

    class FakeErr(Exception):
        code = 429

    class FakeModels:
        def generate_content(self, **kw):
            raise FakeErr("429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing")

    monkeypatch.setattr(gemini, "client", lambda: type("C", (), {"models": FakeModels()})())
    monkeypatch.setattr(gemini, "model_chain", lambda: ["gemini-3-flash-preview", "gemini-3.8-flash"])
    monkeypatch.setattr(gemini, "_grounding_blocked_until", 0.0)
    before = dict(gemini._exhausted)
    with pytest.raises(gemini.GroundingUnavailable):
        gemini.generate_grounded("find products")
    assert gemini._exhausted == before  # normal detection calls are unaffected
    assert not gemini.grounding_available()


# ------------------------------------------------------------------ DB migration
def test_items_status_migration_preserves_rows(tmp_path):
    from app import db
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    old_schema = db.SCHEMA.replace("'detected','suggestion'", "'detected'")
    c.executescript(old_schema)
    c.execute("INSERT INTO items(id,status,category,attributes,created_at) VALUES ('it_a','closet','top','{}','t')")
    c.execute("INSERT INTO item_embeddings VALUES ('it_a','fclip','m',1,x'00000000','t')")
    c.execute("INSERT INTO evaluations VALUES ('ev_a','it_a',1,'BUY','{}','t')")
    c.commit()
    db._migrate_items_status(c)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='items'").fetchone()[0]
    assert "'suggestion'" in sql
    assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM item_embeddings").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    c.execute("INSERT INTO items(id,status,category,attributes,created_at) VALUES ('it_s','suggestion','top','{}','t')")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("DELETE FROM items WHERE id='it_a'")  # FKs still cascade after the table swap
    assert c.execute("SELECT COUNT(*) FROM item_embeddings").fetchone()[0] == 0
    db._migrate_items_status(c)  # idempotent
    c.close()
