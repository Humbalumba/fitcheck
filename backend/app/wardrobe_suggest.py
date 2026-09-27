""""Worth a look" on the Should I buy? page: 3-5 real products that fill gaps in the CURRENT closet.

Reuses the live-shopping machinery of app.suggest (no new scraping / matching code):
 1. closet_summary() of the closet -> ONE grounded Gemini call (Google Search) asking which kinds of items would fill
    gaps / unlock the most new outfits, returning real products (same JSON format + one short "why" per product).
    If grounding isn't available for the key it falls back to the same Gemini plan + live store search
    (suggest.shop_search) the post-verdict suggestions use.
 2. suggest.fetch_products (link check + product photo) -> suggest.create_suggestion_item (cutout, stored as an item
    with status 'suggestion', never in the closet) -> fashion-clip redundancy vs the closet (near-duplicates dropped)
    -> OutfitTransformer outfit generation (evaluate.generate_outfits) + the verdict math (evaluate.verdict_for).
 3. Rank by new outfits unlocked, then value score, one per category first (variety), max 2 per category; return up to
    5, fewer rather than padding. Nothing is hand-matched: every number comes from the pipeline.
Cached per closet contents (hash of item ids + the attributes/settings that change outfit scoring): in memory and in
SQLite (table wardrobe_suggestions), so revisiting the page is instant and any closet change recomputes.
Computed in a background thread; GET /api/suggestions/wardrobe returns status pending | ready | error immediately
(the page polls), so a slow first search never blocks the page or hits a tunnel/proxy timeout.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import config, db, gemini, suggest
from .accessories import is_accessory_item
from .evaluate import SUPPORTED, generate_outfits, item_name, redundancy, verdict_for
from .pipeline import item_to_api

log = logging.getLogger("fitcheck.wardrobe_suggest")

MAX_RETURNED = 5
MIN_WANTED = 3
ASK_N = 9                 # products asked from Gemini (some drop out: no photo, broken link, duplicate, no outfits)
MAX_PER_CATEGORY = 2
EID_PREFIX = "wardrobe_"  # suggestion_for tag of the items this module creates
ERROR_RETRY_S = 90        # a failed search is retried by the next request after this long
TITLE = "Worth a look"
SUBTITLE = "Picked to fill gaps in My Closet"

_mem: dict[str, dict] = {}          # closet key -> ready result
_errors: dict[str, dict] = {}       # closet key -> {"reason", "at"}
_jobs: dict[str, dict] = {}         # closet key -> {"thread", "started"}
_guard = threading.Lock()

CATEGORY_WORDS = {"top": "tops", "bottom": "pants, jeans or shorts", "outerwear": "jackets or coats",
                  "shoes": "shoes", "dress": "dresses"}
GAP_PHRASES = {"top": "You don't have any tops yet", "bottom": "You don't have any pants or shorts yet",
               "outerwear": "You don't have a jacket yet", "shoes": "You don't have any shoes in My Closet yet",
               "dress": "You don't have a dress yet"}


# ------------------------------------------------------------------ cache key + storage
def closet_key(closet: list[dict], settings: dict) -> str:
    rows = sorted([it["id"], it.get("category"), (it.get("attributes") or {}).get("subcategory"),
                   (it.get("attributes") or {}).get("primary_color"),
                   (it.get("attributes") or {}).get("gender_presentation")] for it in closet)
    s = {k: settings.get(k) for k in ("compat_threshold", "redundancy_similar_threshold",
                                        "redundancy_duplicate_threshold", "redundancy_text_weight",
                                        "match_gender_presentation", "use_shoes_layer", "style_goal", "occasions")}
    return hashlib.sha1(json.dumps([rows, s], sort_keys=True, default=str).encode()).hexdigest()[:16]


_DDL = ("CREATE TABLE IF NOT EXISTS wardrobe_suggestions (closet_key TEXT PRIMARY KEY, results TEXT NOT NULL, "
        "created_at TEXT)")


def load_cached(key: str) -> dict | None:
    if key in _mem:
        return _mem[key]
    try:
        with db.get_conn() as c:
            c.execute(_DDL)
            row = c.execute("SELECT results FROM wardrobe_suggestions WHERE closet_key=?", (key,)).fetchone()
    except Exception:
        log.exception("wardrobe cache read failed")
        return None
    if not row:
        return None
    res = json.loads(row["results"])
    _mem[key] = res
    return res


def save_cached(key: str, res: dict) -> None:
    _mem.clear()  # only the current closet matters
    _mem[key] = res
    try:
        with db.get_conn() as c:
            c.execute(_DDL)
            c.execute("DELETE FROM wardrobe_suggestions WHERE closet_key<>?", (key,))
            c.execute("INSERT OR REPLACE INTO wardrobe_suggestions(closet_key, results, created_at) VALUES (?,?,?)",
                      (key, json.dumps(res), db.now_iso()))
    except Exception:
        log.exception("wardrobe cache write failed")


def cleanup_old_items(keep_eid: str) -> None:
    """Drop suggestion items from previous closet versions (unless an evaluation / outfit still references them)."""
    try:
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT id, crop_path, cutout_path, white_path FROM items WHERE status='suggestion' "
                "AND json_extract(attributes, '$.suggestion_for') LIKE ? "
                "AND json_extract(attributes, '$.suggestion_for')<>? "
                "AND id NOT IN (SELECT candidate_item_id FROM evaluations WHERE candidate_item_id IS NOT NULL) "
                "AND id NOT IN (SELECT item_id FROM outfit_items)", (EID_PREFIX + "%", keep_eid)).fetchall()
        for r in rows:
            suggest.delete_suggestion_items_by_id(dict(r))
    except Exception:
        log.exception("wardrobe cleanup failed")


# ------------------------------------------------------------------ closet -> prompt
def closet_gender(closet: list[dict]) -> str:
    g = collections.Counter((i.get("attributes") or {}).get("gender_presentation") or "unisex" for i in closet)
    if g["mens"] and not g["womens"]:
        return "mens"
    if g["womens"] and not g["mens"]:
        return "womens"
    return "unisex"


def closet_styles(closet: list[dict], n: int = 6) -> list[str]:
    c = collections.Counter(t.lower() for i in closet for t in ((i.get("attributes") or {}).get("style_tags") or []))
    return [t for t, _ in c.most_common(n)]


def closet_formality(closet: list[dict]) -> float | None:
    f = [float((i.get("attributes") or {}).get("formality")) for i in closet
         if isinstance((i.get("attributes") or {}).get("formality"), (int, float))]
    return round(sum(f) / len(f), 1) if f else None


def missing_categories(summary: dict, gender: str) -> list[str]:
    cats = ["top", "bottom", "outerwear", "shoes"] + ([] if gender == "mens" else ["dress"])
    return [c for c in cats if c not in summary["categories"]]


def build_prompt(closet: list[dict], summary: dict, gender: str, grounded: bool = True) -> str:
    missing = missing_categories(summary, gender)
    form = closet_formality(closet)
    items = "; ".join(f"{item_name(i)} ({', '.join(((i.get('attributes') or {}).get('style_tags') or [])[:3])})"
                      for i in closet[:30])
    lines = [
        ("You are a personal shopper with Google Search. Find REAL products that are currently sold online in the US."
         if grounded else "You are a personal shopper planning an online shopping search."),
        f"Shopper's whole wardrobe ({summary['size']} items, {suggest._gender_words(gender)}): "
        f"{suggest.summary_text(summary)}.",
        f"Items: {items}.",
        f"Their style: {', '.join(closet_styles(closet)) or 'casual'}"
        + (f"; average formality {form}/5" if form else "") + ".",
        ("Categories they own NOTHING in: " + ", ".join(missing) + "." if missing else
         "They own something in every category."),
        f"TASK: pick the {ASK_N} {suggest._gender_words(gender)} pieces that would fill the biggest gaps in this "
        "wardrobe and unlock the MOST new outfits with what they already own. Spread them across categories "
        "(top, bottom, outerwear, shoes" + (", dress" if gender != "mens" else "") + "): at least one of each "
        "missing category, at most 3 in any one category. Don't repeat what they already own (no more "
        "black/white t-shirts if they have several); choose versatile colors that work with their pieces. "
        "Keep them affordable (mostly $20-$120).",
        "Clothes and shoes only: NEVER suggest accessories (bags, jewellery, watches, hats, caps, belts, scarves, "
        "sunglasses, gloves, ties, socks).",
    ]
    if grounded:
        lines += [
            "RULES: each product must be one specific, currently listed product on a retailer or brand website "
            "(e.g. Uniqlo, Gap, Old Navy, J.Crew, Madewell, Everlane, H&M, Zara, Abercrombie, Levi's, Nordstrom, "
            "Target, Macy's, ASOS, Nike, Adidas, Vans, Converse). Use the product page URL exactly as found in your "
            "search results, never an invented or guessed URL. image_url = a direct product image URL only if you "
            "saw one, else null. price = the current USD price as a number.",
            "why = one short plain-English phrase (max 8 words) for the shopper, e.g. \"You don't have a light jacket "
            "yet\" or \"Dresses up your graphic tees\". No numbers.",
            "Return ONLY a JSON array (no prose, no markdown fences). Each element: {\"name\": str, \"brand\": str, "
            "\"retailer\": str, \"price\": number, \"product_url\": str, \"image_url\": str|null, \"category\": one of "
            "top|bottom|outerwear|shoes|dress, \"subcategory\": str, \"color\": one simple color name, \"material\": "
            "str, \"pattern\": str, \"formality\": 1-5, \"style_tags\": [str], \"gender_presentation\": "
            "mens|womens|unisex, \"why\": str}.",
        ]
    else:
        lines.append(
            f"Instead of products, return {ASK_N} search plans (one per product to look for), each with a short store "
            "search query and the attributes the product should have. Queries run on the search boxes of mid-priced "
            "US fashion retailers, so use plain product-title words (no brand names). 'why' = a short plain-English "
            "phrase for the shopper, e.g. \"You don't have a light jacket yet\" (no numbers).")
    return "\n".join(lines)


def _whys(text: str) -> dict[str, str]:
    """name -> 'why' from the grounded JSON (parse_products keeps only the standard product fields)."""
    out = {}
    for obj, _, _ in suggest._json_candidates(text or ""):
        items = obj if isinstance(obj, list) else (obj.get("products") if isinstance(obj, dict) else None)
        for it in items or []:
            if isinstance(it, dict) and it.get("name") and it.get("why"):
                out[suggest._CITE_RE.sub("", str(it["name"])).strip()] = str(it["why"]).strip()
        if out:
            break
    return out


def find_products(eid: str, closet: list[dict], summary: dict, gender: str) -> tuple[list[dict], list[dict], dict]:
    """-> (products, rejected, meta). One Gemini request (grounded search, else plan + live store search)."""
    fx = os.environ.get("FITCHECK_SUGGEST_FIXTURE_DIR")
    if fx and (Path(fx) / "wardrobe.products.json").exists():  # offline replay (tests)
        d = json.loads((Path(fx) / "wardrobe.products.json").read_text())
        return d["products"], [], {"source": d.get("source", "fixture"), "fixture": True, "latency_s": 0.0}
    if not gemini.is_configured():
        raise suggest.GeminiUnavailable("Gemini isn't configured on this server")
    try:
        if os.environ.get("SUGGEST_GROUNDING", "auto").lower() == "off":
            raise gemini.GroundingUnavailable("grounding disabled (SUGGEST_GROUNDING=off)")
        t0 = time.time()
        prompt = build_prompt(closet, summary, gender, grounded=True)
        g = gemini.generate_grounded(prompt)
        g.update(kind="grounded", latency_s=round(time.time() - t0, 2), prompt=prompt, mode="wardrobe")
        suggest._record(eid, g)
        whys = _whys(g.get("text") or "")
        products = suggest.parse_products(g.get("text") or "")[:ASK_N + 2]
        for p in products:
            p["why"] = whys.get(p["name"])
        return products, [], {"source": "google_search", "model": g.get("model"), "queries": g.get("queries") or [],
                              "latency_s": g["latency_s"], "grounded": g}
    except gemini.GroundingUnavailable as e:
        log.info("wardrobe suggestions: %s -> Gemini-planned store search", e)
    return store_products(eid, closet, summary, gender)


def store_products(eid: str, closet: list[dict], summary: dict, gender: str,
                   focus: list[str] | None = None) -> tuple[list[dict], list[dict], dict]:
    """Gemini plan + live store search (suggest.shop_search on public Shopify storefronts): always has real CDN
    photos. Used when grounding isn't available, and to top up when too few grounded products had a usable photo
    (big retailers often hide product photos behind bot walls)."""
    fx = os.environ.get("FITCHECK_SUGGEST_FIXTURE_DIR")
    if fx and (Path(fx) / "wardrobe.store.json").exists():  # offline replay (tests)
        d = json.loads((Path(fx) / "wardrobe.store.json").read_text())
        return d["products"], [], {"source": "store_search", "fixture": True, "latency_s": 0.0}
    if fx and (Path(fx) / "wardrobe.products.json").exists():
        return [], [], {"source": "store_search", "fixture": True, "latency_s": 0.0}
    t0 = time.time()
    prompt = build_prompt(closet, summary, gender, grounded=False)
    if focus:
        prompt += ("\nFocus on these categories (the shopper already has picks for the others): "
                   + ", ".join(focus) + ".")
    try:
        plan = gemini._generate([prompt], suggest._plan_schema())
    except Exception as e:
        if gemini._is_status(e, 404, 429, 500, 503):
            raise suggest.GeminiUnavailable("Gemini is out of quota or busy right now") from e
        raise
    queries = json.loads(plan.model_dump_json())["queries"]
    suggest._record(eid, {"kind": "plan", "mode": "wardrobe", "prompt": prompt, "queries": queries})
    lat = round(time.time() - t0, 2)
    products, rejected = suggest.shop_search({"queries": queries}, {"attributes": {"gender_presentation": gender}},
                                             "pairings", {})
    for p in products:
        p["why"] = (p.get("planned") or {}).get("why")
    return products, rejected, {"source": "store_search", "model": gemini._model_name, "latency_s": lat,
                                "queries": [q.get("query") for q in queries]}


# ------------------------------------------------------------------ scoring + ranking
def score_item(it: dict, closet: list[dict], settings: dict) -> dict:
    """Same pipeline + verdict math as an uploaded item, against the current closet."""
    red = redundancy(it, {c["id"]: c for c in closet}, settings)
    top = red.pop("_top_item")
    outfits, templates = generate_outfits(it, closet, settings, red["level"])
    pieces = {i for o in outfits for i in o["item_ids"] if i != it["id"]}
    price = (it.get("attributes") or {}).get("price")
    price = float(price) if price not in (None, "") else None
    value, verdict = verdict_for(it, closet, outfits, price, red, top, settings)
    return {"n": len(outfits), "pieces": len(pieces), "templates": templates, "redundancy": red, "top_match": top,
            "value": value, "verdict": verdict,
            "best_outfit": max((o["score"] for o in outfits), default=None)}


def rank(scored: list[tuple[dict, dict, dict]], settings: dict) -> tuple[list, list[dict]]:
    """Drop near-duplicates of owned items and items that form no outfits; rank by new outfits, then value score;
    one per category first (variety), then up to MAX_PER_CATEGORY; at most MAX_RETURNED."""
    keep, rejected = [], []
    for p, it, s in scored:
        why = None
        if s["redundancy"]["level"] == "near_duplicate":
            why = f"near-duplicate of your {item_name(s['top_match'])} ({s['redundancy']['top_similarity']:.2f})"
        elif s["n"] == 0:
            why = "forms no outfits with your closet"
        if why:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": why})
        else:
            keep.append((p, it, s))
    keep.sort(key=lambda x: (x[2]["n"], x[2]["value"].get("value_score") or 0, x[2]["best_outfit"] or 0),
              reverse=True)
    top, per_cat = [], collections.Counter()
    for limit in (1, MAX_PER_CATEGORY):
        for x in keep:
            if len(top) >= MAX_RETURNED:
                break
            if x not in top and per_cat[x[1]["category"]] < limit:
                top.append(x)
                per_cat[x[1]["category"]] += 1
    top.sort(key=lambda x: keep.index(x))
    for x in keep:
        if x not in top:
            rejected.append({"name": x[0]["name"], "retailer": x[0].get("retailer"), "reason": "ranked below the top 5"})
    return top, rejected


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def plain_reason(p: dict, cat: str, summary: dict) -> str:
    """Headline reason in plain words: a real gap in the closet first, else Gemini's short 'why'."""
    if cat not in summary["categories"]:
        return GAP_PHRASES.get(cat, "Fills a gap in your closet")
    why = re.sub(r"\s+", " ", str(p.get("why") or "")).strip().rstrip(".")
    if why and len(why) <= 70 and not re.search(r"\d", why):
        return why[0].upper() + why[1:]
    col = (p.get("color") or "").lower()
    owned = (summary["categories"].get(cat) or {}).get("colors") or []
    if col and col not in owned:
        return f"A new color for your {CATEGORY_WORDS.get(cat, cat)}"
    return "Works with a lot of what you own"


