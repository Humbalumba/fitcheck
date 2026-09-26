"""Sustainability estimate for a candidate clothing item (pure Python: no network, no Gemini, no DB).

    footprint_kg_co2e = weight_kg(garment type) x co2e_per_kg(material mix)      [shoes: per-pair LCA value]
    water_l           = weight_kg(garment type) x water_per_kg(material mix)     [shoes: not estimated]
    expected_wears    = base_wears(type) x utility(n_new_outfits) x redundancy_factor
        utility(n)         = min(2.0, 0.5 + 0.25 * log2(1 + n))   0.5x at 0 outfits, 1.0x at 3 (the app's default
                                                                   min_new_outfits), +0.25 per doubling, capped at 2.0x
        redundancy_factor  = 1.0 none | 0.75 similar (>= 0.80) | 0.5 near duplicate (>= 0.88)
    per_wear_*        = footprint / expected_wears
    relative          = per_wear_co2e / per_wear_co2e of a *typical* item of the same type
                        (same weight, average-textile factor, base wears; shoes: same per-pair value)
    score             = clamp(round(50 - 30 * log2(relative)), 0, 100)
                        50 = typical; each halving of the per-wear footprint adds 30 points.

Sourced: material factors, garment weights, base wears, shoe per-pair footprints (see
app/data/sustainability_factors.json and docs/SUSTAINABILITY.md). Modelling assumptions (NOT sourced):
the utility curve, the redundancy factors and the score scale. Everything returned is an estimate.

Public API:  score_item(attributes, category, n_new_outfits, redundancy, price=None) -> dict
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

FACTORS_PATH = Path(__file__).resolve().parent / "data" / "sustainability_factors.json"

# ---- modelling assumptions (documented in docs/SUSTAINABILITY.md; not from a source) ----
UTILITY_FLOOR = 0.5          # item that unlocks nothing: worn half as often as a typical item of its type
UTILITY_PER_DOUBLING = 0.25  # +25% wears per doubling of (1 + new outfits)
UTILITY_CAP = 2.0            # 0.5x..2x = 4x spread (EMF 2017: US utilisation ~1/4 of the global average)
REDUNDANCY_FACTOR = {"none": 1.0, "similar": 0.75, "near_duplicate": 0.5}
DUPLICATE_THRESHOLD = 0.88   # the app's defaults (settings.redundancy_duplicate_threshold)
SIMILAR_THRESHOLD = 0.80     # settings.redundancy_similar_threshold
SCORE_MID = 50
SCORE_POINTS_PER_HALVING = 30
GRADES = [(80, "A", "Great"), (65, "B", "Good"), (50, "C", "Fair"), (35, "D", "Poor"), (0, "E", "Very poor")]

_UNKNOWN_TEXT = {"", "unknown", "none", "n/a", "na", "null", "other", "mixed", "various"}


@lru_cache(maxsize=1)
def load_factors() -> dict:
    with open(FACTORS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ material parsing
# (regex, material key, how) - longest / most specific first. "how" says whether the keyword IS the fibre
# ("fibre") or is a fabric/finish name we map to its typical fibre ("fabric_term").
_MATERIAL_PATTERNS: list[tuple[str, str, str]] = [
    (r"recycled\s+polyester|recycled\s+poly\b|\brpet\b|repreve", "recycled_polyester", "fibre"),
    (r"organic\s+cotton", "organic_cotton", "fibre"),
    (r"faux\s+leather|vegan\s+leather|pu\s+leather|pleather|leatherette|faux\s+suede", "polyurethane", "fabric_term"),
    (r"polyurethane|elastane|spandex|lycra|\belastan\b", "polyurethane", "fibre"),
    (r"polyester|\bpoly\b|\bpes\b|\bpet\b", "polyester", "fibre"),
    (r"micro\s*fib(?:er|re)|fleece|satin|chiffon|georgette|\bsynthetic", "polyester", "fabric_term"),
    (r"nylon|polyamide|\bpa\b", "nylon", "fibre"),
    (r"acrylic|polyacryl", "acrylic", "fibre"),
    (r"cashmere|merino|alpaca|mohair|angora|lambswool|\bwool|woolen|woollen", "wool", "fibre"),
    (r"tweed|boucl[eé]|felt\b", "wool", "fabric_term"),
    (r"viscose|rayon|\bmodal\b|lyocell|tencel|cupro|bamboo|acetate", "viscose", "fibre"),
    (r"linen|\bflax\b|\bhemp\b|\bramie\b", "linen", "fibre"),
    (r"\bsilk", "silk", "fibre"),
    (r"suede|nubuck|\bleather", "leather", "fibre"),
    (r"\bcotton", "cotton", "fibre"),
    (r"denim|chambray|corduroy|\bcord\b|twill|poplin|oxford|piqu[eé]|terry|canvas|seersucker|flannel|jersey|"
     r"gingham|muslin|voile|velour|sweatshirt\s+fabric|french\s+terry", "cotton", "fabric_term"),
]
_POLYCOTTON = re.compile(r"poly[\s\-/]*cotton|cotton[\s\-/]*poly(?:ester)?\s+blend")
_PCT_FIRST = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%\s*([^\d%]*)")
_PCT_LAST = re.compile(r"([^\d%]*?)(\d{1,3}(?:[.,]\d+)?)\s*%")


def _find_materials(text: str) -> list[tuple[int, str, str]]:
    """All non-overlapping keyword hits as (position, material, how), in order of appearance."""
    taken: list[tuple[int, int]] = []
    hits = []
    for pat, mat, how in _MATERIAL_PATTERNS:
        for m in re.finditer(pat, text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            taken.append((s, e))
            hits.append((s, mat, how))
    hits.sort()
    return hits


def _first_material(chunk: str) -> tuple[str, str] | None:
    hits = _find_materials(chunk)
    return (hits[0][1], hits[0][2]) if hits else None


def _merge(parts: list[tuple[str, float, str]]) -> list[dict]:
    out: dict[str, dict] = {}
    for mat, share, how in parts:
        if share <= 0:
            continue
        d = out.setdefault(mat, {"material": mat, "share": 0.0, "how": how})
        d["share"] += share
    total = sum(d["share"] for d in out.values()) or 1.0
    res = sorted(out.values(), key=lambda d: -d["share"])
    for d in res:
        d["share"] = round(d["share"] / total, 4)
    return res


def parse_materials(text: str | None) -> tuple[list[dict], str]:
    """Parse a free-text material string into a weighted mix.

    Returns (components, method): components = [{"material", "share" (0..1, sums to 1), "how"}],
    method = "percentages" | "keywords" | "blend_assumption" | "fallback".
      '95% cotton 5% elastane' / 'cotton 95%, elastane 5%' -> percentages (unlisted remainder -> unknown)
      'cotton and linen'   -> equal split of the recognised fibres
      'cotton blend'       -> 50% cotton + 50% average textile (assumption)
      'polycotton'         -> 50/50 cotton/polyester (WRAP 2012's example blend)
      '' / 'unknown' / unrecognised -> 100% 'unknown' (average textile)
    """
    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    if t in _UNKNOWN_TEXT:
        return [{"material": "unknown", "share": 1.0, "how": "fallback"}], "fallback"

    if "%" in t:
        pat = _PCT_FIRST if re.match(r"^\s*\d", t) else _PCT_LAST
        parts: list[tuple[str, float, str]] = []
        for m in pat.finditer(t):
            if pat is _PCT_FIRST:
                pct, chunk = m.group(1), m.group(2)
            else:
                chunk, pct = m.group(1), m.group(2)
            share = float(pct.replace(",", "."))
            found = _first_material(chunk)
            parts.append((found[0], share, found[1]) if found else ("unknown", share, "unrecognised"))
        total = sum(p[1] for p in parts)
        if parts and total > 0:
            if total < 100:
                parts.append(("unknown", 100 - total, "unlisted_remainder"))
            return _merge(parts), "percentages"

    if _POLYCOTTON.search(t):
        return _merge([("cotton", 0.5, "fibre"), ("polyester", 0.5, "fibre")]), "keywords"

    hits = _find_materials(t)
    mats: list[tuple[str, str]] = []
    for _, mat, how in hits:
        if mat not in [m for m, _ in mats]:
            mats.append((mat, how))
    if not mats:
        return [{"material": "unknown", "share": 1.0, "how": "unrecognised"}], "fallback"
    if len(mats) == 1 and re.search(r"\bblend|\bmix", t):
        return _merge([(mats[0][0], 0.5, mats[0][1]), ("unknown", 0.5, "blend_assumption")]), "blend_assumption"
    return _merge([(m, 1.0 / len(mats), how) for m, how in mats]), "keywords"


def material_text(attributes: dict | None) -> tuple[str, str]:
    """Best material string from the item's attributes -> (text, attribute key used or 'none')."""
    a = attributes or {}
    for key in ("material", "materials", "composition", "fabric", "fabric_guess"):
        v = a.get(key)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v if x)
        if isinstance(v, dict):  # e.g. {"cotton": 95, "elastane": 5}
            v = ", ".join(f"{k} {val}%" for k, val in v.items())
        if v and str(v).strip().lower() not in _UNKNOWN_TEXT:
            return str(v), key
    return "", "none"


