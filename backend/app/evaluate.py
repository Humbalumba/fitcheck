"""Stages 3-7: closet retrieval, redundancy, outfit candidate generation, value math and verdict."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from . import db
from .pipeline import item_to_api
from .scoring import calibrate, calibration_table, ensure_compat_embeddings, score_outfits_cached, scorer_kind
from .vectors import closet_index, ensure_fclip

log = logging.getLogger("fitcheck.evaluate")

DISPLAY_ORDER = {"top": 0, "dress": 0, "bottom": 1, "outerwear": 2, "shoes": 3, "accessory": 4}
MAX_OUTFITS_RETURNED_PER_TEMPLATE = 60
MATCH_DISPLAY_FLOOR = 0.75  # redundancy matches below this aren't worth showing
REDUNDANCY_FACTOR = {"none": 1.0, "similar": 0.7, "near_duplicate": 0.2}


def item_name(it: dict) -> str:
    a = it.get("attributes") or {}
    color = a.get("primary_color") or ""
    sub = a.get("subcategory") or a.get("category") or it.get("category") or "item"
    return f"{color} {sub}".strip()


def gender_ok(a: dict, b: dict) -> bool:
    ga = (a.get("attributes") or {}).get("gender_presentation") or "unisex"
    gb = (b.get("attributes") or {}).get("gender_presentation") or "unisex"
    return ga == "unisex" or gb == "unisex" or ga == gb


# ------------------------------------------------------------------ Stage 4
def redundancy_text(it: dict) -> str:
    a = it.get("attributes") or {}
    p = a.get("pattern")
    return (f"a photo of a {a.get('primary_color') or ''} {p + ' ' if p and p != 'solid' else ''}"
            f"{a.get('subcategory') or a.get('category') or it.get('category') or 'garment'}").replace("  ", " ")


def redundancy(cand: dict, closet_by_id: dict[str, dict], settings: dict) -> dict:
    """Similarity = (1-w) * image cosine (FAISS, fashion-clip cutout embeddings)
                  +    w  * text cosine (fashion-clip text embeddings of "color pattern subcategory").
    Pure image cosine confuses e.g. navy vs black polos on small photos; the text term (w=0.3 default)
    uses the (user-editable) attributes to separate them. Restricted to the same category."""
    w = float(settings.get("redundancy_text_weight", 0.3))
    vec = ensure_fclip(cand)
    hits = [(iid, s) for iid, s in closet_index.search(vec)
            if iid in closet_by_id and iid != cand["id"] and closet_by_id[iid].get("category") == cand.get("category")]
    scored = []
    if hits:
        others = [closet_by_id[iid] for iid, _ in hits]
        T = text_embeddings([redundancy_text(cand)] + [redundancy_text(o) for o in others])
        tsims = T[1:] @ T[0]
        for (iid, isim), tsim, o in zip(hits, tsims, others):
            scored.append((o, (1 - w) * isim + w * float(tsim), isim, float(tsim)))
        scored.sort(key=lambda x: -x[1])
    top = scored[0][1] if scored else 0.0
    if top >= settings["redundancy_duplicate_threshold"]:
        level = "near_duplicate"
    elif top >= settings["redundancy_similar_threshold"]:
        level = "similar"
    else:
        level = "none"
    shown = [x for x in scored[:3] if x[1] >= MATCH_DISPLAY_FLOOR]
    return {"level": level, "top_similarity": round(float(top), 4),
            "matches": [{"item": item_to_api(o), "similarity": round(float(s), 4),
                         "image_similarity": round(float(i), 4), "text_similarity": round(float(t), 4)}
                        for o, s, i, t in shown],
            "_top_item": scored[0][0] if scored else None}


_text_cache: dict[str, "object"] = {}


def text_embeddings(texts: list[str]):
    import numpy as np
    from .vectors import embed_texts
    missing = [t for t in dict.fromkeys(texts) if t not in _text_cache]
    if missing:
        for t, v in zip(missing, embed_texts(missing)):
            _text_cache[t] = v
    return np.stack([_text_cache[t] for t in texts])


# ------------------------------------------------------------------ Stage 5
SUPPORTED = ("top", "bottom", "outerwear", "dress", "shoes")


def templates_for(cat: str, use_shoes: bool) -> list[str]:
    sh = "+shoes" if use_shoes else ""
    return {
        "top": [f"top+bottom{sh}", f"top+bottom+outerwear{sh}"],
        "bottom": [f"top+bottom{sh}", f"top+bottom+outerwear{sh}"],
        "outerwear": [f"top+bottom+outerwear{sh}", f"dress+outerwear{sh}"],
        "dress": ["dress+shoes" if use_shoes else "dress", f"dress+outerwear{sh}"],
        "shoes": ["top+bottom+shoes", "dress+shoes"],
    }[cat]


def generate_outfits(cand: dict, closet: list[dict], settings: dict, redundancy_level: str) -> tuple[list[dict], list[str]]:
    """Greedy, slot-by-slot outfit search (never two items of the same slot).

    Every candidate outfit is scored by the compat model; raw scores are calibrated per outfit size
    (scoring.calibrate) and an outfit is kept iff calibrated >= compat_threshold ("match strictness").
    Shoes are an optional LAST layer: if the closet has shoes, each base look (top+bottom, +outerwear,
    dress, dress+outerwear) is scored with every shoe and judged by its best shoe -> one outfit per look,
    so shoes never multiply the count. For a shoes candidate, a look counts only if the new shoes pass
    AND beat every shoe you already own for that look (i.e. it's a genuinely new/better outfit).
    """
    s = float(settings["compat_threshold"])
    cat = cand["category"]
    pool = [c for c in closet if c["id"] != cand["id"]]
    if settings.get("match_gender_presentation", True):
        pool = [c for c in pool if gender_ok(cand, c)]
    by_cat: dict[str, list[dict]] = {}
    for c in pool:
        by_cat.setdefault(c["category"], []).append(c)
    tops, bottoms, outer, dresses, shoes = (by_cat.get(k, []) for k in ("top", "bottom", "outerwear", "dress", "shoes"))
    use_shoes = bool(shoes) and cat != "shoes" and settings.get("use_shoes_layer", True)
    templates = templates_for(cat, use_shoes)
    needed = [cand] + tops + bottoms + outer + dresses + shoes
    emb = ensure_compat_embeddings(needed)
    items_by_id = {i["id"]: i for i in needed}
    shoe_ids = [x["id"] for x in shoes]
    g_ok = (lambda a, b: gender_ok(items_by_id[a], items_by_id[b])) if settings.get("match_gender_presentation", True) \
        else (lambda a, b: True)

    def score(combos: list[list[str]]) -> list[tuple[float, float]]:
        raws = score_outfits_cached(combos, emb) if combos else []
        return [(r, calibrate(r, len(c))) for r, c in zip(raws, combos)]

    def looks(bases: list[list[str]], with_shoes: bool) -> list[dict]:
        """Score each base look (optionally completed with its best shoe). Returns all, with pass flag."""
        out = []
        if with_shoes:
            combos, owner = [], []
            for bi, b in enumerate(bases):
                for sh in shoe_ids:
                    if all(g_ok(sh, x) for x in b):
                        combos.append(b + [sh]); owner.append(bi)
            best: dict[int, tuple] = {}
            for (raw, cal), c, bi in zip(score(combos), combos, owner):
                if bi not in best or cal > best[bi][2]:
                    best[bi] = (c, raw, cal)
            for bi, b in enumerate(bases):
                if bi in best:
                    c, raw, cal = best[bi]
                    out.append({"base": b, "item_ids": c, "raw": raw, "score": cal, "ok": cal >= s})
        else:
            for b, (raw, cal) in zip(bases, score(bases)):
                out.append({"base": b, "item_ids": list(b), "raw": raw, "score": cal, "ok": cal >= s})
        return out

    outfits: list[dict] = []

    def add(template: str, results: list[dict]):
        for r in results:
            if r["ok"]:
                outfits.append({"template": template, "item_ids": r["item_ids"], "score": r["score"],
                                "raw_score": r["raw"]})
        return [r for r in results if r["ok"]]

    if cat in ("top", "bottom"):
        partners = bottoms if cat == "top" else tops
        base = add(templates[0], looks([[cand["id"], p["id"]] for p in partners], use_shoes))
        add(templates[1], looks([r["base"] + [o["id"]] for r in base for o in outer], use_shoes))
    elif cat == "outerwear":
        pairs = [[t["id"], b["id"]] for t in tops for b in bottoms if g_ok(t["id"], b["id"])]
        good = sorted([r for r in looks(pairs, use_shoes) if r["ok"]], key=lambda r: -r["score"])
        good = good[: int(settings.get("max_pairs_for_layering", 40))]
        add(templates[0], looks([r["base"] + [cand["id"]] for r in good], use_shoes))
        add(templates[1], looks([[d["id"], cand["id"]] for d in dresses], use_shoes))
    elif cat == "dress":
        if use_shoes:
            add(templates[0], looks([[cand["id"]]], True))
        elif redundancy_level != "near_duplicate":  # no shoes: the dress alone is one (neutral-score) outfit
            outfits.append({"template": "dress", "item_ids": [cand["id"]], "score": 0.5, "raw_score": None})
        add(templates[1], looks([[cand["id"], o["id"]] for o in outer], use_shoes))
    elif cat == "shoes":
        for template, bases in (
                (templates[0], [[t["id"], b["id"]] for t in tops for b in bottoms if g_ok(t["id"], b["id"])]),
                (templates[1], [[d["id"]] for d in dresses])):
            bases = [b for b in bases if all(g_ok(cand["id"], x) for x in b)]
            new = score([b + [cand["id"]] for b in bases])
            existing = {tuple(r["base"]): r["score"] for r in looks(bases, True)} if shoe_ids else {}
            for b, (raw, cal) in zip(bases, new):
                if cal >= s and cal > existing.get(tuple(b), -1.0):
                    outfits.append({"template": template, "item_ids": b + [cand["id"]], "score": cal,
                                    "raw_score": raw})

    for o in outfits:
        o["item_ids"].sort(key=lambda i: DISPLAY_ORDER.get(items_by_id[i]["category"], 9))
    outfits.sort(key=lambda o: (templates.index(o["template"]), -o["score"]))
    return outfits, templates


# ------------------------------------------------------------------ Stage 6
def spent_this_month() -> float:
    now = datetime.now(timezone.utc)
    total = 0.0
    for it in db.list_items(status="closet"):
        a = it["attributes"]
        if a.get("purchased_at") and a.get("purchase_price") is not None:
            try:
                ts = datetime.fromisoformat(a["purchased_at"])
                if ts.year == now.year and ts.month == now.month:
                    total += float(a["purchase_price"])
            except Exception:
                pass
    return total


def compute_value_and_verdict(*, n_outfits: int, weighted_outfits: float, counts: dict[str, int], price: float | None,
                              redundancy_level: str, top_match: dict | None, top_similarity: float,
                              settings: dict, spent: float) -> tuple[dict, dict]:
    """THE verdict formula (pure math, no LLM). All inputs explicit so it is unit-testable.

    N  = number of generated outfits (each already passed the compat threshold)
    W  = weighted_outfits = sum of their compatibility scores (0..N)
    cost_per_outfit = price / max(N, 1)
    r  = (W * max_cost_per_outfit) / price      (r = 1  <=> each *weighted* outfit costs exactly your max)
         (no price: r = W / min_new_outfits)
    redundancy_factor = 1.0 none | 0.7 similar | 0.2 near duplicate
    budget_factor     = 0.5 if price > remaining monthly budget else 1.0
    value_score = round(100 * redundancy_factor * budget_factor * r / (1 + r))   (0..100; 50 = right at your limit)

    BUY iff N >= min_new_outfits AND not near duplicate AND (no price OR cost_per_outfit <= max_cost_per_outfit)
            AND (no price OR no budget OR price <= remaining budget). Otherwise SKIP.
    """
    min_n = int(settings["min_new_outfits"])
    max_cpo = float(settings["max_cost_per_outfit"])
    budget = settings.get("monthly_budget")
    remaining = (float(budget) - spent) if budget not in (None, "") else None

    cpo = (price / max(n_outfits, 1)) if price is not None else None
    if price is not None and price > 0:
        r = weighted_outfits * max_cpo / price
    elif price is not None:  # free item
        r = float("inf") if weighted_outfits > 0 else 0.0
    else:
        r = weighted_outfits / max(min_n, 1)
    rf = REDUNDANCY_FACTOR[redundancy_level]
    over_budget = price is not None and remaining is not None and price > remaining
    bf = 0.5 if over_budget else 1.0
    frac = 1.0 if math.isinf(r) else r / (1 + r)
    value_score = int(round(100 * rf * bf * frac))

    enough = n_outfits >= min_n
    dup = redundancy_level == "near_duplicate"
    cheap_enough = cpo is None or cpo <= max_cpo
    decision = "BUY" if (enough and not dup and cheap_enough and not over_budget) else "SKIP"

    reasons = []
    if n_outfits == 0:
        reasons.append("Doesn't create any new outfits with your current wardrobe")
    else:
        breakdown = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        s = "s" if n_outfits != 1 else ""
        if enough and redundancy_level == "near_duplicate":
            reasons.append(f"Pairs into {n_outfits} outfit{s}, but they'd mostly repeat looks you already have")
        elif enough:
            reasons.append(f"Creates {n_outfits} new outfit{s} with your wardrobe ({breakdown})")
        else:
            reasons.append(f"Only creates {n_outfits} new outfit{s} — you want at least {min_n}")
    name = item_name(top_match) if top_match else None
    if dup:
        reasons.append(f"Very similar to your {name} ({top_similarity:.2f} match)")
    elif redundancy_level == "similar":
        reasons.append(f"Similar to your {name} ({top_similarity:.2f} match), but not a duplicate")
    elif len(reasons) < 3:
        reasons.append("Nothing like it in your closet yet")
    if cpo is not None:
        cmp = "within" if cheap_enough else "above"
        reasons.append(f"${cpo:.2f} per new outfit ({cmp} your ${max_cpo:.0f} limit)")
    else:
        reasons.append("Add a price to see the cost per outfit")
    if over_budget:
        reasons.append(f"Costs more than the ${max(remaining, 0):.0f} left in your monthly budget")
    reasons = reasons[:4]

    value = {"price": price, "cost_per_outfit": round(cpo, 2) if cpo is not None else None,
             "weighted_outfits": round(weighted_outfits, 3), "value_score": value_score,
             "redundancy_factor": rf, "budget_remaining": round(remaining, 2) if remaining is not None else None,
             "currency": "USD"}
    return value, {"decision": decision, "reasons": reasons}


# ------------------------------------------------------------------ Stage 7
def evaluate(item_id: str, price: float | None = None) -> dict:
    cand = db.get_item(item_id)
    if cand is None:
        raise KeyError(item_id)
    settings = db.get_settings()
    attrs = dict(cand["attributes"])
    if price is not None:
        attrs["price"] = float(price)
        attrs.setdefault("currency", "USD")
        attrs["currency"] = attrs.get("currency") or "USD"
        attrs["price_source"] = "user"
    if cand["status"] == "detected":
        db.update_item(item_id, status="candidate", attributes=attrs)
    elif price is not None:
        db.update_item(item_id, attributes=attrs)
    cand = db.get_item(item_id)
    price = cand["attributes"].get("price")
    price = float(price) if price not in (None, "") else None
    cat = cand.get("category")

    closet = [c for c in db.list_items(status="closet") if c["id"] != item_id]  # Stage 3
    closet_by_id = {c["id"]: c for c in closet}
    red = redundancy(cand, closet_by_id, settings)                          # Stage 4
    top_item = red.pop("_top_item")

    base = {"item": item_to_api(cand), "redundancy": red, "settings": settings, "scorer": scorer_kind()}
    if cat not in SUPPORTED:
        msg = (f"Outfit matching for {cat or 'this item'} isn't supported yet — FitCheck currently evaluates "
               f"tops, bottoms, outerwear, dresses and shoes.")
        res = {**base, "supported": False, "message": msg, "template_names": [], "outfits": [],
               "outfit_count_by_template": {}, "total_new_outfits": 0,
               "value": {"price": price, "cost_per_outfit": None, "weighted_outfits": 0.0, "value_score": None},
               "verdict": {"decision": "UNSUPPORTED", "reasons": [msg]}}
        res["evaluation_id"] = db.save_evaluation(item_id, price, "UNSUPPORTED", res, [])
        return res

    outfits, templates = generate_outfits(cand, closet, settings, red["level"])  # Stage 5
    counts = {tn: 0 for tn in templates}
    for o in outfits:
        counts[o["template"]] += 1
    n = len(outfits)
    w = float(sum(o["score"] for o in outfits))
    value, verdict = compute_value_and_verdict(                           # Stage 6
        n_outfits=n, weighted_outfits=w, counts=counts, price=price, redundancy_level=red["level"],
        top_match=top_item, top_similarity=red["top_similarity"], settings=settings, spent=spent_this_month())

    all_items = {**closet_by_id, cand["id"]: cand}
    out_list, per_t = [], {}
    for o in outfits:
        per_t[o["template"]] = per_t.get(o["template"], 0) + 1
        if per_t[o["template"]] <= MAX_OUTFITS_RETURNED_PER_TEMPLATE:
            out_list.append({"template": o["template"], "score": round(o["score"], 4),
                             "raw_score": round(o["raw_score"], 4) if o["raw_score"] is not None else None,
                             "n_items": len(o["item_ids"]),
                             "items": [item_to_api(all_items[i]) for i in o["item_ids"]]})
    res = {**base, "supported": True, "message": None, "template_names": templates, "outfits": out_list,
           "calibration": {"strictness": settings["compat_threshold"],
                           "raw_cutoffs_by_size": calibration_table(settings["compat_threshold"])},
           "outfits_truncated": len(out_list) < n, "outfit_count_by_template": counts, "total_new_outfits": n,
           "value": value, "verdict": verdict}
    res["evaluation_id"] = db.save_evaluation(item_id, price, verdict["decision"], res, outfits)
    return res