def pairs_line(s: dict) -> str:
    return f"Goes with {_plural(s['pieces'], 'piece')} you own"


def details_line(s: dict) -> str:
    bits = [f"{_plural(s['n'], 'new outfit')} with your closet"]
    cpw = s["value"].get("cost_per_wear")
    if cpw is not None:
        bits.append(f"about ${cpw:.2f} a wear")
    red = s["redundancy"]
    bits.append("nothing like it in your closet" if red["level"] == "none"
                else f"a bit like your {item_name(s['top_match'])}" if s.get("top_match") else "some overlap")
    return " · ".join(bits)


def to_api(p: dict, it: dict, s: dict, summary: dict) -> dict:
    api_it = item_to_api(it)
    red = s["redundancy"]
    return {
        "id": it["id"], "item": api_it, "name": p["name"], "brand": p.get("brand"),
        "retailer": p.get("retailer") or suggest._host(p.get("product_url")), "price": p.get("price"),
        "currency": "USD", "product_url": p.get("product_url"), "link_status": p.get("link_status"),
        "link_ok": p.get("link_ok"), "image_url": api_it["image_url"],
        "photo_url": config.media_url(it.get("crop_path")), "source_image_url": p.get("image_source_url"),
        "category": it["category"], "subcategory": (it.get("attributes") or {}).get("subcategory"),
        "color": (it.get("attributes") or {}).get("primary_color"),
        "reason": plain_reason(p, it["category"], summary), "pairs_line": pairs_line(s),
        "details": details_line(s), "gemini_why": p.get("why"),
        "total_new_outfits": s["n"], "pieces_it_goes_with": s["pieces"],
        "value": {k: s["value"].get(k) for k in ("value_score", "cost_per_wear", "price", "expected_wears")},
        "verdict": {k: s["verdict"].get(k) for k in ("decision", "score", "reasons")},
        "redundancy": {"level": red["level"], "top_similarity": red.get("top_similarity"),
                       "closest": item_name(s["top_match"]) if s.get("top_match") else None},
    }