# ------------------------------------------------------------------ garment type
_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"t[\s\-]?shirt|\btee\b|\btank\b|\bcami|camisole|\bpolo\b|crop\s+top|bodysuit|\bsinglet", "tshirt"),
    (r"puffer|parka|overcoat|trench|pea\s?coat|anorak|\bcoat\b|duffle", "coat"),
    (r"sweater|jumper|pullover|hoodie|hooded|sweatshirt|cardigan|\bknit|fleece|turtleneck|crewneck\s+sweat", "sweater"),
    (r"blazer|jacket|bomber|\bvest\b|gilet|windbreaker|shacket", "jacket"),
    (r"button[\s\-]?(?:down|up)|\bblouse|\bshirt|overshirt|tunic", "shirt"),
    (r"\bjeans?\b|\bdenim\b", "jeans"),
    (r"\bshorts\b", "shorts"),
    (r"\bskirt", "skirt"),
    (r"trouser|\bpants?\b|chino|slacks|legging|jogger|sweatpants|culottes|cargo", "trousers"),
    (r"dress|jumpsuit|romper|playsuit|\bgown\b", "dress"),
    (r"\bboot|bootie|chelsea", "boots"),
    (r"sandal|flip[\s\-]?flop|\bslides?\b|espadrille|open[\s\-]?toe", "sandals"),
    (r"sneaker|trainer|running\s+shoe|loafer|\bheels?\b|\bpumps?\b|\bflats?\b|oxford|derby|brogue|\bmules?\b|"
     r"\bshoes?\b|moccasin|slip[\s\-]?on", "sneakers"),
    (r"\btop\b|camisole|\bshell\b", "tshirt"),
]
_ALLOWED = {
    "top": {"tshirt", "shirt", "sweater"},
    "bottom": {"jeans", "trousers", "shorts", "skirt"},
    "outerwear": {"jacket", "coat", "sweater"},
    "dress": {"dress"},
    "shoes": {"sneakers", "boots", "sandals"},
}


