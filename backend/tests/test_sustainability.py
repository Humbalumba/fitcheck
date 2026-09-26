"""Unit tests for app/sustainability.py (pure Python: no DB, no network, no Gemini)."""
import json
import math

import pytest

from app import sustainability as S
from app.sustainability import parse_materials, resolve_garment_type, score_item


def mix(text):
    comps, method = parse_materials(text)
    return {c["material"]: c["share"] for c in comps}, method


# ------------------------------------------------------------------ factor file
def test_factor_file_is_complete_and_sourced():
    F = S.load_factors()
    for key, m in F["materials"].items():
        assert m["co2e_kg_per_kg"] > 0, key
        assert m["source"] in F["sources"], key
        assert m.get("note"), key
        if m.get("water_source"):
            assert m["water_source"] in F["sources"]
    for key, g in F["garment_types"].items():
        assert g["base_wears"] > 0 and g["wears_source"] in F["sources"], key
        if "per_pair_co2e_kg" in g:
            assert g["per_pair_source"] in F["sources"]
        else:
            assert 0 < g["weight_kg"] < 3 and g["weight_source"] in F["sources"], key
    for s in F["sources"].values():
        assert s["url"].startswith("http") and s["title"]
    assert set(F["category_default_type"]) == {"top", "bottom", "outerwear", "dress", "shoes"}


# ------------------------------------------------------------------ material parsing
@pytest.mark.parametrize("text", ["95% cotton 5% elastane", "cotton 95%, elastane 5%", "95 % Cotton / 5 % Spandex",
                                  "Cotton 95% Lycra 5%"])
def test_percentage_blends(text):
    m, method = mix(text)
    assert method == "percentages"
    assert m == pytest.approx({"cotton": 0.95, "polyurethane": 0.05})


def test_three_way_blend_and_normalisation():
    m, _ = mix("50% polyester, 30% viscose, 20% wool")
    assert m == pytest.approx({"polyester": 0.5, "viscose": 0.3, "wool": 0.2})
    m, _ = mix("60% cotton 60% polyester")  # sums to 120 -> normalised
    assert m == pytest.approx({"cotton": 0.5, "polyester": 0.5})


def test_unlisted_remainder_goes_to_unknown():
    m, _ = mix("60% cotton")
    assert m == pytest.approx({"cotton": 0.6, "unknown": 0.4})


def test_keywords_and_fabric_terms():
    assert mix("cotton")[0] == {"cotton": 1.0}
    assert mix("Cotton pique")[0] == {"cotton": 1.0}
    assert mix("cotton jersey")[0] == {"cotton": 1.0}
    assert mix("denim")[0] == {"cotton": 1.0}
    assert mix("satin")[0] == {"polyester": 1.0}
    assert mix("fleece")[0] == {"polyester": 1.0}
    assert mix("merino wool")[0] == {"wool": 1.0}
    assert mix("100% linen")[0] == {"linen": 1.0}
    assert mix("recycled polyester")[0] == {"recycled_polyester": 1.0}
    assert mix("organic cotton")[0] == {"organic_cotton": 1.0}
    assert mix("faux leather")[0] == {"polyurethane": 1.0}
    assert mix("genuine leather")[0] == {"leather": 1.0}
    assert mix("rayon")[0] == {"viscose": 1.0}
    assert mix("cotton and linen")[0] == pytest.approx({"cotton": 0.5, "linen": 0.5})


def test_blend_words():
    m, method = mix("cotton blend")
    assert method == "blend_assumption" and m == pytest.approx({"cotton": 0.5, "unknown": 0.5})
    assert mix("polycotton")[0] == pytest.approx({"cotton": 0.5, "polyester": 0.5})
    assert mix("poly-cotton")[0] == pytest.approx({"cotton": 0.5, "polyester": 0.5})


@pytest.mark.parametrize("text", [None, "", "unknown", "N/A", "rubber", "sparkly stuff"])
def test_unknown_materials_fall_back(text):
    m, method = mix(text)
    assert m == {"unknown": 1.0} and method == "fallback"


def test_polyester_not_double_counted():
    assert mix("polyester")[0] == {"polyester": 1.0}
    assert mix("recycled polyester and polyester")[0] == pytest.approx({"recycled_polyester": 0.5, "polyester": 0.5})