# ------------------------------------------------------------------ orchestration
_BAD_LANDING = re.compile(r"no-?results|noresult|notfound|not-found|/404|/error|/search[/?]|[?&](q|query|searchTerm)=|"
                          r"/sitemap|/home/?$|^https?://[^/]+/s/", re.I)


def _store_token(name: str | None) -> str:
    t = re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("&", ""))
    return t[:4] if len(t) >= 4 else t


def host_matches_store(url: str | None, p: dict) -> bool:
    """The product page must be on the retailer's / brand's own site: grounding redirects sometimes resolve to a
    YouTube video, a blog or a review site whose og:image is not the product."""
    host = re.sub(r"[^a-z0-9.]", "", suggest._host(url).lower()).replace(".", "")
    toks = [t for t in (_store_token(p.get("retailer")), _store_token(p.get("brand"))) if len(t) >= 2]
    return bool(host) and (not toks or any(t in host for t in toks))


def not_a_product_page(url: str | None) -> bool:
    """The link resolved to a 'no results' / search / error / home page (e.g. oldnavy .../GeneralNoResults.do)."""
    if not url:
        return True
    from urllib.parse import urlparse as _u
    u = _u(url)
    return bool(_BAD_LANDING.search(url)) or u.path in ("", "/")


def shoes_photo_shows_outfit(img) -> bool:
    """A 'product' photo of shoes that is really legs in pants + shoes on white (e.g. a boot under wide trousers):
    the compat model would score the trousers, so it isn't usable. segformer: other clothing covers much more of the
    photo than the shoes (clean shoe shots measure <= ~0.1 of stray 'clothing' pixels)."""
    try:
        shoes = suggest.photo_stats(img, "shoes")["garment"]
        other = max(suggest.photo_stats(img, c)["garment"] for c in ("bottom", "top", "dress"))
    except Exception:
        return False
    return other > 0.15 and other > 2.5 * shoes


