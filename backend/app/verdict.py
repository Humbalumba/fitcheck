"""THE BUY / CONSIDER / SKIP verdict (pure math: no LLM, no DB, no models -> unit-testable).

    score (0-100) = weighted mean of three 0..1 components (weights renormalised when there is no price)
                    + gap-fill points + similarity points, clamped to 0..100

    1. versatility (40%)  = 1/2 * log(1+n) / log(1+M')             share of the outfits this item's slot COULD make
                          + 1/2 * min(1, log(1+n) / log(1+min(8, M')))   count, saturating at 8 (or M' if smaller)
         M' = max(M, 2) so 1 of 1 possible outfit isn't full marks.
         n = new outfits that passed "match strictness", M = max possible outfits for this category with this closet
         (every base look the outfit builder tries, e.g. dress: 1 + #outerwear; top: #bottoms x (1 + #outerwear)).
         Both halves are logarithmic: 40 outfits is not 4x better than 10.
    2. outfit quality (20%) = mean calibrated compatibility of the best 5 outfits (0 if none)
    3. cost (40%)           = clamp(0.6 + 0.30 * log2(price_bar / cost_per_wear), -0.5, 1)
         cost_per_wear = price / expected_wears; expected_wears = the shared wears model in app.sustainability
         (PEFCR base wears for the garment type x utility(n) [log, 0.5x..2x] x 0.75 similar / 0.5 near-duplicate).
         price = the user's price, else a tag price, else the ESTIMATE (app.pricing: Gemini brand/type/material
         estimate, or a per-type table). Estimated prices keep the cost weight x1 / x0.75 / x0.5 for high / medium /
         low confidence (weights renormalised), and the reason says "est.".
         price_bar = "your usual cost per wear" = median over closet items that have a real (user/tag) price of
         price / base_wears(their type) when >= 3 do ("closet_median"); otherwise the median over real prices
         (counted twice) + the estimates of unpriced closet items ("closet_estimates"); DEFAULT_PRICE_BAR
         ($0.75/wear, ~ a $50 item worn the PEFCR-typical ~67 times) only when the closet has no prices or
         estimates at all. At the bar -> 0.6; each halving of cost per wear +0.3, each
         doubling -0.3 (so ~2.5x the bar scores 0.2, 4x scores 0; past 4x it goes negative, down to -0.5 at ~9x,
         so a wildly overpriced item can't coast to BUY on versatility alone).
    gap fill: +12 first item of its category (first dress, first outerwear), +6 first of its garment type (first
              sweater among t-shirts), -6 when you already own >= 5 of that garment type.
    similarity: -5 if "similar" to something you own.

    BUY >= 65, CONSIDER 45-64, SKIP < 45. Automatic SKIP (score capped at 44): near-duplicate of something you own, or
    no outfits although the closet has partners for it. Nothing in the closet to pair it with yet: capped at 64.
    reasons = the 2 factors that moved the score most (BUY: most positive, SKIP: most negative, CONSIDER: one each).
"""
from __future__ import annotations

import math
import statistics

from .pricing import estimate_for, real_price
from .sustainability import base_wears, expected_wears

FORMULA_VERSION = 2
WEIGHTS = {"versatility": 0.40, "outfit_quality": 0.20, "cost": 0.40}
BUY_AT = 65
CONSIDER_AT = 45
ABS_SATURATION = 8          # outfits at which the absolute-count half of versatility maxes out
MIN_MAX_POSSIBLE = 2        # tiny categories: 1 of 1 possible outfit isn't full marks
TOP_K = 5                   # outfit quality = mean score of the best K outfits
DEFAULT_PRICE_BAR = 0.75    # $/wear when the closet has < MIN_PRICED_ITEMS prices (~ a $50 item worn ~67 times)
MIN_PRICED_ITEMS = 3
REAL_PRICE_WEIGHT = 2       # when the bar mixes real prices and estimates, each real price counts twice
ESTIMATE_COST_WEIGHT = {"high": 1.0, "medium": 0.75, "low": 0.5}   # cost weight multiplier for estimated prices
PRICE_BAR_CLAMP = (0.10, 10.0)
COST_AT_BAR = 0.6
COST_PER_DOUBLING = 0.30
COST_FLOOR = -0.5           # below 0 only past 4x the bar
GAP_FIRST_CATEGORY = 12
GAP_FIRST_TYPE = 6
CROWDED_TYPE_COUNT = 5
CROWDED_PENALTY = -6
SIMILAR_PENALTY = -5