# ------------------------------------------------------------------ garment type
@pytest.mark.parametrize("attrs,cat,expected", [
    ({"subcategory": "t-shirt"}, "top", "tshirt"),
    ({"subcategory": "button-down shirt"}, "top", "shirt"),
    ({"subcategory": "hoodie"}, "outerwear", "sweater"),
    ({"subcategory": "cardigan"}, "outerwear", "sweater"),
    ({"subcategory": "denim jacket"}, "outerwear", "jacket"),
    ({"subcategory": "puffer jacket"}, "outerwear", "coat"),
    ({"subcategory": "jeans"}, "bottom", "jeans"),
    ({"subcategory": "chinos"}, "bottom", "trousers"),
    ({"subcategory": "mini skirt"}, "bottom", "skirt"),
    ({"subcategory": "t-shirt dress"}, "dress", "dress"),
    ({"subcategory": "ankle boots"}, "shoes", "boots"),
    ({"subcategory": "sandals"}, "shoes", "sandals"),
    ({"subcategory": "loafers"}, "shoes", "sneakers"),
    ({}, "top", "tshirt"),
    ({}, "bottom", "trousers"),
    ({"subcategory": "something odd"}, "outerwear", "jacket"),
])
def test_garment_type_resolution(attrs, cat, expected):
    assert resolve_garment_type(attrs, cat)[0] == expected


def test_accessory_and_unknown_are_unsupported():
    r = score_item({"subcategory": "handbag"}, "accessory", 5, 0.5)
    assert r["supported"] is False and r["score"] is None and r["is_estimate"] is True and r["reasons"]
    assert score_item(None, None, None, None)["supported"] is False


