"""Price estimates (app/pricing.py): Gemini schema parsing, the fallback table, the verdict using and labelling an
estimate, user price > estimate, the price bar with estimates, and the backfill. No real Gemini calls."""
import json

import pytest
from PIL import Image

from app import gemini, pricing
from app import verdict as V
from app.fallback import SUBCATS


# ------------------------------------------------------------------ Gemini detection schema
def _detected(**over):
    base = {"category": "top", "subcategory": "polo shirt", "primary_color": "navy", "secondary_colors": [],
            "pattern": "solid", "fabric_guess": "cotton pique", "formality": 2, "seasons": ["summer"],
            "style_tags": ["preppy"], "gender_presentation": "mens", "brand": "Polo Ralph Lauren", "price": None,
            "currency": None, "description": "navy polo", "box_2d": [100, 100, 600, 500], "label": "navy polo",
            "estimated_price_usd": 98.5, "price_confidence": "high"}
    return {**base, **over}


def _fake_detect(monkeypatch, items):
    parsed = gemini.DetectedItems.model_validate_json(json.dumps({"items": items}))
    monkeypatch.setattr(gemini, "_generate", lambda contents, schema, low_thinking=True: parsed)
    return gemini.detect_items(Image.new("RGB", (64, 64), "white"))


def test_schema_has_estimate_fields():
    props = gemini.DetectedItems.model_json_schema()["$defs"]["DetectedItem"]["properties"]
    assert "estimated_price_usd" in props and "price_confidence" in props
    assert "estimated_price_usd" in gemini.Attributes.model_fields


def test_detect_items_parses_estimate(monkeypatch):
    out = _fake_detect(monkeypatch, [
        _detected(),
        _detected(brand=None, estimated_price_usd=41.2, price_confidence="HIGH"),     # high needs a brand
        _detected(brand=None, subcategory="jeans", category="bottom", estimated_price_usd=None,
                  price_confidence=None),                                              # omitted -> table
        _detected(price=25.0, currency="USD"),                                         # tag price
    ])
    a0, a1, a2, a3 = (o["attributes"] for o in out)
    assert (a0["estimated_price_usd"], a0["price_confidence"], a0["price_estimate_source"]) == (98.0, "high", "gemini")
    assert (a1["estimated_price_usd"], a1["price_confidence"]) == (41.0, "medium")
    assert (a2["estimated_price_usd"], a2["price_confidence"], a2["price_estimate_source"]) == (70.0, "low", "table")
    assert a3["price"] == 25.0 and a3["price_source"] == "tag"
    assert "price_source" not in a0


def test_identify_parses_estimate(monkeypatch):
    d = _detected()
    d.pop("box_2d"); d.pop("label")
    parsed = gemini.Attributes.model_validate_json(json.dumps({**d, "price_confidence": "bogus"}))
    monkeypatch.setattr(gemini, "_generate", lambda contents, schema, low_thinking=True: parsed)
    img = Image.new("RGB", (32, 32), "white")
    a = gemini.identify(img, img, "navy polo")
    assert a["estimated_price_usd"] == 98.0 and a["price_confidence"] == "medium"  # invalid -> medium (has brand)


# ------------------------------------------------------------------ fallback table
def test_table_is_documented_and_covers_every_fallback_type():
    raw = json.loads(pricing.TABLE_PATH.read_text())
    assert "ROUGH" in raw["_doc"]
    rules = pricing.load_table()["rules"]
    for sub in SUBCATS:  # every garment type the fallback detector can output hits a specific rule
        assert any(rx.search(sub) for rx, _ in rules), sub


@pytest.mark.parametrize("sub,cat,price", [
    ("t-shirt", "top", 25), ("polo shirt", "top", 45), ("button-down shirt", "top", 55), ("hoodie", "top", 55),
    ("jeans", "bottom", 70), ("dress trousers", "bottom", 60), ("denim shorts", "bottom", 40),
    ("dress", "dress", 75), ("blazer", "outerwear", 130), ("puffer jacket", "outerwear", 160),
    ("sneakers", "shoes", 90), ("handbag", "accessory", 60), ("mystery thing", "shoes", 90),
    ("mystery thing", None, 50)])
def test_table_prices(sub, cat, price):
    assert pricing.table_price({"subcategory": sub}, cat) == price
    e = pricing.table_estimate({"subcategory": sub}, cat)
    assert e == {"estimated_price_usd": float(price), "price_confidence": "low", "price_estimate_source": "table"}