NOUN = {"top": "top", "bottom": "bottom", "outerwear": "layer", "dress": "dress", "shoes": "pair of shoes"}
CATEGORY_NAME = {"top": "top", "bottom": "bottom", "outerwear": "jacket or layer", "dress": "dress",
                 "shoes": "pair of shoes"}
TYPE_SINGULAR = {"tshirt": "t-shirt or top", "shirt": "shirt", "sweater": "sweater", "trousers": "pair of trousers",
                 "jeans": "pair of jeans", "shorts": "pair of shorts", "skirt": "skirt", "dress": "dress",
                 "jacket": "jacket", "coat": "coat", "sneakers": "pair of shoes", "boots": "pair of boots",
                 "sandals": "pair of sandals"}
TYPE_PLURAL = {"tshirt": "t-shirts and tops", "shirt": "shirts", "sweater": "sweaters", "trousers": "trousers",
               "jeans": "pairs of jeans", "shorts": "pairs of shorts", "skirt": "skirts", "dress": "dresses",
               "jacket": "jackets", "coat": "coats", "sneakers": "pairs of shoes", "boots": "pairs of boots",
               "sandals": "pairs of sandals"}


def _price(a: dict) -> float | None:
    rp = real_price(a)
    return rp[0] if rp else None


def closet_context(closet: list[dict], counts_filter=None) -> dict:
    """Everything the verdict needs to know about the closet (besides outfits): per-category / per-garment-type
    counts (only items passing `counts_filter`, e.g. same gender presentation as the candidate) and the personal
    price bar (all priced items). Pure function of the item dicts."""
    cat_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    per_wear: list[float] = []
    est_per_wear: list[float] = []
    for it in closet:
        a = it.get("attributes") or {}
        cat = it.get("category") or a.get("category")
        counted = counts_filter is None or counts_filter(it)
        if cat and counted:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        bw = base_wears(a, cat)
        if bw:
            if counted:
                type_counts[bw[0]] = type_counts.get(bw[0], 0) + 1
            rp = real_price(a, it.get("purchase_price"))
            if rp is not None:
                per_wear.append(rp[0] / bw[1])
            else:
                est_per_wear.append(estimate_for(a, cat)["estimated_price_usd"] / bw[1])
    if len(per_wear) >= MIN_PRICED_ITEMS:
        vals, src = per_wear, "closet_median"
    elif per_wear or est_per_wear:
        vals, src = per_wear * REAL_PRICE_WEIGHT + est_per_wear, "closet_estimates"
    else:
        vals, src = [], "default"
    bar = min(max(statistics.median(vals), PRICE_BAR_CLAMP[0]), PRICE_BAR_CLAMP[1]) if vals else DEFAULT_PRICE_BAR
    return {"category_counts": cat_counts, "type_counts": type_counts, "price_bar": round(bar, 4),
            "price_bar_source": src, "priced_items": len(per_wear), "estimated_items": len(est_per_wear)}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def versatility_score(n: int, max_possible: int) -> float:
    if n <= 0 or max_possible <= 0:
        return 0.0
    m = max(max_possible, MIN_MAX_POSSIBLE)
    rel = math.log1p(n) / math.log1p(m)
    ab = math.log1p(n) / math.log1p(min(ABS_SATURATION, m))
    return _clamp01(0.5 * _clamp01(rel) + 0.5 * _clamp01(ab))


def quality_score(scores: list[float]) -> float:
    if not scores:
        return 0.0
    best = sorted((float(s) for s in scores), reverse=True)[:TOP_K]
    return _clamp01(sum(best) / len(best))


def cost_score(cpw: float, bar: float) -> float:
    """1 = far cheaper per wear than your bar, 0.6 = at it, 0 = 4x it; floors at -0.5 (~9x) so a wildly overpriced
    item can't coast to BUY on versatility alone."""
    if cpw <= 0:
        return 1.0
    return max(COST_FLOOR, min(1.0, COST_AT_BAR + COST_PER_DOUBLING * math.log2(bar / cpw)))


def _money(x: float) -> str:
    return f"${x:.2f}" if x < 100 else f"${x:.0f}"