# ------------------------------------------------------------------ usage model
def test_utility_curve():
    assert S.utility_multiplier(0) == pytest.approx(0.5)
    assert S.utility_multiplier(3) == pytest.approx(1.0)
    assert S.utility_multiplier(None) == 1.0
    assert S.utility_multiplier(-5) == pytest.approx(0.5)
    assert S.utility_multiplier(10_000) == S.UTILITY_CAP
    vals = [S.utility_multiplier(n) for n in range(0, 80)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    # diminishing returns: the gain from 1->2 outfits exceeds the gain from 40->41
    assert S.utility_multiplier(2) - S.utility_multiplier(1) > S.utility_multiplier(41) - S.utility_multiplier(40)


def test_redundancy_levels():
    assert S.redundancy_level(0.898) == "near_duplicate"
    assert S.redundancy_level(0.88) == "near_duplicate"
    assert S.redundancy_level(0.85) == "similar"
    assert S.redundancy_level(0.7981) == "none"
    assert S.redundancy_level(None) is None
    assert S.redundancy_level("near_duplicate") == "near_duplicate"
    assert S.redundancy_level(float("nan")) is None
    assert S.redundancy_level(0.85, dup_threshold=0.84) == "near_duplicate"


# ------------------------------------------------------------------ scoring
TROUSERS = {"category": "bottom", "subcategory": "trousers", "fabric_guess": "cotton", "primary_color": "white"}


def s(attrs=TROUSERS, cat="bottom", n=10, red=0.5, **kw):
    return score_item(attrs, cat, n, red, **kw)


def test_math_is_transparent():
    r = s(n=42, red=0.7981, price=45)
    F = S.load_factors()
    w = F["garment_types"]["trousers"]["weight_kg"]
    f = F["materials"]["cotton"]["co2e_kg_per_kg"]
    assert r["footprint_kg_co2e"] == pytest.approx(w * f, rel=1e-3)
    wears = 70 * (0.5 + 0.25 * math.log2(43))
    assert r["expected_wears"] == pytest.approx(wears, abs=0.1)
    assert r["per_wear_kg_co2e"] == pytest.approx(w * f / wears, rel=1e-3)
    assert r["water_l"] == pytest.approx(w * 3100, abs=1)
    assert r["cost_per_wear"] == pytest.approx(45 / wears, abs=0.01)
    assert r["is_estimate"] is True and r["methodology"] and r["sources"]
    assert all(k in S.load_factors()["sources"] for k in r["sources"])
    json.dumps(r)  # JSON-serialisable


def test_more_outfits_is_better():
    scores = [s(n=n)["score"] for n in (0, 1, 3, 10, 42, 200)]
    assert all(b >= a for a, b in zip(scores, scores[1:]))
    assert scores[-1] > scores[0]
    pw = [s(n=n)["per_wear_kg_co2e"] for n in (0, 3, 42)]
    assert pw[0] > pw[1] > pw[2]


def test_near_duplicate_is_worse():
    none, similar, dup = s(red=0.6), s(red=0.82), s(red=0.898)
    assert none["score"] > similar["score"] > dup["score"]
    assert dup["expected_wears"] == pytest.approx(none["expected_wears"] * 0.5, abs=0.1)
    assert dup["components"]["redundancy_level"] == "near_duplicate"


def test_polyester_worse_than_linen_all_else_equal():
    poly = s(attrs={**TROUSERS, "fabric_guess": "polyester"})
    linen = s(attrs={**TROUSERS, "fabric_guess": "linen"})
    cotton = s()
    assert linen["score"] > poly["score"]
    assert linen["per_wear_kg_co2e"] < poly["per_wear_kg_co2e"]
    assert linen["score"] > cotton["score"]
    rpet = s(attrs={**TROUSERS, "fabric_guess": "recycled polyester"})
    assert rpet["score"] >= poly["score"]


def test_meaningful_spread_between_bad_and_good_items():
    cami = score_item({"category": "top", "subcategory": "cami", "fabric_guess": "polyester"}, "top", 3, 0.898)
    linen = s(attrs={**TROUSERS, "fabric_guess": "linen"}, n=42, red=0.7)
    assert linen["score"] - cami["score"] >= 40
    assert linen["grade"] in ("A", "B") and cami["grade"] in ("D", "E")


def test_score_bounds_and_grades():
    extremes = [
        score_item({"subcategory": "coat", "fabric_guess": "acrylic"}, "outerwear", 0, 0.99),
        score_item({"subcategory": "coat", "fabric_guess": "wool"}, "outerwear", 0, 0.95),
        score_item({"subcategory": "t-shirt", "fabric_guess": "linen"}, "top", 10_000, 0.1),
        score_item({}, "dress", None, None),
        score_item({"subcategory": "boots"}, "shoes", 5, None),
    ]
    for r in extremes:
        assert 0 <= r["score"] <= 100
        assert r["grade"] in "ABCDE" and r["label"]
        assert 1 <= len(r["reasons"]) <= 3
    assert score_item({}, "dress", None, None)["score"] == 50  # unknown material, typical use == typical item


def test_missing_attributes_are_robust():
    for attrs in (None, {}, {"fabric_guess": None}, {"fabric_guess": "unknown"}, {"subcategory": 123}):
        r = score_item(attrs, "top", 5, None)
        assert r["supported"] and 0 <= r["score"] <= 100
    r = score_item({"fabric_guess": "unknown", "description": "Navy cotton crew-neck t-shirt"}, "top", 5, None)
    assert r["materials"][0]["material"] == "cotton"
    assert r["material_parse"]["method"] == "description_keywords"
    assert r["garment_type"] == "tshirt"
    r = score_item({"material": ["cotton", "elastane"]}, "bottom", 5, None)
    assert {c["material"] for c in r["materials"]} == {"cotton", "polyurethane"}


def test_shoes_use_per_pair_footprint():
    boots = score_item({"subcategory": "ankle boots", "fabric_guess": "leather"}, "shoes", 20, None)
    assert boots["footprint_basis"] == "per_pair"
    assert boots["footprint_kg_co2e"] == pytest.approx(18.65)
    assert boots["water_l"] is None and boots["per_wear_water_l"] is None
    more = score_item({"subcategory": "ankle boots"}, "shoes", 40, None)
    assert more["score"] >= boots["score"]


def test_water_partial_when_factor_missing():
    r = s(attrs={**TROUSERS, "fabric_guess": "55% linen 45% cotton"})
    assert r["water_complete"] is False
    assert r["water_l"] == pytest.approx(0.45 * 0.45 * 3100, abs=1)
    assert s()["water_complete"] is True


def test_reasons_mention_numbers():
    r = s(n=42, red=0.7)
    assert "42 outfits" in r["reasons"][0] and "per wear" in r["reasons"][0]
    assert "Cotton" in r["reasons"][1]
    dup = score_item({"subcategory": "cami", "fabric_guess": "polyester"}, "top", 3, 0.898)
    assert "Near-duplicate" in dup["reasons"][0] and "0.90" in dup["reasons"][0]
    zero = s(n=0)
    assert "Doesn't unlock" in zero["reasons"][0]