def _process(products, grounded, closet, settings, gender, eid, pseudo, rejected, seen, fetcher=None):
    """Filter -> fetch photo + verify link -> cutout item -> score. Returns [(product, item, score)]."""
    valid = []
    for p in products:
        cat = suggest.normalize_category(p)
        pg = p.get("gender_presentation")
        name_key = re.sub(r"[^a-z0-9]", "", (p.get("name") or "").lower())
        if is_accessory_item(p) or cat is None:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": "not clothes or shoes"})
        elif gender in ("mens", "womens") and pg in ("mens", "womens") and pg != gender:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": f"{pg} item"})
        elif p.get("price") is None:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": "no price"})
        elif name_key in seen:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": "listed twice"})
        else:
            seen.add(name_key)
            p["_category"] = cat
            valid.append(p)
    scored = []
    for p in (fetcher or suggest.fetch_products)(valid, grounded):
        if p.get("fetch_error"):
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": p["fetch_error"]})
            continue
        if not_a_product_page(p.get("product_url")) or not host_matches_store(p.get("product_url"), p):
            rejected.append({"name": p["name"], "retailer": p.get("retailer"),
                             "reason": "product link lands on a search / error page"})
            continue
        try:
            if p["_category"] == "shoes" and shoes_photo_shows_outfit(p["image"]):
                raise ValueError("photo shows the outfit (pants) more than the shoes")
            it = suggest.create_suggestion_item(p, p["_category"], pseudo, eid)
            scored.append((p, it, score_item(it, closet, settings)))
        except Exception as e:  # one bad image must not sink the rest
            if not isinstance(e, ValueError):
                log.exception("wardrobe suggestion scoring failed for %s", p["name"])
            rejected.append({"name": p["name"], "retailer": p.get("retailer"),
                             "reason": str(e) if isinstance(e, ValueError) else f"pipeline error: {e}"})
    return scored


