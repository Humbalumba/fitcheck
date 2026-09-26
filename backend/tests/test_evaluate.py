"""End-to-end tests of the evaluate flow against the seeded demo closet (no Gemini needed)."""
from pathlib import Path

import pytest

TEST_IMAGES = Path(__file__).resolve().parent.parent / "data" / "test_images"
TEMPLATES = {"top+bottom", "top+bottom+outerwear", "dress", "dress+outerwear"}


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


def test_near_duplicate_navy_tshirt(client, candidates):
    res = ev(client, candidates["navy_tshirt_mens"], price=18)
    check_shape(res)
    assert res["redundancy"]["level"] == "near_duplicate"
    assert res["redundancy"]["matches"][0]["item"]["attributes"]["primary_color"] == "navy"
    assert res["verdict"]["decision"] == "SKIP"
    assert any("Very similar" in r for r in res["verdict"]["reasons"])


def test_distinct_top_is_buy(client, candidates):
    res = ev(client, candidates["olive_shirt_mens"], price=35)
    check_shape(res)
    assert res["redundancy"]["level"] == "none"
    assert res["template_names"] == ["top+bottom", "top+bottom+outerwear"]
    assert res["total_new_outfits"] >= 3
    assert res["verdict"]["decision"] == "BUY"
    assert res["value"]["cost_per_outfit"] == pytest.approx(35 / res["total_new_outfits"], abs=0.01)
    # men's shirt should only be paired with mens/unisex items (match_gender_presentation default)
    for o in res["outfits"]:
        assert all(i["attributes"]["gender_presentation"] in ("mens", "unisex") for i in o["items"])


def test_expensive_item_skips(client, candidates):
    res = ev(client, candidates["olive_shirt_mens"], price=500)
    assert res["verdict"]["decision"] == "SKIP"
    assert res["value"]["cost_per_outfit"] > 10


def test_outerwear_templates(client, candidates):
    res = ev(client, candidates["denim_jacket_womens"])
    check_shape(res)
    assert res["template_names"] == ["top+bottom+outerwear", "dress+outerwear"]
    assert res["outfit_count_by_template"]["top+bottom+outerwear"] > 0


def test_dress_templates(client, candidates):
    res = ev(client, candidates["floral_dress_womens"])
    check_shape(res)
    assert res["template_names"] == ["dress", "dress+outerwear"]
    if res["redundancy"]["level"] != "near_duplicate":
        assert res["outfit_count_by_template"]["dress"] == 1


def test_shoes_unsupported(client, candidates):
    res = ev(client, candidates["white_sneakers_mens"])
    assert res["supported"] is False and res["verdict"]["decision"] == "UNSUPPORTED"
    assert res["total_new_outfits"] == 0


def test_threshold_changes_count(client, candidates):
    base = ev(client, candidates["pink_top_womens"])["total_new_outfits"]
    client.put("/api/settings", json={"compat_threshold": 0.99})
    strict = ev(client, candidates["pink_top_womens"])["total_new_outfits"]
    client.put("/api/settings", json={"compat_threshold": 0.5})
    assert strict <= base


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
    img = TEST_IMAGES / "product_olive_shirt_mens.jpg"
    with open(img, "rb") as f:
        r = client.post("/api/detect", files={"file": ("shirt.jpg", f, "image/jpeg")}, data={"purpose": "closet"})
    assert r.status_code == 200, r.text
    det = r.json()
    assert det["photo_id"] and det["image_url"].startswith("/media/") and det["items"]
    it = det["items"][0]
    assert it["status"] == "detected" and len(it["bbox"]) == 4 and it["cutout_url"].startswith("/media/")
    assert it["attributes"]["category"] in ("top", "outerwear")
    n0 = len(client.get("/api/closet/items").json()["items"])
    added = client.post("/api/closet/items", json={"item_ids": [it["id"]],
                                                   "attributes_overrides": {it["id"]: {"primary_color": "olive"}}})
    assert added.status_code == 200 and added.json()["items"][0]["status"] == "closet"
    assert added.json()["items"][0]["attributes"]["primary_color"] == "olive"
    assert len(client.get("/api/closet/items").json()["items"]) == n0 + 1
    p = client.patch(f"/api/closet/items/{it['id']}", json={"attributes": {"formality": 4}}).json()
    assert p["attributes"]["formality"] == 4 and p["attributes"]["formality_label"] == "business"
    # the held-out olive shirt candidate is now a near duplicate of what we just added
    from app import db
    cand = next(c for c in db.list_items(status="candidate") if c["attributes"].get("test_key") == "olive_shirt_mens")
    res = ev(client, cand["id"])
    assert res["redundancy"]["level"] in ("similar", "near_duplicate")
    assert client.delete(f"/api/closet/items/{it['id']}").json() == {"ok": True}
    assert len(client.get("/api/closet/items").json()["items"]) == n0


def test_candidate_add_to_closet(client, candidates):
    iid = candidates["pink_top_womens"]
    ev(client, iid, price=22)
    r = client.post(f"/api/candidate/{iid}/add-to-closet")
    assert r.status_code == 200 and r.json()["status"] == "closet"
    assert r.json()["attributes"]["purchase_price"] == 22
    assert client.get("/api/settings").status_code == 200
