"""Stages 3-7: closet retrieval, redundancy, outfit candidate generation, value math and verdict."""
from __future__ import annotations

import logging

from . import db
from .pipeline import item_to_api
from .sustainability import score_item
from .scoring import calibrate, calibration_table, ensure_compat_embeddings, score_outfits_cached, scorer_kind
from .vectors import closet_index, ensure_fclip
from .pricing import effective_price
from .verdict import FORMULA_VERSION, closet_context, compute_verdict

log = logging.getLogger("fitcheck.evaluate")

DISPLAY_ORDER = {"top": 0, "dress": 0, "bottom": 1, "outerwear": 2, "shoes": 3, "accessory": 4}
MAX_OUTFITS_RETURNED_PER_TEMPLATE = 60
MATCH_DISPLAY_FLOOR = 0.75  # redundancy matches below this aren't worth showing


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
def max_possible_outfits(cand: dict, closet: list[dict], settings: dict) -> int:
    """How many outfits an item of this category COULD make with this closet if every combination matched: exactly
    the base looks generate_outfits() tries (shoes complete a look, they never multiply it). The denominator of the
    verdict's category-relative versatility (a dress can only ever make 1 + #outerwear looks; a top #bottoms x
    (1 + #outerwear))."""
    cat = cand.get("category")
    pool = [c for c in closet if c["id"] != cand["id"]]
    gender = settings.get("match_gender_presentation", True)
    if gender:
        pool = [c for c in pool if gender_ok(cand, c)]
    by_cat: dict[str, list[dict]] = {}
    for c in pool:
        by_cat.setdefault(c["category"], []).append(c)
    tops, bottoms, outer, dresses = (by_cat.get(k, []) for k in ("top", "bottom", "outerwear", "dress"))

    def pairs() -> int:
        return sum(1 for t in tops for b in bottoms if not gender or gender_ok(t, b))
    if cat in ("top", "bottom"):
        partners = len(bottoms if cat == "top" else tops)
        return partners * (1 + len(outer))
    if cat == "outerwear":
        return min(pairs(), int(settings.get("max_pairs_for_layering", 40))) + len(dresses)
    if cat == "dress":
        return 1 + len(outer)
    if cat == "shoes":
        return pairs() + len(dresses)
    return 0


def context_for(cand: dict, closet: list[dict], settings: dict) -> dict:
    """Closet facts for the verdict; gap-fill counts only include items the candidate could be worn with."""
    same = (lambda it: gender_ok(cand, it)) if settings.get("match_gender_presentation", True) else None
    return closet_context(closet, same)


def verdict_for(cand: dict, closet: list[dict], outfits: list[dict], price: float | None, red: dict,
                top_item: dict | None, settings: dict, price_source: str | None = None,
                price_confidence: str | None = None) -> tuple[dict, dict]:
    """Stage 6 glue: gathers the verdict's inputs (app.verdict.compute_verdict is the pure formula)."""
    return compute_verdict(
        category=cand.get("category"), attributes=cand.get("attributes") or {}, n_outfits=len(outfits),
        max_possible=max_possible_outfits(cand, closet, settings), outfit_scores=[o["score"] for o in outfits],
        price=price, redundancy_level=red["level"], top_similarity=red.get("top_similarity"),
        top_match_name=item_name(top_item) if top_item else None,
        context=context_for(cand, closet, settings),
        dup_threshold=float(settings.get("redundancy_duplicate_threshold", 0.88)),
        similar_threshold=float(settings.get("redundancy_similar_threshold", 0.80)),
        price_source=price_source, price_confidence=price_confidence)


# ------------------------------------------------------------------ sustainability (informational; never changes the verdict)
def sustainability_for(item: dict, n_new_outfits, top_similarity, price, settings: dict) -> dict | None:
    """app.sustainability.score_item with the app's redundancy thresholds. None if the estimator fails."""
    try:
        return score_item(item.get("attributes") or {}, item.get("category"), n_new_outfits, top_similarity, price,
                          dup_threshold=float(settings.get("redundancy_duplicate_threshold", 0.88)),
                          similar_threshold=float(settings.get("redundancy_similar_threshold", 0.80)))
    except Exception:  # pure-Python estimate; must never break an evaluation
        log.exception("sustainability estimate failed for %s", item.get("id"))
        return None