def compute(closet: list[dict], settings: dict, key: str, fetcher=None) -> dict:
    t0 = time.time()
    eid = EID_PREFIX + key
    base = {"status": "ready", "closet_key": key, "title": TITLE, "subtitle": SUBTITLE, "suggestions": [],
            "rejected": [], "reason": None}
    if len(closet) == 0:
        return {**base, "reason": "Add a few items to My Closet and we'll suggest what goes with them."}
    summary = suggest.closet_summary(closet)
    gender = closet_gender(closet)
    products, rejected, meta = find_products(eid, closet, summary, gender)
    grounded = meta.pop("grounded", None) or {"text": "", "chunks": [], "supports": []}
    suggest.delete_suggestion_items(eid)
    pseudo = {"attributes": {"gender_presentation": gender, "formality": round(closet_formality(closet) or 2)}}
    seen: set[str] = set()
    t_gem = time.time()
    scored = _process(products, grounded, closet, settings, gender, eid, pseudo, rejected, seen, fetcher)
    t_fetch = time.time()
    top, _ = rank(scored, settings)
    if len(top) < MIN_WANTED + 1 and meta.get("source") == "google_search":
        # too few grounded products had a usable photo: top up from live store search (one more Gemini call)
        have = collections.Counter(it["category"] for _, it, _ in top)
        focus = [c for c in ["bottom", "outerwear", "shoes", "top"] + ([] if gender == "mens" else ["dress"])
                 if have[c] == 0]
        try:
            more, rej2, meta2 = store_products(eid, closet, summary, gender, focus=focus)
            rejected += rej2
            scored += _process(more, {"text": "", "chunks": [], "supports": []}, closet, settings, gender, eid,
                               pseudo, rejected, seen, fetcher)
            meta["source"] = "google_search+store_search"
            meta["queries"] = (meta.get("queries") or []) + (meta2.get("queries") or [])
            meta["latency_s"] = round(meta.get("latency_s", 0.0) + meta2.get("latency_s", 0.0), 2)
        except Exception as e:  # the grounded picks still stand
            log.info("wardrobe top-up failed: %s", e)
    top, rej = rank(scored, settings)
    rejected += rej
    keep = {it["id"] for _, it, _ in top}
    for _, it, _ in scored:
        if it["id"] not in keep:
            suggest.delete_suggestion_items_by_id(it)
    cleanup_old_items(eid)
    return {**base, "suggestions": [to_api(p, it, s, summary) for p, it, s in top], "rejected": rejected,
            "reason": None if top else "We couldn't find products that clearly add to your closet right now.",
            "considered": len(products), "with_photo": len(scored), "source": meta.get("source"),
            "model": meta.get("model"), "search_queries": meta.get("queries") or [], "fixture": bool(meta.get("fixture")),
            "timing_s": {"gemini": meta.get("latency_s", 0.0), "fetch_and_score": round(t_fetch - t_gem, 2),
                         "top_up_and_rank": round(time.time() - t_fetch, 2), "total": round(time.time() - t0, 2)},
            "generated_at": db.now_iso()}


