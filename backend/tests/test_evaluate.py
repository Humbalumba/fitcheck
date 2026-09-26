"""End-to-end tests of the evaluate flow against the seeded demo closet (no Gemini needed)."""
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
    assert res["verdict"]["decision"] in ("BUY", "SKIP", "UNSUPPORTED")
    assert 1 <= len(res["verdict"]["reasons"]) <= 4
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
    assert res["verdict"]["decision"] == "BUY"
    assert res["value"]["cost_per_outfit"] == pytest.approx(45 / res["total_new_outfits"], abs=0.01)
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
    res = ev(client, candidates["bottom_womens"], price=900)
    assert res["verdict"]["decision"] == "SKIP"
    assert res["value"]["cost_per_outfit"] > 10


def test_outerwear_templates(client, candidates):
    res = ev(client, candidates["outerwear_womens"])
    check_shape(res)
    assert res["template_names"] == ["top+bottom+outerwear+shoes", "dress+outerwear+shoes"]
    assert res["outfit_count_by_template"]["top+bottom+outerwear+shoes"] > 0


def test_dress_templates(client, candidates):
    res = ev(client, candidates["dress_womens"])
    check_shape(res)
    assert res["template_names"] == ["dress+shoes", "dress+outerwear+shoes"]


def test_shoes_supported(client, candidates):
    res = ev(client, candidates["shoes_womens"])
    check_shape(res)
    assert res["supported"] is True
    assert res["template_names"] == ["top+bottom+shoes", "dress+shoes"]


def test_accessory_unsupported(client, candidates):
    res = ev(client, candidates["bag_womens"])
    assert res["supported"] is False and res["verdict"]["decision"] == "UNSUPPORTED"
    assert res["total_new_outfits"] == 0


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
    s = client.put("/api/settings", json={"style_goal": "minimal capsule", "occasions": ["work", "date"]}).json()
    assert s["style_goal"] == "minimal capsule" and s["occasions"] == ["work", "date"]
    assert client.get("/api/settings").json()["min_new_outfits"] == 3


def test_verdict_formula():
    from app.config import DEFAULT_SETTINGS
    from app.evaluate import compute_value_and_verdict
    kw = dict(counts={"top+bottom": 4}, top_match=None, top_similarity=0.5, settings=DEFAULT_SETTINGS, spent=0)
    v, d = compute_value_and_verdict(n_outfits=4, weighted_outfits=3.0, price=30.0, redundancy_level="none", **kw)
    assert d["decision"] == "BUY" and v["cost_per_outfit"] == 7.5
    assert v["value_score"] == 50  # r = 3*10/30 = 1 -> 100*1/(1+1)
    _, d = compute_value_and_verdict(n_outfits=2, weighted_outfits=2.0, price=10.0, redundancy_level="none", **kw)
    assert d["decision"] == "SKIP"  # too few outfits
    v, d = compute_value_and_verdict(n_outfits=10, weighted_outfits=9.0, price=20.0,
                                     redundancy_level="near_duplicate", **kw)
    assert d["decision"] == "SKIP" and v["redundancy_factor"] == 0.2
    _, d = compute_value_and_verdict(n_outfits=10, weighted_outfits=9.0, price=None, redundancy_level="similar", **kw)
    assert d["decision"] == "BUY" and any("Add a price" in r for r in d["reasons"])


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
