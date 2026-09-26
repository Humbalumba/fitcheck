"""End-to-end tests of the evaluate flow against the seeded demo closet (no Gemini needed)."""
import json
from pathlib import Path

import pytest

TEST_IMAGES = Path(__file__).resolve().parent.parent / "data" / "test_images"
TEMPLATES = {"top+bottom", "top+bottom+outerwear", "dress", "dress+outerwear", "top+bottom+shoes",
             "top+bottom+outerwear+shoes", "dress+shoes", "dress+outerwear+shoes"}


def ev(client, item_id, price=None):
    body = {"item_id": item_id}
    if price is not None:
        body["price"] = price
    r = client.post("/api/evaluate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def check_shape(res):
    for k in ("item", "template_names", "redundancy", "outfits", "outfit_count_by_template", "total_new_outfits",
              "value", "verdict", "evaluation_id"):
        assert k in res
    assert res["verdict"]["decision"] in ("BUY", "CONSIDER", "SKIP", "UNSUPPORTED")
    assert 1 <= len(res["verdict"]["reasons"]) <= 2
    assert "budget" not in json.dumps(res).lower()
    if res["verdict"]["decision"] != "UNSUPPORTED":
        assert 0 <= res["verdict"]["score"] <= 100 and res["verdict"]["score"] == res["value"]["value_score"]
        assert res["value"]["versatility"]["new_outfits"] == res["total_new_outfits"]
        assert res["value"]["versatility"]["max_possible"] >= res["total_new_outfits"]
    assert res["total_new_outfits"] == sum(res["outfit_count_by_template"].values())
    for o in res["outfits"]:
        assert o["template"] in TEMPLATES and 0 <= o["score"] <= 1
        assert o["template"] in res["template_names"]
        assert o["n_items"] == len(o["items"]) == o["template"].count("+") + 1
        cats = [i["category"] for i in o["items"]]
        assert len(cats) == len(set(cats)), "never two items of the same slot"
        if o["raw_score"] is not None:
            assert 0 <= o["raw_score"] <= 1
        assert any(i["id"] == res["item"]["id"] for i in o["items"])
        for i in o["items"]:
            assert i["image_url"].startswith("/media/")


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["scorer"] in ("outfit_transformer", "stub") and isinstance(h["gemini"], bool)


def test_closet_listing(client):
    items = client.get("/api/closet/items").json()["items"]
    assert len(items) >= 35
    cats = {i["category"] for i in items}
    assert {"top", "bottom", "outerwear", "dress", "shoes"} <= cats
    genders = {i["attributes"]["gender_presentation"] for i in items}
    assert {"mens", "womens"} <= genders
    tops = client.get("/api/closet/items", params={"category": "top"}).json()["items"]
    assert tops and all(i["category"] == "top" for i in tops)
    r = client.get(tops[0]["image_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")


def test_near_duplicate_top(client, candidates):
    res = ev(client, candidates["near_dup_top"], price=28)
    check_shape(res)
    assert res["redundancy"]["level"] == "near_duplicate"
    assert res["redundancy"]["matches"][0]["item"]["attributes"]["subcategory"] == "tank top"
    assert res["verdict"]["decision"] == "SKIP"
    assert any("Very similar" in r for r in res["verdict"]["reasons"])


def test_bottom_is_buy_with_shoes_layer(client, candidates):
    res = ev(client, candidates["bottom_womens"], price=45)
    check_shape(res)
    assert res["redundancy"]["level"] in ("none", "similar")
    assert res["template_names"] == ["top+bottom+shoes", "top+bottom+outerwear+shoes"]
    assert res["total_new_outfits"] >= 3
    assert res["verdict"]["decision"] == "BUY" and res["verdict"]["score"] >= 65
    # cost per wear uses the SAME wears model as the sustainability card
    assert res["value"]["expected_wears"] == pytest.approx(res["sustainability"]["expected_wears"], abs=0.1)
    assert res["value"]["cost_per_wear"] == pytest.approx(45 / res["value"]["expected_wears"], abs=0.01)
    assert res["value"]["cost_per_wear"] == pytest.approx(res["sustainability"]["cost_per_wear"], abs=0.01)
    # a bottom can make (#tops) x (1 + #outerwear) looks with this closet (gender-filtered)
    assert res["value"]["versatility"]["max_possible"] >= res["total_new_outfits"] >= 30
    # shoes are a completing layer: at most one outfit per (top, bottom[, outerwear]) base look
    bases = [tuple(sorted(i["id"] for i in o["items"] if i["category"] != "shoes")) for o in res["outfits"]]
    assert len(bases) == len(set(bases))
    assert res["calibration"]["raw_cutoffs_by_size"] == {"2": 0.15, "3": 0.35, "4+": 0.6}


def test_mens_top_pairs_with_mens_only(client, candidates):
    res = ev(client, candidates["top_mens"], price=28)
    check_shape(res)
    for o in res["outfits"]:
        assert all(i["attributes"]["gender_presentation"] in ("mens", "unisex") for i in o["items"])


def test_expensive_item_skips(client, candidates):
    # the test closet has no prices, so the bar comes from the closet's price estimates (~$1.00/wear, not the
    # $0.75 default): $1500 trousers are ~11x that -> cost floor -> SKIP despite being very versatile
    res = ev(client, candidates["bottom_womens"], price=1500)
    assert res["verdict"]["decision"] == "SKIP"
    assert res["value"]["price_source"] == "user"
    assert res["value"]["price_bar_source"] == "closet_estimates"
    assert res["value"]["cost_per_wear"] > 4 * res["value"]["price_bar"]
    mid = ev(client, candidates["bottom_womens"], price=150)  # pricey but very versatile: not an automatic SKIP
    assert mid["verdict"]["decision"] in ("BUY", "CONSIDER")
    ev(client, candidates["bottom_womens"], price=45)


def test_outerwear_templates(client, candidates):
    res = ev(client, candidates["outerwear_womens"])
    check_shape(res)
    assert res["template_names"] == ["top+bottom+outerwear+shoes", "dress+outerwear+shoes"]
    assert res["outfit_count_by_template"]["top+bottom+outerwear+shoes"] > 0


def test_dress_templates(client, candidates):
    res = ev(client, candidates["dress_womens"])
    check_shape(res)
    assert res["template_names"] == ["dress+shoes", "dress+outerwear+shoes"]
    n_outer = len(client.get("/api/closet/items", params={"category": "outerwear"}).json()["items"])
    # a dress can only ever make 1 look + 1 per outerwear piece; judged against that, not a fixed outfit floor
    assert res["value"]["versatility"]["max_possible"] <= 1 + n_outer
    assert res["verdict"]["decision"] == "BUY"


def test_shoes_supported(client, candidates):
    res = ev(client, candidates["shoes_womens"])
    check_shape(res)
    assert res["supported"] is True
    assert res["template_names"] == ["top+bottom+shoes", "dress+shoes"]


def test_accessory_unsupported(client):
    # accessories are never detected any more; a legacy accessory row still gets the friendly answer, not a verdict
    from app import db
    iid = db.insert_item(status="detected", category="accessory", source="test",
                         attributes={"category": "accessory", "subcategory": "handbag", "primary_color": "tan"})
    res = ev(client, iid)
    assert res["supported"] is False and res["verdict"]["decision"] == "UNSUPPORTED"
    assert res["total_new_outfits"] == 0 and res["evaluation_id"] is None
    assert "clothes and shoes" in res["message"]
    db.delete_item(iid)


def test_strictness_changes_count(client, candidates):
    base = ev(client, candidates["bottom_womens"])["total_new_outfits"]
    client.put("/api/settings", json={"compat_threshold": 0.9})
    strict = ev(client, candidates["bottom_womens"])
    client.put("/api/settings", json={"compat_threshold": 0.1})
    lenient = ev(client, candidates["bottom_womens"])["total_new_outfits"]
    client.put("/api/settings", json={"compat_threshold": 0.5})
    assert strict["total_new_outfits"] <= base <= lenient
    assert all(o["score"] >= 0.9 for o in strict["outfits"])


def test_no_shoes_layer_setting(client, candidates):
    client.put("/api/settings", json={"use_shoes_layer": False})
    res = ev(client, candidates["bottom_womens"])
    client.put("/api/settings", json={"use_shoes_layer": True})
    assert res["template_names"] == ["top+bottom", "top+bottom+outerwear"]
    check_shape(res)


def test_calibration_math():
    from app.scoring import calibrate, raw_cutoff
    for n, t in ((2, 0.15), (3, 0.35), (4, 0.6), (6, 0.6)):
        assert calibrate(t, n) == pytest.approx(0.5)
        assert raw_cutoff(0.5, n) == pytest.approx(t)
        assert raw_cutoff(0.0, n) == 0.0 and raw_cutoff(1.0, n) == pytest.approx(1.0)
        for s in (0.1, 0.3, 0.7, 0.9):
            assert calibrate(raw_cutoff(s, n), n) == pytest.approx(s)
    assert calibrate(0.3, 2) > calibrate(0.3, 3) > calibrate(0.3, 4)


def test_item_text():
    from app.scoring import item_text
    t = item_text({"attributes": {"primary_color": "Navy", "pattern": "solid", "fabric_guess": "cotton twill",
                                  "subcategory": "chinos"}})
    assert t == "navy cotton twill chinos"


def test_settings_roundtrip(client):
    s = client.put("/api/settings", json={"style_goal": "minimal capsule", "occasions": ["work", "date"],
                                          "monthly_budget": 150, "min_new_outfits": 5}).json()  # retired: ignored
    assert s["style_goal"] == "minimal capsule" and s["occasions"] == ["work", "date"]
    got = client.get("/api/settings").json()
    for k in ("monthly_budget", "min_new_outfits", "max_cost_per_outfit"):
        assert k not in got and k not in s
    assert got["compat_threshold"] == 0.5


def test_old_saved_settings_are_ignored(client):
    from app import db
    with db.get_conn() as c:  # a settings row written by an older version
        old = json.loads(c.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])
        c.execute("UPDATE settings SET data=? WHERE id=1",
                  (json.dumps({**old, "monthly_budget": 200.0, "min_new_outfits": 3, "max_cost_per_outfit": 10.0}),))
    got = client.get("/api/settings").json()
    assert not {"monthly_budget", "min_new_outfits", "max_cost_per_outfit"} & set(got)
    client.put("/api/settings", json={"style_goal": ""})
    with db.get_conn() as c:
        assert "budget" not in c.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]