def test_fallback_detector_always_estimates(monkeypatch):
    import numpy as np
    from app import fallback
    monkeypatch.setattr(fallback, "_best", lambda v, key, labels, template: (labels[0], 1.0))
    monkeypatch.setattr(fallback, "_texts", lambda key, prompts: np.eye(2, dtype=np.float32))
    a = fallback.zero_shot_attributes(Image.new("RGB", (8, 8)), img_vec=np.array([1.0, 0.0], dtype=np.float32))
    assert a["estimated_price_usd"] == pricing.table_price(a, a["category"])
    assert a["price_confidence"] == "low" and a["price_estimate_source"] == "table"


# ------------------------------------------------------------------ effective price: user > tag > estimate
def test_user_price_overrides_estimate():
    a = {"subcategory": "jeans", "estimated_price_usd": 80.0, "price_confidence": "medium",
         "price_estimate_source": "gemini"}
    est = pricing.effective_price(a, "bottom")
    assert (est["price"], est["price_source"], est["price_confidence"]) == (80.0, "estimated", "medium")
    assert pricing.effective_price(a, "bottom", user_price=120)["price_source"] == "user"
    assert pricing.effective_price({**a, "price": 120, "price_source": "user"}, "bottom")["price"] == 120
    tag = pricing.effective_price({**a, "price": 59.99}, "bottom")
    assert (tag["price"], tag["price_source"]) == (59.99, "tag")
    assert pricing.effective_price({"subcategory": "jeans"}, "bottom")["price"] == 70.0  # on-the-fly table
    assert pricing.effective_price({**a, "price": 0, "price_source": "user"}, "bottom")["price"] == 0.0  # gift


# ------------------------------------------------------------------ verdict with an estimated price
def _run(price, source=None, conf=None, ctx=None):
    ctx = ctx or {"category_counts": {"top": 5, "bottom": 5}, "type_counts": {"tshirt": 2, "jeans": 2},
                  "price_bar": 1.10, "price_bar_source": "closet_median", "priced_items": 5}
    return V.compute_verdict(category="bottom", attributes={"subcategory": "jeans"}, n_outfits=10, max_possible=40,
                             outfit_scores=[0.85] * 10, price=price, redundancy_level="none", top_similarity=0.5,
                             top_match_name=None, context=ctx, price_source=source, price_confidence=conf)


def test_verdict_uses_estimate_and_labels_it():
    value, d = _run(60.0, "estimated", "low")
    assert value["price_source"] == "estimated" and value["price_confidence"] == "low"
    assert value["cost_per_wear"] is not None
    assert d["components"]["cost"]["weight"] == pytest.approx(0.2)            # low confidence: half weight
    assert d["components"]["cost"]["price_source"] == "estimated"
    cost_reason = next(r for r in d["all_reasons"] if "per wear" in r)
    assert "(est. price ~$60)" in cost_reason and "your usual $1.10" in cost_reason
    assert any("enter the price" in r for r in d["all_reasons"])
    _, dm = _run(60.0, "estimated", "medium")
    _, dh = _run(60.0, "estimated", "high")
    assert dm["components"]["cost"]["weight"] == pytest.approx(0.3)
    assert dh["components"]["cost"]["weight"] == pytest.approx(0.4)


def test_user_price_is_not_labelled_estimate():
    value, d = _run(60.0, "user")
    assert value["price_source"] == "user" and value["price_confidence"] is None
    assert d["components"]["cost"]["weight"] == pytest.approx(0.4)
    assert not any("est." in r for r in d["all_reasons"])
    v2, _ = _run(60.0)                    # legacy callers (no source) = a real price
    assert v2["price_source"] == "user"
    v3, d3 = _run(None)
    assert v3["price_source"] is None and d3["components"]["cost"]["score"] is None


# ------------------------------------------------------------------ price bar with estimates
def _it(sub, cat, price=None, est=None, **extra):
    a = {"subcategory": sub, **extra}
    if price is not None:
        a["price"], a["price_source"] = price, "user"
    if est is not None:
        a.update(estimated_price_usd=est, price_confidence="medium", price_estimate_source="gemini_text")
    return {"category": cat, "attributes": a}


