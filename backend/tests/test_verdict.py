"""The BUY / CONSIDER / SKIP formula (app/verdict.py): pure math, no DB, no models, no LLM."""
import json
from pathlib import Path

import pytest

from app import verdict as V
from app.sustainability import expected_wears

DEFAULT_CTX = {"category_counts": {"top": 8, "bottom": 6, "outerwear": 3, "dress": 1, "shoes": 4},
               "type_counts": {"tshirt": 3, "shirt": 2, "trousers": 3, "jeans": 3, "jacket": 3, "dress": 1,
                               "sneakers": 4},
               "price_bar": V.DEFAULT_PRICE_BAR, "price_bar_source": "default", "priced_items": 0}


def run(category="top", sub="shirt", n=10, M=40, scores=None, price=50.0, level="none", sim=0.5, ctx=None,
        match="navy shirt"):
    scores = scores if scores is not None else [0.85] * n
    return V.compute_verdict(category=category, attributes={"subcategory": sub}, n_outfits=n, max_possible=M,
                             outfit_scores=scores, price=price, redundancy_level=level, top_similarity=sim,
                             top_match_name=match, context=ctx or DEFAULT_CTX)


def test_a_expensive_shirt_with_ten_good_outfits_is_not_skipped():
    value, d = run(category="top", sub="shirt", n=10, M=40, scores=[0.9] * 10, price=120.0)
    assert d["decision"] in ("BUY", "CONSIDER"), d
    assert value["cost_per_wear"] == pytest.approx(120 / expected_wears({"subcategory": "shirt"}, "top", 10)["wears"],
                                                   abs=0.01)
    # with a closet of pricier clothes (personal bar ~$2.50/wear) the same shirt is a BUY
    rich = {**DEFAULT_CTX, "price_bar": 2.5, "price_bar_source": "closet_median", "priced_items": 12}
    _, d2 = run(category="top", sub="shirt", n=10, M=40, scores=[0.9] * 10, price=120.0, ctx=rich)
    assert d2["decision"] == "BUY" and d2["score"] > d["score"]
    assert any("your usual $2.50" in r for r in d2["all_reasons"])


def test_b_dress_two_of_three_possible_outfits_at_reasonable_price_is_buy():
    value, d = run(category="dress", sub="midi dress", n=2, M=3, scores=[0.8, 0.75], price=45.0)
    assert d["decision"] == "BUY", d
    assert value["versatility"] == {"new_outfits": 2, "max_possible": 3, "share": 0.667}
    assert "Makes 2 of the 3 outfits a dress can make with your closet" in d["reasons"]
    # the same count for a top in a big closet is weak versatility
    _, top = run(category="top", sub="t-shirt", n=2, M=40, scores=[0.8, 0.75], price=45.0)
    assert top["components"]["versatility"]["score"] < d["components"]["versatility"]["score"] - 25


def test_c_near_duplicate_is_always_skip():
    for price in (5.0, 30.0, None):
        _, d = run(n=30, M=40, scores=[1.0] * 30, price=price, level="near_duplicate", sim=0.93)
        assert d["decision"] == "SKIP"
        assert d["reasons"][0] == "Very similar to your navy shirt (0.93 match)"
        assert len(d["reasons"]) == 2
        # the score is capped into the SKIP band (so the number never contradicts the verdict)
        assert d["score"] < V.CONSIDER_AT <= d["uncapped_score"] and d["capped_by"] == "near_duplicate"


def test_d_cheap_but_low_versatility_crowded_item_is_not_buy():
    ctx = {**DEFAULT_CTX, "type_counts": {**DEFAULT_CTX["type_counts"], "tshirt": 7}}
    _, d = run(category="top", sub="t-shirt", n=2, M=48, scores=[0.6, 0.55], price=10.0, ctx=ctx)
    assert d["decision"] in ("SKIP", "CONSIDER"), d
    assert d["components"]["gap_fill"]["points"] == V.CROWDED_PENALTY
    assert "You already own 7 t-shirts and tops" in d["all_reasons"]
    # ... and one that goes with nothing you own is a SKIP however cheap
    _, d0 = run(category="top", sub="t-shirt", n=0, M=48, scores=[], price=5.0, ctx=ctx)
    assert d0["decision"] == "SKIP" and d0["reasons"][0] == "Doesn't go with anything in your closet yet"
    # nothing in the closet to pair it with yet (e.g. first bottom, no tops): never an outright BUY
    empty = {**DEFAULT_CTX, "category_counts": {}, "type_counts": {}}
    _, lone = run(category="bottom", sub="jeans", n=0, M=0, scores=[], price=5.0, ctx=empty)
    assert lone["decision"] in ("CONSIDER", "SKIP") and lone["score"] < V.BUY_AT