def resolve_garment_type(attributes: dict | None, category: str | None) -> tuple[str | None, str]:
    """-> (garment type key or None if unsupported, how it was resolved)."""
    a = attributes or {}
    cat = (category or a.get("category") or "").strip().lower() or None
    allowed = _ALLOWED.get(cat) if cat else set().union(*_ALLOWED.values())
    if allowed is None:  # accessory or unknown category
        return None, "unsupported_category"
    for key in ("subcategory", "label", "description", "name", "title"):
        text = str(a.get(key) or "").lower()
        if not text:
            continue
        for pat, typ in _TYPE_PATTERNS:
            if typ in allowed and re.search(pat, text):
                return typ, key
    if cat:
        return load_factors()["category_default_type"][cat], "category_default"
    return None, "unresolved"


# ------------------------------------------------------------------ usage model
def utility_multiplier(n_new_outfits: int | float | None) -> float:
    if n_new_outfits is None:
        return 1.0
    n = max(0.0, float(n_new_outfits))
    return min(UTILITY_CAP, UTILITY_FLOOR + UTILITY_PER_DOUBLING * math.log2(1.0 + n))


def redundancy_level(redundancy, dup_threshold: float = DUPLICATE_THRESHOLD,
                     similar_threshold: float = SIMILAR_THRESHOLD) -> str | None:
    """Accepts a similarity (float) or an app level string; None -> None (unknown)."""
    if redundancy is None:
        return None
    if isinstance(redundancy, str):
        r = redundancy.strip().lower().replace("-", "_").replace(" ", "_")
        return r if r in REDUNDANCY_FACTOR else None
    try:
        s = float(redundancy)
    except (TypeError, ValueError):
        return None
    if math.isnan(s):
        return None
    return "near_duplicate" if s >= dup_threshold else "similar" if s >= similar_threshold else "none"