def _current() -> tuple[list[dict], dict, str]:
    closet = [c for c in db.list_items(status="closet") if c.get("category") in SUPPORTED]
    settings = db.get_settings()
    return closet, settings, closet_key(closet, settings)


def _run(key: str, closet: list[dict], settings: dict) -> None:
    try:
        res = compute(closet, settings, key)
        save_cached(key, res)
        _errors.pop(key, None)
        log.info("wardrobe suggestions %s: %d picks in %.1fs", key, len(res["suggestions"]), res.get("timing_s", {}).get("total", 0))
    except suggest.GeminiUnavailable as e:
        _errors[key] = {"reason": "Shopping picks are taking a break right now. Try again in a bit.", "detail": str(e),
                        "at": time.time()}
    except Exception as e:
        log.exception("wardrobe suggestions failed")
        _errors[key] = {"reason": "Couldn't look through stores right now.", "detail": f"{type(e).__name__}: {e}",
                        "at": time.time()}
    finally:
        with _guard:
            _jobs.pop(key, None)


def start(key: str, closet: list[dict], settings: dict) -> dict:
    with _guard:
        job = _jobs.get(key)
        if job is None:
            t = threading.Thread(target=_run, args=(key, closet, settings), daemon=True, name=f"wardrobe-{key}")
            job = _jobs[key] = {"thread": t, "started": time.time()}
            t.start()
    return job