def test_e_missing_price_drops_cost_and_renormalises():
    value, d = run(n=10, M=40, price=None)
    c = d["components"]
    assert value["cost_per_wear"] is None and c["cost"]["score"] is None and c["cost"]["weight"] == 0
    w = V.WEIGHTS["versatility"] + V.WEIGHTS["outfit_quality"]
    expect = (V.WEIGHTS["versatility"] * c["versatility"]["score"]
              + V.WEIGHTS["outfit_quality"] * c["outfit_quality"]["score"]) / w
    assert d["score"] == round(expect + c["gap_fill"]["points"] + c["similarity"]["points"])
    assert c["versatility"]["points"] + c["outfit_quality"]["points"] == pytest.approx(expect, abs=0.1)
    assert d["decision"] in ("BUY", "CONSIDER", "SKIP") and not any("per wear" in r for r in d["reasons"])
    assert "Add a price to see the cost per wear" in d["all_reasons"]


def test_f_gap_fill_bonus():
    no_dress = {**DEFAULT_CTX, "category_counts": {**DEFAULT_CTX["category_counts"], "dress": 0},
                "type_counts": {**DEFAULT_CTX["type_counts"], "dress": 0}}
    many = {**DEFAULT_CTX, "category_counts": {**DEFAULT_CTX["category_counts"], "dress": 6},
            "type_counts": {**DEFAULT_CTX["type_counts"], "dress": 6}}
    kw = dict(category="dress", sub="slip dress", n=2, M=4, scores=[0.7, 0.6], price=70.0)
    _, first = run(ctx=no_dress, **kw)
    _, normal = run(**kw)
    _, crowded = run(ctx=many, **kw)
    assert first["components"]["gap_fill"]["points"] == V.GAP_FIRST_CATEGORY
    assert normal["components"]["gap_fill"]["points"] == 0
    assert crowded["components"]["gap_fill"]["points"] == V.CROWDED_PENALTY
    assert first["score"] - normal["score"] == V.GAP_FIRST_CATEGORY
    assert normal["score"] - crowded["score"] == -V.CROWDED_PENALTY
    assert "Would be your first dress" in first["all_reasons"]
    # first of its garment type (a jacket when you only own coats) -> smaller bonus
    coats = {**DEFAULT_CTX, "type_counts": {"coat": 3}}
    _, j = run(category="outerwear", sub="denim jacket", n=10, M=30, price=60.0, ctx=coats)
    assert j["components"]["gap_fill"]["points"] == V.GAP_FIRST_TYPE and "Would be your first jacket" in j["all_reasons"]


def test_g_no_budget_anywhere():
    from app.config import DEFAULT_SETTINGS
    value, d = run()
    blob = json.dumps([value, d, DEFAULT_SETTINGS]).lower()
    assert "budget" not in blob
    for f in ("verdict.py", "evaluate.py", "suggest.py", "config.py", "main.py", "db.py"):
        assert "budget" not in (Path(V.__file__).parent / f).read_text().lower(), f