def grade_for(score: int) -> tuple[str, str]:
    for lo, g, label in GRADES:
        if score >= lo:
            return g, label
    return GRADES[-1][1], GRADES[-1][2]


_NAMES = {"cotton": "Cotton", "organic_cotton": "Organic cotton", "polyester": "Polyester",
          "recycled_polyester": "Recycled polyester", "nylon": "Nylon", "acrylic": "Acrylic", "wool": "Wool",
          "viscose": "Viscose", "linen": "Linen", "silk": "Silk", "polyurethane": "Elastane/PU",
          "leather": "Leather", "unknown": "an average textile"}
_SHORT = {"organic_cotton": "organic cotton", "recycled_polyester": "recycled polyester",
          "polyurethane": "elastane"}
_TYPE_NAMES = {"tshirt": "t-shirt/top", "shirt": "shirt/blouse", "sweater": "sweater", "trousers": "pair of trousers",
               "jeans": "pair of jeans", "shorts": "pair of shorts", "skirt": "skirt", "dress": "dress",
               "jacket": "jacket", "coat": "coat", "sneakers": "pair of shoes", "boots": "pair of boots",
               "sandals": "pair of sandals"}

METHODOLOGY = ("Estimate only. Footprint = typical weight for the garment type x published CO2e/water per kg of its "
               "material mix (WRAP 2012 fibre factors; shoes use per-pair LCA values). Expected wears = PEFCR default "
               "wears for the type, scaled by how many new outfits it unlocks (0.5x-2x, log curve) and halved for a "
               "near-duplicate (x0.75 if similar) - these scalings are modelling assumptions. Score compares the "
               "per-wear footprint with a typical item of the same type in an average textile: 50 = typical, "
               "+30 points per halving.")


def _fmt_kg(x: float) -> str:
    return f"{x:.2f}" if x < 1 else f"{x:.1f}" if x < 100 else f"{x:.0f}"


def _unsupported(category, why: str) -> dict:
    return {"supported": False, "is_estimate": True, "score": None, "grade": None, "label": None,
            "reasons": [f"No sustainability estimate for {category or 'this item'} yet ({why})."],
            "footprint_kg_co2e": None, "water_l": None, "expected_wears": None, "per_wear_kg_co2e": None,
            "per_wear_water_l": None, "cost_per_wear": None, "garment_type": None, "materials": [],
            "methodology": METHODOLOGY, "sources": [], "version": load_factors()["version"]}


