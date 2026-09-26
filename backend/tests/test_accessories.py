"""Accessories are removed from the whole app (clothes and shoes only): dropped at detection (Gemini + offline
fallback), refused at save / PATCH, a friendly answer from "Should I buy?" (nothing saved), never suggested.
Offline: Gemini responses are stubbed (FITCHECK_GEMINI_OFF=1), segformer is stubbed where a photo is processed."""
import io
import json

import numpy as np
import pytest
from PIL import Image

from app import config, db, fallback, gemini, pipeline
from app.accessories import NO_CLOTHES_MESSAGE, is_accessory


def _det(**over):
    base = {"category": "top", "subcategory": "t-shirt", "primary_color": "black", "secondary_colors": [],
            "pattern": "solid", "fabric_guess": "cotton", "formality": 1, "seasons": ["summer"],
            "style_tags": ["casual"], "gender_presentation": "unisex", "brand": None, "price": None,
            "currency": None, "description": "black tee", "box_2d": [100, 100, 600, 500], "label": "black tee",
            "estimated_price_usd": 25.0, "price_confidence": "low"}
    return {**base, **over}


BAG = dict(subcategory="tote bag", label="tan tote bag", description="tan canvas tote", box_2d=[500, 500, 900, 900])


def _stub_gemini(monkeypatch, items):
    parsed = gemini.DetectedItems.model_validate_json(json.dumps({"items": items}))
    monkeypatch.setattr(gemini, "_generate", lambda contents, schema, low_thinking=True: parsed)


# ------------------------------------------------------------------ helper
@pytest.mark.parametrize("text,acc", [
    ("tote bag", True), ("baseball cap", True), ("silver bangle", True), ("aviator sunglasses", True),
    ("leather belt", True), ("wool scarf", True), ("crew socks", True), ("bow tie", True), ("watch", True),
    ("cap-sleeve top", False), ("tie-dye t-shirt", False), ("belted dress", False), ("scarf print blouse", False),
    ("hoodie", False), ("sneakers", False), ("jeans", False), ("clothing item", False)])
def test_is_accessory_head_word(text, acc):
    assert is_accessory(None, text) is acc


def test_schema_has_no_accessory_category():
    assert "accessory" not in [c.value for c in gemini.Category]
    assert "accessory" not in config.CATEGORIES
    assert "accessor" in gemini.DETECT_PROMPT.lower() and "never return a box" in gemini.DETECT_PROMPT.lower()
    with pytest.raises(Exception):  # the structured-output schema can't even carry an accessory now
        gemini.DetectedItems.model_validate_json(json.dumps({"items": [_det(category="accessory")]}))


# ------------------------------------------------------------------ detection (Gemini)
def test_gemini_detect_items_drops_accessories(monkeypatch):
    # a bag mislabelled as a 'top' is dropped by its subcategory; the tee stays
    _stub_gemini(monkeypatch, [_det(), _det(**BAG), _det(subcategory="necklace", label="gold necklace")])
    dropped = []
    out = gemini.detect_items(Image.new("RGB", (64, 64), "white"), dropped=dropped)
    assert [o["label"] for o in out] == ["black tee"]
    assert dropped == ["tan tote bag", "gold necklace"]