def with_sustainability(res: dict) -> dict:
    """Saved evaluations from before the score existed: compute it on read (pure Python, microseconds), using the
    settings snapshot stored with the evaluation."""
    if "sustainability" in res or not res.get("item"):
        return res
    settings = res.get("settings") or db.get_settings()
    return {**res, "sustainability": sustainability_for(
        res["item"], res.get("total_new_outfits") or 0, (res.get("redundancy") or {}).get("top_similarity"),
        (res.get("value") or {}).get("price"), settings)}


def with_current_verdict(res: dict) -> dict:
    """Evaluations saved before the BUY / CONSIDER / SKIP score (formula v1: BUY/SKIP only): recompute
    the verdict on read from the stored numbers (new-outfit count, stored outfit scores, redundancy, price) and the
    CURRENT closet (max possible outfits, price bar, gap fill). Pure Python, no model calls; never raises."""
    verdict = res.get("verdict") or {}
    if (verdict.get("formula_version") == FORMULA_VERSION or not res.get("item")
            or res.get("supported") is False or verdict.get("decision") == "UNSUPPORTED"):
        return res
    try:
        settings = {**db.get_settings(), **(res.get("settings") or {})}
        item = res["item"]
        cand = db.get_item(item["id"]) or item
        closet = [c for c in db.list_items(status="closet") if c["id"] != item["id"]]
        red = res.get("redundancy") or {}
        matches = red.get("matches") or []
        price = (res.get("value") or {}).get("price")
        pinfo = effective_price(cand.get("attributes") or {}, cand.get("category"), user_price=price)
        value, new = compute_verdict(
            category=cand.get("category"), attributes=cand.get("attributes") or {},
            n_outfits=int(res.get("total_new_outfits") or 0), max_possible=max_possible_outfits(cand, closet, settings),
            outfit_scores=[float(o["score"]) for o in res.get("outfits") or [] if o.get("score") is not None],
            price=pinfo["price"], price_source=pinfo["price_source"], price_confidence=pinfo["price_confidence"],
            redundancy_level=red.get("level") or "none",
            top_similarity=red.get("top_similarity"),
            top_match_name=item_name(matches[0]["item"]) if matches and matches[0].get("item") else None,
            context=context_for(cand, closet, settings),
            dup_threshold=float(settings.get("redundancy_duplicate_threshold", 0.88)),
            similar_threshold=float(settings.get("redundancy_similar_threshold", 0.80)))
        value["estimated_price"] = pinfo["estimated_price"]
        new["recomputed_on_read"] = True
        new["original_decision"] = verdict.get("decision")
        return {**res, "value": value, "verdict": new}
    except Exception:  # an old row must never break the page: show it as stored (known value fields only)
        log.exception("recomputing verdict for saved evaluation failed")
        v = {k: x for k, x in (res.get("value") or {}).items()
             if k in ("price", "currency", "cost_per_outfit", "weighted_outfits", "value_score")}
        return {**res, "value": v}


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
    cat = cand.get("category")
    # a user-entered price always wins, then a price read off a tag, then the estimate (app.pricing)
    pinfo = effective_price(cand["attributes"], cat)
    price = pinfo["price"]
    real = price if pinfo["price_source"] != "estimated" else None

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
               "value": {"price": price, "currency": "USD", "price_source": pinfo["price_source"],
                         "price_confidence": pinfo["price_confidence"], "estimated_price": pinfo["estimated_price"],
                         "cost_per_wear": None, "weighted_outfits": 0.0, "value_score": None},
               "verdict": {"decision": "UNSUPPORTED", "score": None, "reasons": [msg],
                           "formula_version": FORMULA_VERSION}}
        res["sustainability"] = sustainability_for(cand, 0, red["top_similarity"], price, settings)  # accessory: unsupported
        res["evaluation_id"] = db.save_evaluation(item_id, real, "UNSUPPORTED", res, [])
        return res

    outfits, templates = generate_outfits(cand, closet, settings, red["level"])  # Stage 5
    counts = {tn: 0 for tn in templates}
    for o in outfits:
        counts[o["template"]] += 1
    n = len(outfits)
    value, verdict = verdict_for(cand, closet, outfits, price, red, top_item, settings,  # Stage 6
                                 pinfo["price_source"], pinfo["price_confidence"])
    value["estimated_price"] = pinfo["estimated_price"]

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
           "value": value, "verdict": verdict,
           # after n + redundancy are known; informational only (the verdict above is already final)
           "sustainability": sustainability_for(cand, n, red["top_similarity"], price, settings)}
    res["evaluation_id"] = db.save_evaluation(item_id, real, verdict["decision"], res, outfits)
    return res
