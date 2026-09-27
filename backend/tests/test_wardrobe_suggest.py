""""Worth a look" (GET /api/suggestions/wardrobe): offline -- recorded products + photos replayed through the real
pipeline (fashion-clip redundancy, OutfitTransformer outfits, verdict math). No network, no Gemini calls."""
import json
import time
from pathlib import Path

import pytest

FX = Path(__file__).resolve().parent / "fixtures" / "suggest"


def _reset_cache():
    from app import db, wardrobe_suggest as ws
    ws._mem.clear()
    ws._errors.clear()
    with db.get_conn() as c:
        c.execute(ws._DDL)
        c.execute("DELETE FROM wardrobe_suggestions")


def _offline_fetch(products, grounded):
    from app.pipeline import load_image
    out = []
    for p in products:
        p = dict(p)
        p["image"] = load_image((FX / "images" / p["fixture_image"]).read_bytes())
        p["image_source_url"], p["link_ok"], p["link_status"] = "fixture", True, 200
        p["product_page"], p["photo_kind"] = True, "product_only"
        p["page"] = {"name": p["name"], "price": p.get("price"), "types": [p.get("subcategory") or ""]}
        out.append(p)
    return out


@pytest.fixture
def offline_wardrobe(monkeypatch, tmp_path):
    from app import suggest
    prods = []
    for mode in ("pairings", "alternatives"):
        prods += json.loads((FX / f"{mode}.products.json").read_text())["products"]
    for p in prods:
        p["gender_presentation"] = "unisex"
        p["why"] = "Goes with your everyday pieces"
    prods.append({**prods[0], "name": prods[0]["name"].upper()})  # listed twice -> dropped
    prods.append({"name": "Leather Belt", "brand": "T", "retailer": "T", "price": 20, "category": "accessory",
                  "subcategory": "belt", "product_url": "https://example.com/belt", "fixture_image": "pairings_0.jpg"})
    prods.append({**prods[1], "name": "Ludlow Slim-fit Chino", "brand": "J.Crew", "retailer": "J.Crew",
                  "product_url": "https://www.jcrew.com/p/mens/ludlow-chino/BH1"})  # user blocked J.Crew
    prods.append({**prods[2], "name": "Flex Oxford Shirt", "brand": "Other", "retailer": "Other",
                  "product_url": "https://factory.jcrew.com/p/flex-oxford/BH2"})
    (tmp_path / "wardrobe.products.json").write_text(json.dumps({"products": prods}))
    monkeypatch.setenv("FITCHECK_SUGGEST_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setattr(suggest, "fetch_products", _offline_fetch)
    _reset_cache()
    yield prods
    _reset_cache()


def test_wardrobe_endpoint_offline_ranked_and_cached(client, offline_wardrobe):
    r = client.get("/api/suggestions/wardrobe?wait=120")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ready", d
    sugs = d["suggestions"]
    assert 1 <= len(sugs) <= 5
    closet_ids = {i["id"] for i in client.get("/api/closet/items").json()["items"]}
    for s in sugs:
        assert s["id"] not in closet_ids and s["item"]["status"] == "suggestion"
        assert s["reason"] and not any(ch.isdigit() for ch in s["reason"])  # plain words, no figures
        assert s["pairs_line"].startswith("Goes with") and s["details"]
        assert s["total_new_outfits"] > 0 and s["redundancy"]["level"] != "near_duplicate"
        assert s["product_url"].startswith("https://") and s["image_url"]
        assert s["category"] != "accessory"
    ns = [s["total_new_outfits"] for s in sugs]
    reasons = {x["reason"] for x in d["rejected"]}
    assert "listed twice" in reasons and "not clothes or shoes" in reasons
    assert sum(r.startswith("blocked store") for r in reasons) >= 1
    assert not any("jcrew" in (s["product_url"] or "") or "Crew" in (s.get("retailer") or "") for s in sugs)
    # variety: no category repeats while another category is still available among the kept picks
    cats = [s["category"] for s in sugs]
    assert max(cats.count(c) for c in set(cats)) <= 2
    # second visit: instant from the cache, same picks
    t0 = time.time()
    d2 = client.get("/api/suggestions/wardrobe").json()
    assert d2["status"] == "ready" and d2["cached"] is True and time.time() - t0 < 1.0
    assert [s["id"] for s in d2["suggestions"]] == [s["id"] for s in sugs]
    # survives a restart (SQLite), not just memory
    from app import wardrobe_suggest as ws
    ws._mem.clear()
    assert client.get("/api/suggestions/wardrobe").json()["cached"] is True
    # a pick goes through the normal buy check
    ev = client.post("/api/evaluate", json={"item_id": sugs[0]["id"]})
    assert ev.status_code == 200 and ev.json()["verdict"]["decision"] in ("BUY", "CONSIDER", "SKIP")
    assert ns == sorted(ns, reverse=True) or len(set(cats)) > 1


def test_closet_change_invalidates_cache(client, offline_wardrobe):
    from app import db, wardrobe_suggest as ws
    closet = db.list_items(status="closet")
    settings = db.get_settings()
    k1 = ws.closet_key(closet, settings)
    assert k1 == ws.closet_key(list(reversed(closet)), settings)  # order-independent
    assert ws.closet_key(closet[1:], settings) != k1                 # an item removed
    changed = [dict(closet[0], attributes={**closet[0]["attributes"], "primary_color": "magenta"})] + closet[1:]
    assert ws.closet_key(changed, settings) != k1                    # an item edited
    ws.save_cached(k1, {"suggestions": [], "status": "ready"})
    assert ws.load_cached(k1) is not None
    ws.save_cached("other", {"suggestions": [], "status": "ready"})
    ws._mem.clear()
    assert ws.load_cached(k1) is None  # only the current closet's picks are kept


def test_gemini_unavailable_gives_empty_list_and_reason(client, monkeypatch):
    monkeypatch.delenv("FITCHECK_SUGGEST_FIXTURE_DIR", raising=False)
    _reset_cache()
    d = client.get("/api/suggestions/wardrobe?wait=30").json()
    assert d["status"] == "error" and d["suggestions"] == [] and d["reason"]
    d2 = client.get("/api/suggestions/wardrobe").json()  # not retried immediately, not cached as ready
    assert d2["status"] == "error"
    _reset_cache()


def test_rank_variety_and_filters():
    from app.wardrobe_suggest import rank

    def mk(name, cat, n, level="none", vs=50):
        return ({"name": name}, {"id": name, "category": cat},
                {"n": n, "pieces": n, "redundancy": {"level": level, "top_similarity": 0.5},
                 "top_match": {"attributes": {"primary_color": "black", "subcategory": "tee"}},
                 "value": {"value_score": vs}, "verdict": {}, "best_outfit": 0.5})
    scored = [mk("s1", "shoes", 14), mk("s2", "shoes", 14), mk("s3", "shoes", 13), mk("o1", "outerwear", 12),
              mk("b1", "bottom", 10), mk("dup", "top", 20, level="near_duplicate"), mk("none", "top", 0),
              mk("t1", "top", 3)]
    top, rej = rank(scored, {})
    names = [x[0]["name"] for x in top]
    assert len(names) == 5 and "dup" not in names and "none" not in names
    assert {"s1", "o1", "b1", "t1"} <= set(names) and sum(n.startswith("s") for n in names) == 2
    assert names[0] == "s1"  # ordered by new outfits
    assert any("near-duplicate" in r["reason"] for r in rej)


def test_plain_reason_and_landing_pages():
    from app.wardrobe_suggest import not_a_product_page, plain_reason
    summary = {"size": 3, "categories": {"top": {"count": 3, "colors": ["black"], "types": ["t-shirt"]}}}
    assert plain_reason({"why": "whatever"}, "shoes", summary) == "You don't have any shoes in My Closet yet"
    assert plain_reason({"why": "dresses up your tees."}, "top", summary) == "Dresses up your tees"
    assert plain_reason({"why": "goes with 6 outfits", "color": "green"}, "top", summary) == \
        "A new color for your tops"
    assert not_a_product_page("https://oldnavy.gap.com/browse/GeneralNoResults.do")
    assert not_a_product_page("https://shop.com/") and not_a_product_page(None)
    assert not not_a_product_page("https://www.zara.com/us/en/cotton-bomber-jacket-p03286309.html")


def test_shoes_photo_that_is_really_an_outfit_is_rejected(monkeypatch):
    from app import suggest, wardrobe_suggest as ws
    stats = {"shoes": 0.07, "bottom": 0.02, "top": 0.40, "dress": 0.40}  # measured on a boot-under-trousers photo
    monkeypatch.setattr(suggest, "photo_stats", lambda img, c, max_side=320: {"garment": stats[c]})
    assert ws.shoes_photo_shows_outfit(None)
    stats.update(shoes=0.04, bottom=0.01, top=0.098, dress=0.098)  # a clean sneaker product shot
    assert not ws.shoes_photo_shows_outfit(None)


def test_product_link_must_be_on_the_store_site():
    from app.wardrobe_suggest import host_matches_store, not_a_product_page
    assert not host_matches_store("https://www.youtube.com/watch?v=s2gWov0Da-0", {"retailer": "ASOS", "brand": "ASOS"})
    assert host_matches_store("https://www.levi.com/US/en_US/p/723340134", {"retailer": "Levi's", "brand": "Levi's"})
    assert host_matches_store("https://www2.hm.com/en_us/productpage.1.html", {"retailer": "H&M"})
    assert host_matches_store("https://oldnavy.gap.com/browse/product.do?pid=1", {"retailer": "Old Navy"})
    assert host_matches_store("https://us.princesspolly.com/products/x", {"retailer": "Princess Polly"})
    assert host_matches_store("https://www.nike.com/t/air-force-1", {"retailer": "Nike", "brand": "Nike"})
    assert not_a_product_page("https://www.target.com/s/olive+green+pants+men")



def _vp(**kw):
    p = {"name": "Blanket Lined Denim Jacket", "brand": "Wrangler", "retailer": "Wrangler", "_category": "outerwear",
         "product_url": "https://www.wrangler.com/shop/wrangler-blanket-lined-denim-jacket-74265.html",
         "link_ok": True, "product_page": True, "photo_kind": "product_only",
         "image_source_url": "https://www.wrangler.com/img/1.jpg",
         "page": {"name": "Wrangler Blanket Lined Denim Jacket", "price": 64.99, "types": []}}
    p.update(kw)
    return p


def test_vet_strict_entries():
    from app.wardrobe_suggest import vet
    assert vet(_vp()) is None
    assert "on-model" in vet(_vp(photo_kind="on_model"))                       # lifestyle photo of a man
    assert "no price" in vet(_vp(page={"name": "X Jacket", "types": []}))
    assert "product page" in vet(_vp(product_page=False))
    assert "for shoes" in vet(_vp(name="Leather Sneaker", product_url="https://www.wrangler.com/p/1",
                                  page={"name": "Leather Sneaker", "price": 60, "types": []}))
    # a page that never says what the garment is (e.g. just a model number) isn't solid enough
    assert vet(_vp(name="997H", product_url="https://www.newbalance.com/pd/997h/1.html",
                   page={"name": "997H", "price": 90, "types": []}, _category="shoes")) is not None
    assert vet(_vp(name="997H", product_url="https://www.newbalance.com/pd/997h/1.html",
                   page={"name": "997H", "price": 90, "types": ["Shoes"]}, _category="shoes")) is None
    # bot wall: photo must come from the store or a known image CDN
    assert vet(_vp(link_ok=None, page={}, image_source_url="https://random-blog.net/x.jpg")) is not None
    assert vet(_vp(link_ok=None, page={}, image_source_url="https://lsco.scene7.com/is/image/denim-jacket-1.jpg")) is None
    # ...and the photo URL must name the product or carry its product code (a bare CDN id isn't enough)
    assert vet(_vp(link_ok=None, page={}, image_source_url="https://lsco.scene7.com/is/image/723340134_x")) is not None
    ss = dict(name="Stan Smith Shoes", brand="Adidas", retailer="Adidas", _category="shoes", link_ok=None, page={},
              product_url="https://www.adidas.com/us/stan-smith-shoes/M20324.html")
    assert vet(_vp(**ss, image_source_url="https://assets.adidas.com/images/w_500/abc/Stan_Smith_Shoes_White_M20324_01.jpg")) is None
    af = dict(name="Cloud Fleece Jogger", brand="Abercrombie & Fitch", retailer="Abercrombie & Fitch", _category="bottom",
              link_ok=None, page={}, product_url="https://www.abercrombie.com/shop/us/p/cloud-fleece-joggers-53413344")
    assert vet(_vp(**af, image_source_url="https://img.abercrombie.com/is/image/anf/KIC_134-6084-00141-122_prod1")) is not None



def test_reason_makes_no_color_claims():
    from app.wardrobe_suggest import sanitize_reason
    assert sanitize_reason("Olive adds a new color to your outerwear", "outerwear") == \
        "A new layer to mix with your clothes"
    assert sanitize_reason("You don't have any shoes in My Closet yet", "shoes") == \
        "You don't have any shoes in My Closet yet"
    assert sanitize_reason("A non-hooded layer for a cleaner silhouette", "top") == \
        "A non-hooded layer for a cleaner silhouette"


def test_reason_color_claim_kept_only_when_verified():
    from app.wardrobe_suggest import sanitize_reason
    assert sanitize_reason("Olive adds a new color to your outerwear", "outerwear", "olive") == \
        "Olive adds a new color to your outerwear"
    assert sanitize_reason("Olive adds a new color to your outerwear", "outerwear", "black") == \
        "A new layer to mix with your clothes"


def test_rank_relaxes_category_cap_only_to_reach_three():
    from app.wardrobe_suggest import rank

    def mk(name, cat, n):
        return ({"name": name}, {"id": name, "category": cat},
                {"n": n, "pieces": n, "redundancy": {"level": "none", "top_similarity": 0.5},
                 "top_match": {"attributes": {}}, "value": {"value_score": 50}, "verdict": {}, "best_outfit": 0.5})
    top, _ = rank([mk("s1", "shoes", 9), mk("s2", "shoes", 8), mk("s3", "shoes", 7), mk("s4", "shoes", 6)], {})
    assert [x[0]["name"] for x in top] == ["s1", "s2", "s3"]
    top, _ = rank([mk("s1", "shoes", 9), mk("s2", "shoes", 8), mk("s3", "shoes", 7), mk("b1", "bottom", 1)], {})
    assert [x[0]["name"] for x in top] == ["s1", "s2", "b1"]