def score_item(attributes: dict | None, category: str | None, n_new_outfits: int | None,
               redundancy: float | str | None, price: float | None = None, *,
               dup_threshold: float = DUPLICATE_THRESHOLD, similar_threshold: float = SIMILAR_THRESHOLD) -> dict:
    """Sustainability estimate for one item. Robust to missing/unknown attributes (falls back to the category's
    default garment type and an average-textile factor). `redundancy` = top similarity to the closet (float,
    thresholds 0.88 / 0.80 by default) or an app level string ('none' | 'similar' | 'near_duplicate')."""
    F = load_factors()
    a = attributes or {}
    typ, type_how = resolve_garment_type(a, category)
    if typ is None:
        return _unsupported(category or a.get("category"), type_how.replace("_", " "))
    gt = F["garment_types"][typ]
    avg = F["materials"]["unknown"]
    sources: list[str] = []

    def use(key):
        if key and key not in sources:
            sources.append(key)

    # ---- footprint
    per_pair = "per_pair_co2e_kg" in gt
    if per_pair:
        components, mat_method, mat_key, mat_text = [], "not_modelled_for_shoes", "none", material_text(a)[0]
        footprint = float(gt["per_pair_co2e_kg"])
        water, water_complete = None, False
        weight = None
        mix_factor = None
        reference_footprint = footprint
        use(gt["per_pair_source"])
    else:
        mat_text, mat_key = material_text(a)
        if mat_text:
            components, mat_method = parse_materials(mat_text)
        else:  # nothing in the material fields: look for fibre words in the description/label
            desc = " ".join(str(a.get(k) or "") for k in ("description", "label", "subcategory"))
            hits = [c for c in parse_materials(desc)[0] if c["material"] != "unknown"] if desc.strip() else []
            if hits:
                components, mat_method, mat_key = hits, "description_keywords", "description"
                total = sum(c["share"] for c in components)
                for c in components:
                    c["share"] = round(c["share"] / total, 4)
            else:
                components, mat_method = [{"material": "unknown", "share": 1.0, "how": "fallback"}], "fallback"
        weight = float(gt["weight_kg"])
        mix_factor = 0.0
        water_sum, water_share = 0.0, 0.0
        for c in components:
            m = F["materials"][c["material"]]
            c["co2e_kg_per_kg"] = m["co2e_kg_per_kg"]
            c["water_l_per_kg"] = m["water_l_per_kg"]
            mix_factor += c["share"] * m["co2e_kg_per_kg"]
            if m["water_l_per_kg"] is not None:
                water_sum += c["share"] * m["water_l_per_kg"]
                water_share += c["share"]
            use(m["source"]); use(m.get("water_source"))
        footprint = weight * mix_factor
        water = weight * water_sum if water_share > 0 else None
        water_complete = water_share >= 0.999
        reference_footprint = weight * avg["co2e_kg_per_kg"]
        use(gt["weight_source"])
        use(avg["source"])

    # ---- usage
    base = float(gt["base_wears"])
    use(gt["wears_source"])
    u = utility_multiplier(n_new_outfits)
    level = redundancy_level(redundancy, dup_threshold, similar_threshold)
    rf = REDUNDANCY_FACTOR.get(level, 1.0) if level else 1.0
    wears = base * u * rf
    per_wear = footprint / wears
    per_wear_water = water / wears if water is not None else None
    reference_per_wear = reference_footprint / base
    relative = per_wear / reference_per_wear
    score = int(max(0, min(100, round(SCORE_MID - SCORE_POINTS_PER_HALVING * math.log2(relative)))))
    grade, label = grade_for(score)
    cpw = round(float(price) / wears, 2) if price not in (None, "") and float(price) >= 0 else None

    # ---- reasons (plain English, from the numbers)
    fp_s, w_s, pw_s = _fmt_kg(footprint), f"{wears:.0f}", _fmt_kg(per_wear)
    sim_s = f" ({float(redundancy):.2f} match)" if isinstance(redundancy, (int, float)) else ""
    if level == "near_duplicate":
        r1 = (f"Near-duplicate of something you already own{sim_s}, so the two would share the same occasions: "
              f"~{fp_s} kg CO2e over only ~{w_s} expected wears (~{pw_s} kg per wear).")
    elif n_new_outfits is None:
        r1 = (f"Its ~{fp_s} kg CO2e is spread over ~{w_s} typical wears for a {_TYPE_NAMES[typ]} "
              f"(~{pw_s} kg per wear).")
    elif int(n_new_outfits) <= 0:
        r1 = (f"Doesn't unlock any new outfits, so it would likely be worn less: ~{fp_s} kg CO2e over only "
              f"~{w_s} expected wears (~{pw_s} kg per wear).")
    else:
        n = int(n_new_outfits)
        r1 = (f"Unlocks {n} outfit{'s' if n != 1 else ''}, so its ~{fp_s} kg CO2e is spread over ~{w_s} expected "
              f"wears (~{pw_s} kg per wear).")
        if level == "similar":
            r1 = r1[:-1] + f"; being similar to something you own{sim_s} trims expected wears by 25%."
    if per_pair:
        r2 = (f"Footwear is estimated per pair (~{_fmt_kg(footprint)} kg CO2e for typical "
              f"{_TYPE_NAMES[typ].replace('pair of ', '')} in published LCAs); the material isn't modelled.")
    else:
        known = [c for c in components if c["material"] != "unknown"]
        avg_f = avg["co2e_kg_per_kg"]
        if not known:
            r2 = f"Material unknown, so we assumed an average textile (~{avg_f:.0f} kg CO2e per kg)."
        else:
            if len(known) == 1 and known[0]["share"] >= 0.999:
                name = _NAMES[known[0]["material"]]
            else:
                name = "This " + "/".join(_SHORT.get(c["material"], c["material"]) for c in known[:3]) + " blend"
            ratio = mix_factor / avg_f
            cmp = ("a lower carbon footprint per kg than" if ratio <= 0.85 else
                   "a higher carbon footprint per kg than" if ratio >= 1.15 else
                   "roughly the same carbon footprint per kg as")
            r2 = f"{name} has {cmp} an average textile ({mix_factor:.0f} vs {avg_f:.0f} kg CO2e/kg)"
            dom = known[0]["material"]
            if dom in ("cotton", "viscose") and known[0]["share"] >= 0.5:
                r2 += f", and it's water-intensive (~{F['materials'][dom]['water_l_per_kg']:,} L/kg)"
            elif dom in ("polyester", "nylon", "acrylic") and known[0]["share"] >= 0.5:
                r2 += ", and it's fossil-based and sheds microfibres"
            r2 += "."

    return {
        "supported": True,
        "is_estimate": True,
        "score": score,
        "grade": grade,
        "label": label,
        "reasons": [r1, r2],
        "footprint_kg_co2e": round(footprint, 2),
        "water_l": round(water) if water is not None else None,
        "water_complete": water_complete,
        "expected_wears": round(wears, 1),
        "per_wear_kg_co2e": round(per_wear, 4),
        "per_wear_water_l": round(per_wear_water, 1) if per_wear_water is not None else None,
        "cost_per_wear": cpw,
        "garment_type": typ,
        "garment_type_resolved_from": type_how,
        "footprint_basis": "per_pair" if per_pair else "per_kg",
        "weight_kg": weight,
        "materials": components,
        "material_parse": {"text": mat_text, "attribute": mat_key, "method": mat_method},
        "components": {
            "base_wears": base,
            "utility_multiplier": round(u, 3),
            "n_new_outfits": n_new_outfits,
            "redundancy_level": level,
            "redundancy_similarity": float(redundancy) if isinstance(redundancy, (int, float)) else None,
            "redundancy_multiplier": rf,
            "material_co2e_kg_per_kg": round(mix_factor, 2) if mix_factor is not None else None,
            "reference_per_wear_kg_co2e": round(reference_per_wear, 4),
            "relative_to_typical": round(relative, 3),
        },
        "methodology": METHODOLOGY,
        "sources": sources,
        "version": F["version"],
    }