def test_old_buy_skip_evaluation_recomputed_on_read(client, candidates):
    """Evaluations stored by formula v1 (BUY/SKIP + budget fields) render with the new verdict."""
    from app import db
    res = ev(client, candidates["dress_womens"], price=55)
    eid = res["evaluation_id"]
    with db.get_conn() as c:
        row = json.loads(c.execute("SELECT results FROM evaluations WHERE id=?", (eid,)).fetchone()[0])
        row["value"] = {"price": 55.0, "cost_per_outfit": 9.17, "weighted_outfits": 5.1, "value_score": 47,
                        "redundancy_factor": 1.0, "budget_remaining": 200.0, "currency": "USD"}
        row["verdict"] = {"decision": "SKIP", "reasons": ["Only creates 2 new outfits — you want at least 3"]}
        row["settings"] = {**row["settings"], "monthly_budget": 200.0, "min_new_outfits": 3}
        c.execute("UPDATE evaluations SET results=? WHERE id=?", (json.dumps(row), eid))
    got = client.get(f"/api/evaluations/{eid}")
    assert got.status_code == 200
    g = got.json()
    assert g["verdict"]["decision"] == res["verdict"]["decision"] and g["verdict"]["score"] == res["verdict"]["score"]
    assert g["verdict"]["recomputed_on_read"] is True and g["verdict"]["original_decision"] == "SKIP"
    assert "budget" not in json.dumps(g).lower() and "settings" not in g