@pytest.fixture
def media_tmp(monkeypatch, tmp_path):
    """Photo processing writes into a scratch media dir; segformer is stubbed (no model load)."""
    for sub in ("originals", "crops", "cutouts", "white", "context"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "segment_crop",
                        lambda crop, hint, inner=None, exclude=None: (crop.convert("RGBA"), crop.convert("RGB"),
                                                                       {"strategy": "stub"}))
    return tmp_path


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (180, 150, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def test_photo_with_only_a_bag_gives_zero_items_not_an_error(client, monkeypatch, media_tmp):
    _stub_gemini(monkeypatch, [_det(**BAG)])
    monkeypatch.setattr(gemini, "is_configured", lambda: True)
    identify_calls = []
    monkeypatch.setattr(gemini, "identify", lambda *a, **k: identify_calls.append(1))
    n_items = len(db.list_items())
    r = client.post("/api/detect", files={"file": ("bag.jpg", _jpeg(), "image/jpeg")}, data={"purpose": "closet"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["items"] == [] and res["skipped_accessories"] == 1 and res["message"] == NO_CLOTHES_MESSAGE
    assert not identify_calls  # no whole-photo fallback (which would force the bag into a garment category)
    assert len(db.list_items()) == n_items
    assert not list((media_tmp / "white").iterdir())


def test_mixed_photo_keeps_only_clothes(client, monkeypatch, media_tmp):
    _stub_gemini(monkeypatch, [_det(), _det(**BAG)])
    monkeypatch.setattr(gemini, "is_configured", lambda: True)
    res = client.post("/api/detect", files={"file": ("x.jpg", _jpeg(), "image/jpeg")}).json()
    assert [i["category"] for i in res["items"]] == ["top"] and res["skipped_accessories"] == 1
    assert res["message"] is None
    for it in res["items"]:
        db.delete_item(it["id"])


# ------------------------------------------------------------------ detection (offline fallback)
def test_offline_fallback_drops_accessories(client, monkeypatch, media_tmp):
    monkeypatch.setattr(pipeline, "propose_boxes", lambda img: [{"box_2d": [0, 0, 900, 900], "label": "top"}])
    monkeypatch.setattr(pipeline, "zero_shot_attributes",
                        lambda white, label="": {"category": None, "subcategory": "handbag", "accessory": True})
    res = client.post("/api/detect", files={"file": ("x.jpg", _jpeg(), "image/jpeg")}).json()
    assert res["items"] == [] and res["message"] == NO_CLOTHES_MESSAGE and res["detector"] == "segformer"


def test_segformer_never_proposes_accessory_boxes():
    from app.segment import _GROUPS
    assert set(_GROUPS) == {"top", "bottom", "dress", "shoes"}


def test_zero_shot_flags_clear_accessories_only(monkeypatch):
    monkeypatch.setattr(fallback, "_texts", lambda key, prompts: np.eye(2, dtype=np.float32))
    v = np.array([1.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(fallback, "_best", lambda vec, key, labels, t: ("handbag", 0.9) if "handbag" in labels
                        else (labels[0], 0.9))
    assert fallback.zero_shot_attributes(Image.new("RGB", (8, 8)), img_vec=v)["accessory"] is True
    # not confident -> best garment type instead (never drop a real garment lightly)
    monkeypatch.setattr(fallback, "_best", lambda vec, key, labels, t: ("handbag", 0.3) if "handbag" in labels
                        else (labels[0], 0.9))
    a = fallback.zero_shot_attributes(Image.new("RGB", (8, 8)), img_vec=v)
    assert "accessory" not in a and a["category"] in config.CATEGORIES
    assert all(c != "accessory" for c, _, _ in fallback.SUBCATS.values())


# ------------------------------------------------------------------ saving
@pytest.fixture
def legacy_accessory():
    iid = db.insert_item(status="detected", category="accessory", label="tan tote bag", source="test",
                         attributes={"category": "accessory", "subcategory": "tote bag", "primary_color": "tan"})
    yield iid
    db.delete_item(iid)


@pytest.fixture
def detected_tee():
    iid = db.insert_item(status="detected", category="top", label="black tee", source="test",
                         attributes={"category": "top", "subcategory": "t-shirt", "primary_color": "black"})
    yield iid
    db.delete_item(iid)


def _closet_ids(client):
    return {i["id"] for i in client.get("/api/closet/items").json()["items"]}


def test_add_accessory_to_closet_is_refused(client, legacy_accessory, detected_tee):
    before = _closet_ids(client)
    r = client.post("/api/closet/items", json={"item_ids": [detected_tee, legacy_accessory]})
    assert r.status_code == 422 and "clothes and shoes" in r.json()["detail"]
    assert _closet_ids(client) == before  # nothing half-saved (the tee wasn't added either)
    assert db.get_item(detected_tee)["status"] == "detected"
    r = client.post(f"/api/candidate/{legacy_accessory}/add-to-closet")
    assert r.status_code == 422 and db.get_item(legacy_accessory)["status"] == "detected"


def test_override_to_accessory_is_refused(client, detected_tee):
    for ov in ({"category": "accessory"}, {"subcategory": "leather belt"}):
        r = client.post("/api/closet/items", json={"item_ids": [detected_tee],
                                                   "attributes_overrides": {detected_tee: ov}})
        assert r.status_code == 422, ov
    assert db.get_item(detected_tee)["status"] == "detected"


def test_patch_category_to_accessory_is_rejected(client, detected_tee):
    for body in ({"category": "accessory"}, {"subcategory": "baseball cap"}, {"category": "banana"}):
        r = client.patch(f"/api/closet/items/{detected_tee}", json={"attributes": body})
        assert r.status_code == 422, body
    it = db.get_item(detected_tee)
    assert it["category"] == "top" and it["attributes"]["subcategory"] == "t-shirt"
    r = client.patch(f"/api/closet/items/{detected_tee}", json={"attributes": {"category": "outerwear"}})
    assert r.status_code == 200 and r.json()["category"] == "outerwear"  # real categories still editable


def test_closet_listing_has_no_accessories(client):
    assert all(i["category"] in config.CATEGORIES for i in client.get("/api/closet/items").json()["items"])


# ------------------------------------------------------------------ Should I buy?
def test_evaluate_accessory_is_friendly_and_not_saved(client, legacy_accessory):
    with db.get_conn() as c:
        n_ev = c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    r = client.post("/api/evaluate", json={"item_id": legacy_accessory, "price": 40})
    assert r.status_code == 200
    res = r.json()
    assert res["supported"] is False and res["verdict"]["decision"] == "UNSUPPORTED"
    assert res["message"].startswith("FitCheck only checks clothes and shoes right now")
    assert res["verdict"]["reasons"] == [res["message"]] and res["evaluation_id"] is None
    assert res["outfits"] == [] and res["sustainability"] is None
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == n_ev
    assert db.get_item(legacy_accessory)["status"] == "detected"  # not turned into a saved candidate
    assert db.get_embedding(legacy_accessory, "fclip") is None


# ------------------------------------------------------------------ suggestions
def test_accessory_products_are_never_suggested():
    from app.suggest import normalize_category, parse_products
    ps = parse_products(json.dumps([
        {"name": "Canvas Tote", "price": 30, "product_url": "https://a.com/1", "category": "top"},
        {"name": "Gold Hoops", "price": 30, "product_url": "https://a.com/2", "category": "accessory",
         "subcategory": "earrings"},
        {"name": "Wool Beanie", "price": 30, "product_url": "https://a.com/3", "category": "bag"},
        {"name": "Linen Shirt", "price": 30, "product_url": "https://a.com/4", "category": "top",
         "subcategory": "button-down shirt"}]))
    assert [normalize_category(p) for p in ps] == [None, None, None, "top"]


def test_compute_rejects_accessory_products(client, candidates, monkeypatch):
    from app import suggest
    ev = client.post("/api/evaluate", json={"item_id": candidates["top_womens"], "price": 28}).json()
    products = [{"name": "Leather Belt", "retailer": "X", "price": 40.0, "category": "bottom", "subcategory": "belt",
                 "product_url": "https://x.com/belt"},
                {"name": "Straw Hat", "retailer": "X", "price": 30.0, "category": "accessory", "subcategory": "hat",
                 "product_url": "https://x.com/hat"}]
    monkeypatch.setattr(suggest, "find_products", lambda *a, **k: (products, [], {"source": "stub"}))
    seen = []
    res = suggest.compute(ev["evaluation_id"], fetcher=lambda valid, grounded: seen.extend(valid) or [])
    assert seen == [] and res["suggestions"] == []
    assert {r["reason"] for r in res["rejected"]} == {"accessory"}


def test_cached_accessory_suggestions_are_hidden():
    from app.suggest import add_sustainability
    cached = {"suggestions": [{"name": "Tote Bag", "category": "accessory", "sustainability": None},
                              {"name": "Linen Shirt", "category": "top", "subcategory": "shirt",
                               "sustainability": None}]}
    assert [s["name"] for s in add_sustainability(cached)["suggestions"]] == ["Linen Shirt"]


def test_store_hit_accessory_rejected():
    from app.suggest import _score_hit
    q = {"category": "top", "subcategory": "t-shirt", "query": "cotton tee", "color": "white"}
    sc, why = _score_hit({"title": "Organic Cotton Bucket Hat", "price": "30", "available": True}, q, None, (5, 120))
    assert sc < 0