def test_price_bar_with_estimates():
    # 1 real price + 2 estimates: real counts twice. per wear: 45/45=1.0 (x2), jeans est 140/70=2.0, t-shirt table 25/45
    ctx = V.closet_context([_it("t-shirt", "top", price=45), _it("jeans", "bottom", est=140), _it("t-shirt", "top")])
    assert ctx["price_bar_source"] == "closet_estimates"
    assert ctx["priced_items"] == 1 and ctx["estimated_items"] == 2
    assert ctx["price_bar"] == pytest.approx(1.0)
    # stored estimates are used (not the table): only estimates -> median of them
    only_est = V.closet_context([_it("jeans", "bottom", est=140), _it("jeans", "bottom", est=70)])
    assert only_est["price_bar"] == pytest.approx(1.5) and only_est["priced_items"] == 0
    # >= 3 real prices: estimates ignored
    ctx3 = V.closet_context([_it("t-shirt", "top", price=45), _it("t-shirt", "top", price=90),
                             _it("t-shirt", "top", price=45), _it("jeans", "bottom", est=700)])
    assert ctx3["price_bar_source"] == "closet_median" and ctx3["price_bar"] == pytest.approx(1.0)
    # the $0.75 default only with no prices or estimates at all
    assert V.closet_context([])["price_bar_source"] == "default"
    _, d = _run(60.0, "estimated", "high", ctx={**ctx, "category_counts": {}, "type_counts": {}})
    assert any("your usual ~$1.00" in r for r in d["all_reasons"])


# ------------------------------------------------------------------ API + backfill (temp test DB, Gemini off)
def test_api_estimate_then_user_override(client, candidates):
    from app import db
    iid = candidates["bottom_womens"]
    orig = db.get_item(iid)["attributes"]
    try:
        db.update_item(iid, attributes={k: v for k, v in orig.items() if k not in ("price", "price_source")})
        r = client.post("/api/evaluate", json={"item_id": iid}).json()
        v = r["value"]
        assert v["price_source"] == "estimated" and v["price"] == 60.0 and v["price_confidence"] == "low"
        assert v["estimated_price"] == 60.0 and v["cost_per_wear"] is not None
        assert any("est. price ~$60" in x for x in r["verdict"]["all_reasons"])
        r2 = client.post("/api/evaluate", json={"item_id": iid, "price": 45}).json()
        assert r2["value"]["price_source"] == "user" and r2["value"]["price"] == 45
        assert r2["value"]["estimated_price"] == 60.0
        assert not any("est." in x for x in r2["verdict"]["all_reasons"])
    finally:
        db.update_item(iid, attributes=orig)


def test_patch_price_marks_user(client):
    from app import db
    it = next(i for i in db.list_items(status="closet") if not (i["attributes"] or {}).get("price"))
    orig = it["attributes"]
    try:
        r = client.patch(f"/api/closet/items/{it['id']}", json={"attributes": {"price": 33}})
        assert r.status_code == 200 and r.json()["attributes"]["price_source"] == "user"
        r = client.patch(f"/api/closet/items/{it['id']}", json={"attributes": {"price": None}})
        assert "price_source" not in r.json()["attributes"]
    finally:
        db.update_item(it["id"], attributes=orig)


def test_backfill_gemini_batch_and_quota_fallback(client, monkeypatch):
    from app import db
    need = [i for i in db.list_items(status="closet") if pricing.needs_estimate(i)]
    assert need  # the seeded test closet has no prices
    calls = []

    def fake_batch(items):
        calls.append(len(items))
        return {it["id"]: {"estimated_price_usd": 42.0, "price_confidence": "high"} for it in items}

    monkeypatch.setattr(gemini, "is_configured", lambda: True)
    monkeypatch.setattr(pricing, "gemini_batch_estimates", fake_batch)
    s = pricing.backfill(statuses=("closet",), dry_run=True)
    assert calls == [len(need)] and s["gemini_calls"] == 1                 # ONE batched call for all items
    assert s["gemini_estimates"] == len(need) and s["table_estimates"] == 0
    e = s["estimates"][0]
    assert e["estimated_price_usd"] == 42.0 and e["source"] == "gemini_text"
    assert e["price_confidence"] == "medium"                                # no brand -> not "high"

    def quota(items):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    monkeypatch.setattr(pricing, "gemini_batch_estimates", quota)
    s = pricing.backfill(statuses=("closet",), dry_run=True)
    assert s["table_estimates"] == len(need) and "429" in s["error"]


def test_backfill_writes_table_estimates_and_is_idempotent(client):
    from app import db
    s = pricing.backfill(statuses=("closet",), use_gemini=False)
    assert s["gemini_calls"] == 0 and s["table_estimates"] == s["items_needing_estimate"] > 0
    it = db.get_item(s["estimates"][0]["id"])
    assert it["attributes"]["price_estimate_source"] == "table" and it["attributes"]["estimated_price_usd"] > 0
    assert it["attributes"].get("price") in (None, "")                     # only estimate keys are written
    assert pricing.backfill(statuses=("closet",), use_gemini=False)["items_needing_estimate"] == 0