def compute_verdict(*, category: str | None, attributes: dict | None, n_outfits: int, max_possible: int,
                    outfit_scores: list[float], price: float | None, redundancy_level: str,
                    top_similarity: float | None, top_match_name: str | None, context: dict,
                    dup_threshold: float = 0.88, similar_threshold: float = 0.80,
                    price_source: str | None = None, price_confidence: str | None = None) -> tuple[dict, dict]:
    """-> (value, verdict). All inputs explicit (see module docstring for the formula).
    price_source: "user" | "tag" | "estimated" (None = treated as a real price); price_confidence for estimates."""
    a = attributes or {}
    estimated = price is not None and price_source == "estimated"
    if price is not None and not price_source:
        price_source = "user"
    cost_weight = WEIGHTS["cost"] * (ESTIMATE_COST_WEIGHT.get(price_confidence or "low", 0.5) if estimated else 1.0)
    n = max(0, int(n_outfits))
    M = max(0, int(max_possible))
    noun = NOUN.get(category or "", "item")
    comp: dict[str, dict] = {}
    cands: list[tuple[float, str]] = []   # (points relative to neutral, reason)

    # 1. versatility
    v = versatility_score(n, M)
    comp["versatility"] = {"score": round(100 * v), "weight": WEIGHTS["versatility"], "new_outfits": n,
                           "max_possible": M}
    # 2. outfit quality
    q = quality_score(outfit_scores)
    comp["outfit_quality"] = {"score": round(100 * q), "weight": WEIGHTS["outfit_quality"],
                              "top_k": min(TOP_K, len(outfit_scores))}
    # 3. cost per wear (shared wears model)
    ew = expected_wears(a, category, n, redundancy_level, dup_threshold=dup_threshold,
                        similar_threshold=similar_threshold)
    wears = ew["wears"] if ew else None
    bar = float(context.get("price_bar") or DEFAULT_PRICE_BAR)
    personal = context.get("price_bar_source") in ("closet_median", "closet_estimates")
    cpw = None
    if price is not None and wears:
        cpw = float(price) / wears
        k = cost_score(cpw, bar)
        comp["cost"] = {"score": round(100 * k), "weight": round(cost_weight, 3), "cost_per_wear": round(cpw, 2),
                        "price_bar": round(bar, 2), "price_bar_source": context.get("price_bar_source"),
                        "price_source": price_source, "price_confidence": price_confidence if estimated else None}
    else:
        comp["cost"] = {"score": None, "weight": 0.0, "cost_per_wear": None, "price_bar": round(bar, 2),
                        "price_bar_source": context.get("price_bar_source"),
                        "dropped": "no price" if price is None else "no wears estimate"}

    wsum = sum(c["weight"] for c in comp.values() if c.get("score") is not None)
    base = sum(c["weight"] * c["score"] for c in comp.values() if c.get("score") is not None) / wsum
    for c in comp.values():
        if c.get("score") is not None:
            c["points"] = round(c["weight"] / wsum * c["score"], 1)

    # reasons for the three components (delta = points above/below a neutral 50)
    def delta(key: str) -> float:
        c = comp[key]
        return c["weight"] / wsum * (c["score"] - 50)

    if n == 0:
        cands.append((delta("versatility") - 10, "Doesn't go with anything in your closet yet" if M > 0
                      else f"Nothing in your closet to wear a {noun} with yet"))
    elif v >= 0.5 and n >= 0.5 * M:
        cands.append((delta("versatility"), f"Makes {n} of the {M} outfits a {noun} can make with your closet"))
    elif v >= 0.5:
        cands.append((delta("versatility"), f"Makes {n} new outfits with your closet ({M} possible for a {noun})"))
    else:
        cands.append((delta("versatility"), f"Only makes {n} of the {M} outfits a {noun} could make with your closet"))
    if n > 0:
        word = "strong" if q >= 0.8 else "good" if q >= 0.65 else "only borderline"
        cands.append((delta("outfit_quality"), f"Its best outfits are {word} matches ({q:.2f} average score)"))
    if cpw is not None:
        approx = "~" if context.get("price_bar_source") == "closet_estimates" else ""
        ref = f"your usual {approx}{_money(bar)}" if personal else f"a typical {_money(bar)}"
        ratio = cpw / bar
        rel = "in line with" if 0.9 <= ratio <= 1.1 else "below" if ratio < 1 else "above"
        est = f" (est. price ~${float(price):.0f})" if estimated else ""
        cands.append((delta("cost"), f"About {_money(cpw)} per wear{est}, {rel} {ref}"))

    # gap fill
    cat_n = int((context.get("category_counts") or {}).get(category or "", 0))
    typ = ew["garment_type"] if ew else None
    type_n = int((context.get("type_counts") or {}).get(typ or "", 0)) if typ else 0
    gap, gap_txt = 0, None
    if category and cat_n == 0:
        gap, gap_txt = GAP_FIRST_CATEGORY, f"Would be your first {CATEGORY_NAME.get(category, category)}"
    elif typ and type_n == 0:
        gap, gap_txt = GAP_FIRST_TYPE, f"Would be your first {TYPE_SINGULAR.get(typ, typ)}"
    elif typ and type_n >= CROWDED_TYPE_COUNT:
        gap, gap_txt = CROWDED_PENALTY, f"You already own {type_n} {TYPE_PLURAL.get(typ, typ)}"
    comp["gap_fill"] = {"points": gap, "category_count": cat_n, "garment_type": typ, "type_count": type_n}
    if gap_txt:
        cands.append((float(gap), gap_txt))

    # similarity (near-duplicate = automatic SKIP below)
    sim_pts = SIMILAR_PENALTY if redundancy_level == "similar" else 0
    comp["similarity"] = {"points": sim_pts, "level": redundancy_level,
                          "top_similarity": round(float(top_similarity), 4) if top_similarity is not None else None}
    sim_s = f" ({float(top_similarity):.2f} match)" if top_similarity is not None else ""
    if redundancy_level == "similar":
        cands.append((float(sim_pts), f"Similar to your {top_match_name or 'closet item'}{sim_s}"))

    raw_score = int(round(max(0.0, min(100.0, base + gap + sim_pts))))
    dup = redundancy_level == "near_duplicate"
    # automatic outcomes cap the score so it always sits inside its verdict's band
    cap, cap_why = 100, None
    if dup:
        cap, cap_why = CONSIDER_AT - 1, "near_duplicate"
    elif n == 0 and M > 0:
        cap, cap_why = CONSIDER_AT - 1, "no_outfits"
    elif n == 0:
        cap, cap_why = BUY_AT - 1, "nothing_to_pair_with_yet"
    score = min(raw_score, cap)
    decision = "BUY" if score >= BUY_AT else "CONSIDER" if score >= CONSIDER_AT else "SKIP"

    # the 2 reasons that moved the score most, in the direction of the decision
    pos = sorted([c for c in cands if c[0] > 0], key=lambda c: -c[0])
    neg = sorted([c for c in cands if c[0] < 0], key=lambda c: c[0])
    by_mag = sorted(cands, key=lambda c: -abs(c[0]))
    if dup:
        picked = [f"Very similar to your {top_match_name or 'closet item'}{sim_s}"] + [c[1] for c in (neg or by_mag)[:1]]
    elif decision == "BUY":
        picked = [c[1] for c in (pos + neg)[:2]]
    elif decision == "SKIP":
        picked = [c[1] for c in (neg + pos)[:2]]
    else:
        picked = ([pos[0][1]] if pos else []) + ([neg[0][1]] if neg else [])
        for c in by_mag:
            if len(picked) >= 2:
                break
            if c[1] not in picked:
                picked.append(c[1])
    reasons = picked[:2] or ["Not enough information to score this item"]
    all_reasons = [c[1] for c in by_mag]
    if price is None:
        all_reasons.append("Add a price to see the cost per wear")
    elif estimated:
        all_reasons.append("Price estimated from brand and type — enter the price for a more accurate verdict")

    value = {"price": price, "currency": "USD",
             "price_source": price_source if price is not None else None,
             "price_confidence": price_confidence if estimated else None,
             "cost_per_wear": round(cpw, 2) if cpw is not None else None,
             "expected_wears": round(wears, 1) if wears else None,
             "price_bar": round(bar, 2), "price_bar_source": context.get("price_bar_source"),
             "priced_closet_items": context.get("priced_items", 0),
             "estimated_closet_items": context.get("estimated_items", 0),
             "versatility": {"new_outfits": n, "max_possible": M, "share": round(n / M, 3) if M else None},
             "outfit_quality": round(q, 3), "weighted_outfits": round(float(sum(outfit_scores)), 3),
             "value_score": score}
    verdict = {"decision": decision, "score": score, "uncapped_score": raw_score, "capped_by": cap_why,
               "reasons": reasons, "all_reasons": all_reasons,
               "components": comp, "bands": {"BUY": BUY_AT, "CONSIDER": CONSIDER_AT},
               "formula_version": FORMULA_VERSION}
    return value, verdict