def get(refresh: bool = False, wait_s: float = 0.0) -> dict:
    """Non-blocking by default: ready (cached), pending (search running in the background) or error."""
    closet, settings, key = _current()
    if not refresh:
        cached = load_cached(key)
        if cached is not None:
            return {**cached, "status": "ready", "cached": True}
        err = _errors.get(key)
        if err and time.time() - err["at"] < ERROR_RETRY_S and key not in _jobs:
            return {"status": "error", "closet_key": key, "title": TITLE, "subtitle": SUBTITLE, "suggestions": [],
                    "reason": err["reason"], "detail": err.get("detail")}
    job = start(key, closet, settings)
    if wait_s > 0:
        job["thread"].join(timeout=wait_s)
        if not job["thread"].is_alive():
            return get(refresh=False, wait_s=0)
    return {"status": "pending", "closet_key": key, "title": TITLE, "subtitle": SUBTITLE, "suggestions": [],
            "reason": None, "elapsed_s": round(time.time() - job["started"], 1)}


def warm() -> None:
    """Startup: compute the current closet's picks in the background if they aren't cached yet."""
    if os.environ.get("FITCHECK_WARDROBE_WARM", "1") == "0" or not gemini.is_configured():
        return
    try:
        closet, settings, key = _current()
        if closet and load_cached(key) is None:
            start(key, closet, settings)
    except Exception:
        log.exception("wardrobe warmup failed")