def test_max_possible_outfits():
    from app.config import DEFAULT_SETTINGS
    from app.evaluate import max_possible_outfits

    def it(i, cat, g="womens"):
        return {"id": f"i{i}", "category": cat, "attributes": {"gender_presentation": g}}
    closet = ([it(i, "top") for i in range(4)] + [it(10 + i, "bottom") for i in range(3)]
              + [it(20 + i, "outerwear") for i in range(2)] + [it(30, "dress"), it(31, "shoes"), it(40, "bottom", "mens")])
    S = DEFAULT_SETTINGS
    assert max_possible_outfits(it(99, "dress"), closet, S) == 1 + 2
    assert max_possible_outfits(it(99, "top"), closet, S) == 3 * (1 + 2)       # mens bottom filtered out
    assert max_possible_outfits(it(99, "bottom"), closet, S) == 4 * (1 + 2)
    assert max_possible_outfits(it(99, "outerwear"), closet, S) == 4 * 3 + 1
    assert max_possible_outfits(it(99, "shoes"), closet, S) == 4 * 3 + 1
    assert max_possible_outfits(it(99, "top"), [], S) == 0
    assert max_possible_outfits(it(99, "top"), closet, {**S, "match_gender_presentation": False}) == 4 * 3


def test_detect_add_evaluate_delete(client):
    """Stage 1-2 via API (uses Gemini if GEMINI_API_KEY is set, else whole-image + zero-shot fallback)."""
    img = TEST_IMAGES / "product_top_womens.jpg"
    with open(img, "rb") as f:
        r = client.post("/api/detect", files={"file": ("shirt.jpg", f, "image/jpeg")}, data={"purpose": "closet"})
    assert r.status_code == 200, r.text
    det = r.json()
    assert det["photo_id"] and det["image_url"].startswith("/media/") and det["items"]
    it = det["items"][0]
    assert it["status"] == "detected" and len(it["bbox"]) == 4 and it["cutout_url"].startswith("/media/")
    assert it["attributes"]["category"] in ("top", "outerwear", "dress")
    n0 = len(client.get("/api/closet/items").json()["items"])
    added = client.post("/api/closet/items", json={"item_ids": [it["id"]],
                                                   "attributes_overrides": {it["id"]: {"primary_color": "pink"}}})
    assert added.status_code == 200 and added.json()["items"][0]["status"] == "closet"
    assert added.json()["items"][0]["attributes"]["primary_color"] == "pink"
    assert len(client.get("/api/closet/items").json()["items"]) == n0 + 1
    p = client.patch(f"/api/closet/items/{it['id']}", json={"attributes": {"formality": 4}}).json()
    assert p["attributes"]["formality"] == 4 and p["attributes"]["formality_label"] == "business"
    # the held-out hoodie candidate is now redundant with the copy we just added from its photo
    from app import db
    cand = next(c for c in db.list_items(status="candidate") if c["attributes"].get("test_key") == "top_womens")
    res = ev(client, cand["id"])
    assert res["redundancy"]["level"] in ("similar", "near_duplicate")
    assert client.delete(f"/api/closet/items/{it['id']}").json() == {"ok": True}
    assert len(client.get("/api/closet/items").json()["items"]) == n0


def test_candidate_add_to_closet(client, candidates):
    iid = candidates["dress_womens"]
    ev(client, iid, price=22)
    r = client.post(f"/api/candidate/{iid}/add-to-closet")
    assert r.status_code == 200 and r.json()["status"] == "closet"
    assert r.json()["attributes"]["purchase_price"] == 22
    assert client.get("/api/settings").status_code == 200