def test_three_bands_and_top_two_reasons():
    _, buy = run(n=20, M=24, scores=[0.95] * 20, price=30.0)
    _, skip = run(n=1, M=60, scores=[0.52], price=150.0)
    assert buy["decision"] == "BUY" and buy["score"] >= V.BUY_AT
    assert skip["decision"] == "SKIP" and skip["score"] < V.CONSIDER_AT
    for d in (buy, skip):
        assert len(d["reasons"]) == 2 and all(isinstance(r, str) and r for r in d["reasons"])
        assert set(d["components"]) == {"versatility", "outfit_quality", "cost", "gap_fill", "similarity"}
    # somewhere in between is CONSIDER, with one reason for and one against
    _, mid = run(category="top", sub="shirt", n=10, M=40, scores=[0.9] * 10, price=120.0)
    assert mid["decision"] == "CONSIDER" and V.CONSIDER_AT <= mid["score"] < V.BUY_AT
    assert any("per wear, above" in r for r in mid["reasons"])
    assert "Makes 10 new outfits with your closet (40 possible for a top)" in mid["reasons"]
    assert buy["bands"] == {"BUY": 65, "CONSIDER": 45} and buy["formula_version"] == V.FORMULA_VERSION


def test_diminishing_returns_and_quality():
    s10 = V.versatility_score(10, 80)
    s40 = V.versatility_score(40, 80)
    assert s40 > s10 and s40 < 2 * s10       # 40 outfits is not 4x (or even 2x) better than 10
    assert V.versatility_score(0, 10) == 0 and V.versatility_score(3, 0) == 0
    assert V.versatility_score(1, 1) < 1     # 1 of 1 possible isn't full marks
    assert V.versatility_score(3, 3) == pytest.approx(1.0)
    assert V.quality_score([0.9, 0.9, 0.9, 0.9, 0.9, 0.1, 0.1]) == pytest.approx(0.9)  # best 5 only
    assert V.quality_score([]) == 0
    _, weak = run(n=10, M=40, scores=[0.52] * 10)
    _, strong = run(n=10, M=40, scores=[0.95] * 10)
    assert strong["score"] > weak["score"]


def test_similar_penalty_and_cost_uses_shared_wears_model():
    v_none, d_none = run(level="none", price=40.0)
    v_sim, d_sim = run(level="similar", sim=0.84, price=40.0)
    assert d_sim["components"]["similarity"]["points"] == V.SIMILAR_PENALTY
    assert "Similar to your navy shirt (0.84 match)" in d_sim["all_reasons"]
    # similar -> fewer expected wears (sustainability's x0.75) -> higher cost per wear
    assert v_sim["expected_wears"] == pytest.approx(0.75 * v_none["expected_wears"], abs=0.1)
    assert v_sim["cost_per_wear"] > v_none["cost_per_wear"] and d_sim["score"] < d_none["score"]


def test_price_bar_from_closet_median():
    def it(cat, sub, price=None):
        return {"category": cat, "attributes": {"subcategory": sub, **({"price": price} if price else {})}}
    # < 3 real prices: real prices (counted twice) + estimates; $0.75 default only with nothing at all
    few = V.closet_context([it("top", "t-shirt", 45), it("bottom", "jeans", 70)])
    assert few["price_bar_source"] == "closet_estimates" and few["price_bar"] == pytest.approx(1.0)
    empty = V.closet_context([])
    assert empty["price_bar_source"] == "default" and empty["price_bar"] == V.DEFAULT_PRICE_BAR
    closet = [it("top", "t-shirt", 45), it("bottom", "jeans", 140), it("dress", "dress", 210),
              it("top", "t-shirt"), {"category": "shoes", "attributes": {"subcategory": "boots", "purchase_price": 300}}]
    ctx = V.closet_context(closet)
    # price / PEFCR base wears: 45/45=1.0, 140/70=2.0, 210/70=3.0, 300/100=3.0 -> median 2.5
    assert ctx["price_bar_source"] == "closet_median" and ctx["priced_items"] == 4
    assert ctx["price_bar"] == pytest.approx(2.5)
    assert ctx["category_counts"] == {"top": 2, "bottom": 1, "dress": 1, "shoes": 1}
    assert ctx["type_counts"]["tshirt"] == 2
    _, d = run(price=60.0, ctx={**DEFAULT_CTX, **{k: ctx[k] for k in ("price_bar", "price_bar_source", "priced_items")}})
    assert any("your usual $2.50" in r for r in d["all_reasons"])


def test_wildly_overpriced_item_is_skipped_even_if_versatile():
    _, d = run(category="bottom", sub="trousers", n=42, M=56, scores=[1.0] * 42, price=900.0)
    assert d["decision"] == "SKIP" and d["components"]["cost"]["score"] < 0
