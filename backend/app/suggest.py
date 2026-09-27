"""Shopping suggestions after a verdict ("Better picks instead" for SKIP, "Pairs well with this" for BUY).

Flow (one Gemini request per evaluation, cached in the `suggestions` table):
 1. ONE Gemini call with the Google Search grounding tool finds ~6 real products currently sold online
    (SKIP: same type as the candidate in other colors/materials; BUY: other outfit slots that pair with it).
    Grounded calls can't always use JSON mode, so the JSON is parsed out of the text (`parse_products`).
 2. For every product, fetch a photo (image_url, else the product page's og:image / twitter:image / JSON-LD) and
    verify the product link (grounding redirect URLs are resolved). Products without a photo are dropped.
 3. Each product goes through the SAME pipeline as an uploaded item, with NO extra Gemini calls: the attributes
    Gemini returned become the item JSON, white-background product shots are cut out directly, other photos get a
    segformer cutout, fashion-clip embedding -> redundancy vs the closet, OutfitTransformer outfit generation
    (evaluate.generate_outfits) and the verdict math (verdict.compute_verdict via evaluate.verdict_for). Products are stored as
    items with status 'suggestion' (never in the closet listing or the FAISS index) so outfits can show them.
 4. Rank from real numbers and return at most 3 (fewer if fewer qualify):
    SKIP / CONSIDER: not a near-duplicate of the closet (or of the considered item), beats it on outfits or score;
          ranked by own verdict (BUY > CONSIDER > SKIP), then score, then #outfits.
    BUY:  scored against the FUTURE closet (closet + the candidate); only suggestions whose own verdict is BUY and
          that form outfits with the candidate; ranked by #outfits with the candidate, then #outfits, then score.
Fixture mode: FITCHECK_SUGGEST_FIXTURE_DIR=<dir> replays <dir>/<mode>.json instead of calling Gemini
(every live response is also recorded to DATA_DIR/suggest_raw/<evaluation_id>.json).
"""
from __future__ import annotations

import collections
import hashlib
import html as _html
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse

import numpy as np
from PIL import Image

from . import config, db, gemini
from .accessories import is_accessory, is_accessory_item
from .evaluate import (SUPPORTED, generate_outfits, item_name, redundancy, redundancy_text,
                       sustainability_for, text_embeddings, verdict_for, with_current_verdict)
from .pipeline import item_to_api, load_image
from .segment import CATEGORY_CLASSES, _clean, guess_category_from_label, segment_crop, white_bg_cutout
from .vectors import ensure_fclip

log = logging.getLogger("fitcheck.suggest")

MAX_RETURNED = 3
ASK_N = 6
MAX_PER_CATEGORY = 2          # BUY: keep the 3 picks from spanning a single slot when alternatives qualify
OUTFITS_PER_SUGGESTION = 12   # outfits returned per suggestion (for the outfit modal)
FETCH_TIMEOUT_S = float(os.environ.get("SUGGEST_FETCH_TIMEOUT_S", "8"))
MAX_HTML_BYTES = 2_500_000
MAX_IMAGE_BYTES = 12_000_000
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
IMG_ACCEPT = "image/webp,image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5"  # no avif: PIL may not decode it
REDIRECT_HOST = "vertexaisearch.cloud.google.com"
MODE_TITLES = {"alternatives": "Better picks instead", "pairings": "Pairs well with this"}

_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
_locks_guard = threading.Lock()


class SuggestError(RuntimeError):
    pass


GeminiUnavailable = gemini.GeminiUnavailable


# ------------------------------------------------------------------ closet summary + prompt
def closet_summary(closet: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    for it in closet:
        by_cat[it.get("category") or "other"].append(it)
    out = {"size": len(closet), "categories": {}}
    for cat, items in sorted(by_cat.items()):
        colors = collections.Counter(((i.get("attributes") or {}).get("primary_color") or "unknown").lower()
                                     for i in items)
        subs = collections.Counter(((i.get("attributes") or {}).get("subcategory") or cat).lower() for i in items)
        out["categories"][cat] = {"count": len(items), "colors": [c for c, _ in colors.most_common(4)],
                                  "types": [s for s, _ in subs.most_common(3)]}
    g = collections.Counter(((i.get("attributes") or {}).get("gender_presentation") or "unisex") for i in closet)
    out["genders"] = dict(g)
    return out


def summary_text(summary: dict) -> str:
    if not summary["categories"]:
        return "The closet is empty."
    parts = [f"{cat}: {v['count']} ({', '.join(v['types'])}; colors {', '.join(v['colors'])})"
             for cat, v in summary["categories"].items()]
    return "; ".join(parts)


def pairing_slots(cand: dict) -> list[str]:
    """Outfit slots (from the outfit builder's templates) that pair with the candidate, other than its own."""
    cat = cand.get("category")
    g = ((cand.get("attributes") or {}).get("gender_presentation") or "unisex")
    slots = {"top": ["bottom", "outerwear", "shoes"], "bottom": ["top", "outerwear", "shoes"],
             "outerwear": ["top", "bottom", "shoes", "dress"], "dress": ["outerwear", "shoes"],
             "shoes": ["top", "bottom", "dress"]}.get(cat, [])
    if g == "mens":
        slots = [s for s in slots if s != "dress"]
    return slots


def _gender_words(g: str) -> str:
    return {"womens": "women's", "mens": "men's"}.get(g or "", "unisex / any gender")


def build_prompt(cand: dict, mode: str, summary: dict, cand_eval: dict) -> str:
    a = cand.get("attributes") or {}
    cat = cand.get("category")
    g = a.get("gender_presentation") or "unisex"
    price = (cand_eval.get("value") or {}).get("price") or a.get("price")
    desc = (f"{a.get('primary_color') or ''} {a.get('pattern') if a.get('pattern') not in (None, '', 'solid') else ''} "
            f"{a.get('fabric_guess') or ''} {a.get('subcategory') or cat}").split()
    desc = " ".join(desc)
    style = ", ".join(a.get("style_tags") or []) or "n/a"
    lines = [
        "You are a personal shopper with Google Search. Find REAL products that are currently sold online in the US.",
        f"Shopper's wardrobe ({summary['size']} items): {summary_text(summary)}.",
        f"Item they are considering: {desc} ({_gender_words(g)}; category {cat}; subcategory "
        f"{a.get('subcategory') or cat}; color {a.get('primary_color') or 'unknown'}; material "
        f"{a.get('fabric_guess') or 'unknown'}; pattern {a.get('pattern') or 'unknown'}; formality "
        f"{a.get('formality') or '?'}/5; style {style}" + (f"; price ${float(price):.0f}" if price else "") + ").",
    ]
    if mode == "alternatives":
        owned = ", ".join((summary["categories"].get(cat) or {}).get("colors") or []) or "none"
        reasons = "; ".join((cand_eval.get("verdict") or {}).get("reasons") or [])
        dec = ((cand_eval.get("verdict") or {}).get("decision") or "SKIP").upper()
        lines += [
            (f"Our wardrobe analysis says SKIP it: {reasons}." if dec == "SKIP" else
             f"Our wardrobe analysis says it's only worth considering (not a clear buy): {reasons}."),
            f"TASK: find {ASK_N} alternative products of the SAME type ({_gender_words(g)} {a.get('subcategory') or cat}, "
            f"category {cat}) that would be better for this wardrobe: DIFFERENT colors, textures, materials or patterns "
            f"than the considered item ({a.get('primary_color')}, {a.get('fabric_guess') or 'unknown material'}) and than "
            f"the {cat} colors they already own ({owned}), but still easy to wear with their other pieces. "
            "Vary them (no two in the same color).",
        ]
        if price:
            lines.append(f"Keep prices roughly between ${max(5, float(price) * 0.4):.0f} and ${float(price) * 1.6:.0f}.")
    else:
        slots = pairing_slots(cand)
        per = max(1, round(ASK_N / max(1, len(slots))))
        want = ", ".join(f"{per} {s}" for s in slots)
        lines += [
            "Our wardrobe analysis says BUY it.",
            f"TASK: find {ASK_N} {_gender_words(g)} products in OTHER categories ({want}) that would pair well with the "
            f"considered {a.get('subcategory') or cat} AND with pieces already in the wardrobe, filling gaps rather than "
            "repeating what they own. Keep them affordable (mostly under $80).",
        ]
    lines += [
        "Clothes and shoes only: NEVER suggest accessories (bags, jewellery, watches, hats, caps, belts, scarves, "
        "sunglasses, gloves, ties, socks).",
        blocked_stores_prompt(),
        "RULES: each product must be one specific, currently listed product on a retailer or brand website "
        "(e.g. Uniqlo, Gap, Old Navy, Madewell, Everlane, H&M, Zara, Mango, Abercrombie, Levi's, Nordstrom, "
        "Target, Macy's, ASOS). Use the product page URL exactly as found in your search results, never an invented "
        "or guessed URL. image_url = a direct product image URL only if you saw one, else null. price = the current "
        "USD price as a number.",
        "Return ONLY a JSON array (no prose, no markdown fences). Each element: {\"name\": str, \"brand\": str, "
        "\"retailer\": str, \"price\": number, \"product_url\": str, \"image_url\": str|null, \"category\": one of "
        "top|bottom|outerwear|shoes|dress, \"subcategory\": str, \"color\": one simple color name, \"material\": str, "
        "\"pattern\": str, \"formality\": 1-5, \"style_tags\": [str], \"gender_presentation\": mens|womens|unisex}.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ parsing Gemini's text
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")
_CITE_RE = re.compile(r"\s*\[(?:\d+(?:,\s*\d+)*)\]")


def _json_candidates(text: str):
    """Yield (obj, start, end) for every top-level JSON array/object that decodes, in text order."""
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        m = re.compile(r"[\[{]").search(text, i)
        if not m:
            return
        s = m.start()
        for fix in (lambda t: t, lambda t: _TRAILING_COMMA_RE.sub(r"\1", t)):
            try:
                seg = fix(text[s:])
                obj, n = dec.raw_decode(seg)
                yield obj, s, s + n
                i = s + n
                break
            except json.JSONDecodeError:
                continue
        else:
            i = s + 1


def _to_price(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    m = re.search(r"(\d{1,5}(?:[.,]\d{3})*(?:\.\d{1,2})?)", str(v))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")) or None
    except ValueError:
        return None


def _clean_url(u) -> str | None:
    if not u or not isinstance(u, str):
        return None
    u = u.strip().strip("<>").strip()
    return u if re.match(r"^https?://", u) else None


def parse_products(text: str) -> list[dict]:
    """Robustly pull the product list out of a grounded (non-JSON-mode) response: handles markdown fences, prose
    around the JSON, {"products": [...]} wrappers, trailing commas, string prices, and missing fields.
    Each product gets `_span` = (start, end) character offsets of its JSON in the text."""
    text = text or ""
    found: list[tuple[dict, int, int]] = []
    for obj, s, e in _json_candidates(text):
        items = None
        if isinstance(obj, list):
            items = obj
        elif isinstance(obj, dict):
            for k in ("products", "items", "results", "suggestions"):
                if isinstance(obj.get(k), list):
                    items = obj[k]
                    break
            if items is None and ("name" in obj or "product_url" in obj):
                items = [obj]
        if not items:
            continue
        # locate each element's own span (for mapping grounding supports), best effort
        cursor = s
        for it in items:
            if not isinstance(it, dict):
                continue
            key = str(it.get("product_url") or it.get("name") or "")
            pos = text.find(key, cursor, e) if key else -1
            span = (cursor, e) if pos < 0 else (max(s, text.rfind("{", s, pos)), text.find("}", pos, e) + 1 or e)
            cursor = span[1] if pos >= 0 else cursor
            found.append((it, span[0], span[1]))
        if found:
            break
    out = []
    for it, s, e in found:
        name = _CITE_RE.sub("", str(it.get("name") or it.get("title") or "")).strip()
        if not name:
            continue
        p = {
            "name": name,
            "brand": (str(it.get("brand")).strip() if it.get("brand") else None),
            "retailer": (str(it.get("retailer") or it.get("store") or it.get("brand") or "")).strip() or None,
            "price": _to_price(it.get("price")),
            "product_url": _clean_url(it.get("product_url") or it.get("url") or it.get("link")),
            "image_url": _clean_url(it.get("image_url") or it.get("image")),
            "category": (str(it.get("category") or "").strip().lower() or None),
            "subcategory": (str(it.get("subcategory") or it.get("type") or "").strip().lower() or None),
            "color": (str(it.get("color") or it.get("primary_color") or "").strip().lower() or None),
            "material": (str(it.get("material") or it.get("fabric") or "").strip().lower() or None),
            "pattern": (str(it.get("pattern") or "").strip().lower() or None),
            "formality": it.get("formality"),
            "style_tags": [str(x) for x in (it.get("style_tags") or []) if x][:6] if isinstance(it.get("style_tags"), list) else [],
            "gender_presentation": (str(it.get("gender_presentation") or "").strip().lower() or None),
            "_span": (s, e),
        }
        out.append(p)
    return out


_CAT_ALIASES = {"tops": "top", "shirt": "top", "bottoms": "bottom", "pants": "bottom", "trousers": "bottom",
                "jeans": "bottom", "skirt": "bottom", "shoe": "shoes", "footwear": "shoes", "sneakers": "shoes",
                "jacket": "outerwear", "coat": "outerwear", "dresses": "dress", "jumpsuit": "dress"}


def normalize_category(p: dict) -> str | None:
    """Outfit slot of a product, or None. Accessories (bags, jewellery, hats, belts...) are never suggested."""
    if is_accessory_item(p):
        return None
    c = (p.get("category") or "").lower().strip()
    c = _CAT_ALIASES.get(c, c)
    if c in SUPPORTED:
        return c
    g = guess_category_from_label(f"{p.get('subcategory') or ''} {p.get('name') or ''}")
    return g if g in SUPPORTED else None


def _host(u: str | None) -> str:
    try:
        h = urlparse(u or "").hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def grounding_urls_for(p: dict, grounded: dict) -> list[str]:
    """Grounding chunk URIs that support this product's JSON (by text span; UTF-8 byte offsets), then chunks whose
    domain/title matches the retailer. These are pages Google Search actually returned (redirect URLs)."""
    text = grounded.get("text") or ""
    chunks = grounded.get("chunks") or []
    s, e = p.get("_span") or (0, 0)
    bs, be = len(text[:s].encode("utf-8")), len(text[:e].encode("utf-8"))
    idx: list[int] = []
    for sup in grounded.get("supports") or []:
        if sup.get("end", 0) > bs and sup.get("start", 0) < be:
            idx += [i for i in sup.get("chunks") or [] if i not in idx]
    ret = re.sub(r"[^a-z0-9]", "", (p.get("retailer") or "").lower())
    if ret:
        for i, c in enumerate(chunks):
            label = re.sub(r"[^a-z0-9]", "", f"{c.get('domain') or ''}{c.get('title') or ''}".lower())
            if i not in idx and ret[:8] and ret[:8] in label:
                idx.append(i)
    return [chunks[i]["uri"] for i in idx if 0 <= i < len(chunks) and chunks[i].get("uri")]


# ------------------------------------------------------------------ fetching
def _client():
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=5.0),
                        max_redirects=8)


def _get(client, url: str, accept: str, max_bytes: int):
    """GET with a size cap. Returns (status, final_url, content_type, body_bytes) or (None, url, None, b'')."""
    try:
        with client.stream("GET", url, headers={"Accept": accept}) as r:
            ct = (r.headers.get("content-type") or "").lower()
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    break
            return r.status_code, str(r.url), ct, body
    except Exception as e:  # DNS, TLS, timeout, too many redirects
        log.info("fetch failed %s: %s", url[:120], type(e).__name__)
        return None, url, None, b""


def _page_images(html_text: str, base: str) -> list[str]:
    out = []
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*'
                r'content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
                r'"image"\s*:\s*"(https?:[^"]+)"',
                r'"image"\s*:\s*\[\s*"(https?:[^"]+)"',
                r'<link[^>]+rel=["\']image_src["\'][^>]*href=["\']([^"\']+)["\']'):
        for m in re.finditer(pat, html_text, re.I):
            u = _html.unescape(m.group(1)).replace("\\/", "/").strip()
            if u.startswith("//"):
                u = "https:" + u
            u = urljoin(base, u)
            if u not in out:
                out.append(u)
    return out


def _decode_image(ct: str | None, body: bytes) -> Image.Image | None:
    if not body or (ct and not ct.startswith("image/") and "octet-stream" not in ct):
        return None
    try:
        img = load_image(body)
    except Exception:
        return None
    if min(img.size) < 120:
        return None
    return img


def _looks_like_product_page(html_text: str) -> bool:
    return bool(re.search(r'og:type["\'][^>]*content=["\'](?:product|og:product)|"@type"\s*:\s*"Product"|'
                          r'itemprop=["\']price|add to (?:bag|cart)', html_text[:MAX_HTML_BYTES], re.I))


# ------------------------------------------------------------------ link validation (shared by every suggestion)
# Root cause of "a denim jacket linked to a video for shoes": when the product_url Gemini wrote failed, fetch_product
# fell back to grounding chunk URLs (Google redirect links to the pages Gemini *read*, which include YouTube videos,
# blogs and review sites) and accepted the first one that returned 200, then took that page's og:image. Nothing
# checked that the destination was a store or that it showed THIS product. Now every candidate link is resolved to
# its final URL and must (a) not be a video / social / editorial page, (b) not be a search / error page, and (c) show
# the product: page title / og:title / JSON-LD name shares a key word with the product name and names no OTHER
# garment type (a 'denim jacket' card never links to a shoe page). Bot-walled store pages (403) can't be read, so
# there the URL slug itself must name the product. Photos come from the validated page first.
NON_SHOPPING_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv", "tiktok.com", "instagram.com",
    "facebook.com", "fb.com", "pinterest.com", "pin.it", "reddit.com", "x.com", "twitter.com", "threads.net",
    "wikipedia.org", "wikimedia.org", "medium.com", "substack.com", "blogspot.com", "wordpress.com", "tumblr.com",
    "quora.com", "gq.com", "esquire.com", "nytimes.com", "nymag.com", "buzzfeed.com", "businessinsider.com",
    "insider.com", "forbes.com", "cnn.com", "vogue.com", "elle.com", "hypebeast.com", "highsnobiety.com",
    "complex.com", "whowhatwear.com", "refinery29.com", "reviewed.com", "usatoday.com", "theguardian.com",
    "cosmopolitan.com", "menshealth.com", "mensjournal.com", "popsugar.com", "wired.com", "theverge.com",
    "rtings.com", "runrepeat.com", "google.com", "googleusercontent.com", "bing.com", "yahoo.com",
    "apple.com", "spotify.com", "linkedin.com", "snapchat.com", "imdb.com",
)
_NON_SHOP_HOST_RE = re.compile(r"(^|\.)(blog|blogs|news|magazine|mag|editorial|stories|journal)\.", re.I)
_NON_SHOP_PATH_RE = re.compile(r"/(blog|blogs|news|article|articles|stories|story|journal|magazine|editorial|watch|"
                               r"video|videos|shorts|reel|reels|review|reviews|guide|guides|best-[a-z-]+)(/|$|\?)",
                               re.I)
_BAD_LANDING_RE = re.compile(r"no-?results|noresult|notfound|not-found|/404|/error|/search[/?]|/s/|[?&](q|query|"
                             r"searchTerm|keyword)=|/sitemap", re.I)
_STOP = {"the", "and", "for", "with", "mens", "men", "womens", "women", "unisex", "new", "shop", "buy", "online",
         "sale", "size", "fit", "style", "from", "our", "your", "official", "store", "usa", "com", "www", "in", "of"}


def is_non_shopping_url(url: str | None) -> bool:
    """Videos, social media, wikis, blogs / news / review sites, search engines: never a product link."""
    h = _host(url).lower()
    if not h:
        return True
    if any(h == d or h.endswith("." + d) for d in NON_SHOPPING_HOSTS) or _NON_SHOP_HOST_RE.search(h + "."):
        return True
    try:
        path = urlparse(url or "").path or ""
    except ValueError:
        return True
    return bool(_NON_SHOP_PATH_RE.search(path))


# ------------------------------------------------------------------ store blocklist (user: "no more suggestions from jcrew")
# One place to extend: store domain (any subdomain counts, e.g. factory.jcrew.com) -> brand / seller names.
BLOCKED_STORES: dict[str, tuple[str, ...]] = {
    "jcrew.com": ("J.Crew", "J.Crew Factory"),
}


def _store_key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def blocked_host(url: str | None) -> str | None:
    """Blocked store domain this URL belongs to (itself or any subdomain), else None."""
    h = _host(url).lower()
    return next((d for d in BLOCKED_STORES if h == d or h.endswith("." + d)), None) if h else None


def _blocked_name(text: str | None) -> str | None:
    k = _store_key(text)
    if not k:
        return None
    for names in BLOCKED_STORES.values():
        for n in names:
            nk = _store_key(n)
            if k == nk or k.startswith(nk):  # 'J.Crew', 'J. Crew Factory', 'JCrew Mercantile'
                return n
    return None


def _name_mentions_blocked(name: str | None) -> str | None:
    for names in BLOCKED_STORES.values():
        for n in names:
            pat = r"(?<![a-z0-9])" + r"\.?\s*".join(re.escape(ch) for ch in _store_key(n)) + r"(?![a-z0-9])"
            if re.search(pat, (name or "").lower()):
                return n
    return None


def blocked_store(p: dict) -> str | None:
    """Why this product is from a blocked store (URL / image host, brand, retailer, the validated page's brand, or
    the product name itself), else None."""
    for k in ("product_url", "image_url", "image_source_url"):
        d = blocked_host(p.get(k))
        if d:
            return f"blocked store ({d})"
    for v in (p.get("brand"), p.get("retailer"), (p.get("page") or {}).get("brand"),
              ((p.get("item") or {}).get("attributes") or {}).get("brand"),
              ((p.get("item") or {}).get("attributes") or {}).get("retailer")):
        n = _blocked_name(v)
        if n:
            return f"blocked store ({n})"
    n = _name_mentions_blocked(p.get("name")) or _name_mentions_blocked(p.get("listing_name"))
    return f"blocked store ({n})" if n else None


def drop_blocked(products: list[dict], rejected: list[dict] | None = None) -> list[dict]:
    keep = []
    for p in products:
        why = blocked_store(p)
        if why:
            if rejected is not None:
                rejected.append({"name": p.get("name"), "retailer": p.get("retailer"), "reason": why})
        else:
            keep.append(p)
    return keep


def blocked_stores_prompt() -> str:
    names = sorted({n for ns in BLOCKED_STORES.values() for n in ns})
    doms = ", ".join(BLOCKED_STORES)
    return (f"NEVER suggest products from {' or '.join(names)} (the shopper asked for no more suggestions from them): "
            f"no {doms} links (including subdomains) and no products of those brands.")


def is_bad_landing(url: str | None) -> bool:
    """Search / 'no results' / error / home page instead of a product page."""
    if not url:
        return True
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return True
    return bool(_BAD_LANDING_RE.search(url)) or path.strip("/") == ""


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower().replace("'", "")) if len(t) >= 3 and t not in _STOP}


def page_title_text(html_text: str) -> str:
    """<title>, og:title / twitter:title and JSON-LD Product names of a page (what the page says it sells)."""
    h = html_text[:600_000]
    out = []
    m = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
    if m:
        out.append(m.group(1))
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]*content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:title|twitter:title)["\']',
                r'"@type"\s*:\s*"Product"[^{}]{0,400}?"name"\s*:\s*"([^"]{3,200})"',
                r'"name"\s*:\s*"([^"]{3,200})"[^{}]{0,400}?"@type"\s*:\s*"Product"'):
        out += re.findall(pat, h, re.I | re.S)[:3]
    return _html.unescape(" | ".join(x.strip() for x in out if x and x.strip()))[:1000]


def _name_tokens(p: dict) -> set[str]:
    brand = _tokens(f"{p.get('brand') or ''} {p.get('retailer') or ''}")
    return _tokens(p.get("name") or "") - brand


def _type_tokens(p: dict) -> set[str]:
    return _tokens(p.get("subcategory") or "") - {"top", "bottom"}


def text_matches_product(p: dict, text: str) -> tuple[bool, str]:
    """Does this page title / URL slug describe THIS product? Needs a shared key word from the product name or type,
    and the text must not name a different garment category (denim jacket card vs a shoe page)."""
    text_l = (text or "").lower().replace("-", " ").replace("_", " ")
    toks = _tokens(text_l)
    cat = p.get("_category") or normalize_category(p)
    other = guess_category_from_label(text_l)
    if other and cat and other != cat and other in SUPPORTED:
        own = guess_category_from_label(f"{p.get('subcategory') or ''} {p.get('name') or ''}".lower())
        if own != other:
            return False, f"page is about {other}, not {cat}"
    name_hit = _name_tokens(p) & toks
    type_hit = _type_tokens(p) & toks
    if name_hit or type_hit:
        return True, ""
    return False, "page doesn't mention the product"


def check_link(p: dict, url: str, status: int | None, html_text: str) -> tuple[bool, str]:
    """-> (ok, reason). A resolved destination is kept only if it's a store page for this product."""
    if REDIRECT_HOST in _host(url):
        return False, "grounding redirect didn't resolve"
    if is_non_shopping_url(url):
        return False, f"not a store page ({_host(url)})"
    if is_bad_landing(url):
        return False, "search / error page"
    if status == 200 and html_text:
        title = page_title_text(html_text)
        if title:
            ok, why = text_matches_product(p, title)
            if ok:
                return True, ""
            slug_ok, _ = text_matches_product(p, urlparse(url).path)
            return (slug_ok and not guess_category_from_label(title.lower())), why
    # bot wall / no readable HTML: the URL itself has to name the product
    return text_matches_product(p, urlparse(url).path)


# ------------------------------------------------------------------ what the validated product page itself says
def _jsonld_objects(html_text: str):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text[:1_500_000], re.S | re.I):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [d]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack += o
            elif isinstance(o, dict):
                yield o
                for k in ("@graph", "hasVariant", "mainEntity", "itemListElement"):
                    if isinstance(o.get(k), (list, dict)):
                        stack.append(o[k])


def _is_product(o: dict) -> bool:
    t = o.get("@type")
    return t in ("Product", "ProductGroup", "IndividualProduct") or (isinstance(t, list) and "Product" in t)


def _meta(html_text: str, *names: str) -> str | None:
    for n in names:
        for pat in (rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']+)',
                    rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(n)}["\']'):
            m = re.search(pat, html_text[:600_000], re.I)
            if m:
                return _html.unescape(m.group(1)).strip()
    return None


def _img_list(v) -> list[str]:
    out = []
    for x in (v if isinstance(v, list) else [v]):
        u = x.get("url") or x.get("contentUrl") if isinstance(x, dict) else x
        if isinstance(u, str) and u.strip():
            u = u.strip()
            out.append("https:" + u if u.startswith("//") else u)
    return out


def page_product_info(html_text: str) -> dict:
    """Name / brand / price / images / garment-type words from the product page (JSON-LD Product first, then
    og:/product: meta tags). Empty dict when the page isn't readable."""
    if not html_text:
        return {}
    info: dict = {"images": [], "types": []}
    for o in _jsonld_objects(html_text):
        if not _is_product(o):
            continue
        info.setdefault("name", _html.unescape(str(o.get("name") or "")).strip() or None)
        b = o.get("brand")
        b = b.get("name") if isinstance(b, dict) else (b[0].get("name") if isinstance(b, list) and b and isinstance(b[0], dict) else b)
        if isinstance(b, str) and b.strip():
            info.setdefault("brand", b.strip())
        offers = o.get("offers")
        for off in (offers if isinstance(offers, list) else [offers]):
            if isinstance(off, dict) and "price" not in info:
                spec = off.get("priceSpecification")
                pr = _to_price(off.get("price") or off.get("lowPrice") or (spec.get("price") if isinstance(spec, dict) else None))
                cur = (off.get("priceCurrency") or "USD").upper()
                if pr and cur == "USD":
                    info["price"] = pr
        info["images"] += [u for u in _img_list(o.get("image")) if u not in info["images"]]
        for k in ("category", "productType", "additionalType"):
            if isinstance(o.get(k), str):
                info["types"].append(o[k])
        if isinstance(o.get("color"), str) and o["color"].strip():
            info.setdefault("color", o["color"].strip())
    if not info.get("name"):
        info["name"] = _meta(html_text, "og:title", "twitter:title")
    if "price" not in info:
        pr = _to_price(_meta(html_text, "product:price:amount", "og:price:amount", "price"))
        cur = (_meta(html_text, "product:price:currency", "og:price:currency", "priceCurrency") or "USD").upper()
        if pr and cur == "USD":
            info["price"] = pr
    if not info.get("color"):
        info["color"] = _meta(html_text, "product:color", "og:color", "color")
    info["types"] += re.findall(r'"(?:product_type|productType|product_category)"\s*:\s*"([^"]{2,40})"', html_text[:1_500_000])[:3]
    info["name"] = info.get("name") or None
    return info


_NAME_SEP = re.compile(r"\s+[|\u2013\u2014-]\s+|\s*\|\s*")


def clean_product_name(name: str, p: dict) -> str:
    """'Wrangler® Blanket Lined Denim Jacket | COLLECTIONS | Wrangler®' -> 'Blanket Lined Denim Jacket'."""
    name = re.sub(r"[\u00ae\u2122]", "", _html.unescape(name or "")).strip()
    store = {t for t in (_tokens(p.get("retailer") or "") | _tokens(p.get("brand") or "")) if len(t) >= 3}
    parts = [x.strip() for x in _NAME_SEP.split(name) if x.strip()]
    generic = re.compile(r"^(collections?|shop|official site|online|men'?s?|women'?s?|new arrivals|sale|us|usa)$", re.I)
    keep = [x for x in parts if not generic.match(x) and not (_tokens(x) and _tokens(x) <= store | _STOP)]
    out = keep[0] if keep else (parts[0] if parts else name)
    for b in sorted([p.get("brand") or "", p.get("retailer") or ""], key=len, reverse=True):
        if b and out.lower().startswith(b.lower() + " ") and len(out) > len(b) + 3:
            out = out[len(b):].strip(" -:")
    return out[:120]


def url_variant_color(url: str | None) -> str | None:
    """Color of the variant the link selects (?color=olive, ?colour=..., /olive-green/ path bits)."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return None
    for k, v in parse_qsl(u.query):
        if k.lower() in ("color", "colour", "colorname", "color_name", "variantcolor", "dwvar_color") and v:
            c = product_color(v.lower().replace("-", " ").replace("_", " "))
            if c:
                return c
    return None


def image_color(img) -> str | None:
    """Very coarse color of a product-only photo's garment (pixels that aren't background white): only the
    unambiguous cases -- 'black' (very dark), 'white' (very light) -- else None."""
    try:
        a = np.asarray(img.convert("RGB").resize((96, 96)), dtype=np.float32)
    except Exception:
        return None
    px = a.reshape(-1, 3)
    fg = px[(px.min(axis=1) < 235)]
    if len(fg) < 200:
        return None
    lum = (0.299 * fg[:, 0] + 0.587 * fg[:, 1] + 0.114 * fg[:, 2])
    sat = fg.max(axis=1) - fg.min(axis=1)
    med_l, med_s = float(np.median(lum)), float(np.median(sat))
    if med_l < 55 and med_s < 30:
        return "black"
    return None


DARK_COLORS = {"black", "navy", "grey", "brown", "burgundy"}
_VARIANT_EXTRA = {"sail": "cream", "off white": "cream", "off-white": "cream", "summit white": "white"}


def first_color(text: str | None) -> str | None:
    """Main color of a variant name: the FIRST color word ('Sail/Gum Yellow' -> cream, 'Black/White' -> black)."""
    t = (text or "").lower()
    words = {**COLOR_WORDS, **_VARIANT_EXTRA}
    best = None
    for w in sorted(words, key=len, reverse=True):
        m = re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), words[w])
    return best[1] if best else None


def verify_color(p: dict, info: dict, img) -> None:
    """The card's color must be the variant actually shown/linked: page variant color (JSON-LD / meta), then the
    link's ?color=, then a color word in the page's product name. Gemini's color is replaced when the page says
    otherwise, and dropped (unverified) when the photo clearly contradicts it (olive claimed, black jacket shown)."""
    claimed = (p.get("color") or "").lower() or None
    page_c = (first_color(info.get("color")) or url_variant_color(p.get("product_url"))
              or first_color(info.get("name")))
    img_c = image_color(img) if img is not None else None
    if page_c and not (img_c == "black" and page_c not in DARK_COLORS):
        p["color"], p["color_verified"] = page_c, True
    elif img_c == "black" and claimed not in DARK_COLORS:
        p["gemini_color"], p["color"], p["color_verified"] = claimed, "black", True
    elif img_c and claimed == img_c:
        p["color_verified"] = True
    else:
        p["color_verified"] = False
    if claimed and p["color"] != claimed:
        p["gemini_color"] = claimed


_page_cache: dict[str, tuple[float, tuple]] = {}
PAGE_CACHE_TTL_S = 1800


def _get_page_cached(client, url: str):
    """Product-page GET with a 30-min in-memory cache (the same links recur across refreshes / evaluations)."""
    hit = _page_cache.get(url)
    if hit and time.time() - hit[0] < PAGE_CACHE_TTL_S:
        return hit[1]
    res = _get(client, url, PAGE_ACCEPT, MAX_HTML_BYTES)
    if res[0] is not None:
        if len(_page_cache) > 300:
            _page_cache.clear()
        _page_cache[url] = (time.time(), res)
    return res


def fetch_product(p: dict, grounded: dict) -> dict:
    """Verify the product link and fetch a photo. Adds: product_url (resolved), link_status (HTTP code or None),
    link_ok (bool|None: None = bot wall, unverified), image (PIL) + image_source_url, or fetch_error."""
    t0 = time.time()
    p = dict(p)
    with _client() as c:
        tried: list[str] = []
        page_html, page_url, status = "", None, None
        link_rejects: list[str] = []
        candidates = ([p["product_url"]] if p.get("product_url") else []) + grounding_urls_for(p, grounded)
        for u in candidates[:5]:
            if u in tried or (REDIRECT_HOST not in _host(u) and is_non_shopping_url(u)):
                continue
            tried.append(u)
            st, final, ct, body = _get_page_cached(c, u)
            if st is None or st in (404, 410) or st >= 500:
                status = status or st
                continue
            h = body.decode("utf-8", "ignore") if ct and "html" in ct else ""
            ok, why = check_link(p, final, st, h)
            if ok and blocked_host(final):
                ok, why = False, f"blocked store ({blocked_host(final)})"
            if not ok:
                link_rejects.append(f"{_host(final)}: {why}")
                continue
            if page_url is None or (status != 200 and st == 200):
                page_url, status, page_html = final, st, h
            if status == 200:
                break
        if page_url is None:  # never show a link we couldn't match to the product
            p["product_url"], p["link_status"], p["link_ok"], p["image"] = None, status, False, None
            p["fetch_error"] = "no store link matching the product" + (f" ({link_rejects[0]})" if link_rejects else "")
            p["link_rejects"] = link_rejects
            p["fetch_s"] = round(time.time() - t0, 2)
            return p
        p["product_url"] = page_url or p.get("product_url")
        info = page_product_info(page_html)
        p["page"] = {k: v for k, v in info.items() if k != "images"}
        if info.get("name"):  # the card shows what the page sells, not Gemini's wording
            p["listing_name"], p["name"] = p["name"], clean_product_name(info["name"], p)
        if info.get("brand") and not p.get("brand"):
            p["brand"] = info["brand"]
        if info.get("price"):
            gp = p.get("price")
            p["listing_price"], p["price"] = gp, info["price"]
            if gp and not (1 / 1.8 <= info["price"] / gp <= 1.8):
                p["price_mismatch"] = f"price on the page (${info['price']:.0f}) doesn't match the listing (${gp:.0f})"
        p["link_status"] = status
        p["link_ok"] = True if status == 200 else (None if status in (401, 403, 429, 503) else False)
        p["product_page"] = _looks_like_product_page(page_html) if page_html else None
        img = None
        img_urls = [urljoin(page_url or "", u) for u in info.get("images") or []]
        img_urls += [u for u in _page_images(page_html, page_url or "") + ([p["image_url"]] if p.get("image_url") else [])
                     if u not in img_urls]
        img_urls = [u for u in img_urls if not is_non_shopping_url(u) or "cdn" in _host(u)]
        if p.get("shopify_js"):
            cat = p.get("_category") or p.get("category") or "top"
            better = _best_shopify_image(c, p["shopify_js"], cat, featured=p.get("image_url"))
            if better == "none":
                img_urls = []  # we looked at the product's photos: none shows the garment cleanly
                p["photo_kind"] = "none"
            elif better is not None:
                img, p["image_source_url"], p["photo_kind"] = better
                img_urls = []
        # prefer a product-only photo (no person: flat lay / ghost mannequin / white-background shot)
        fallback = None
        cat = p.get("_category") or normalize_category(p) or "top"
        for u in img_urls[:6]:
            st, final, ct, body = _get(c, u, IMG_ACCEPT, MAX_IMAGE_BYTES)
            if st != 200:
                continue
            cand = _decode_image(ct, body)
            if cand is None:
                continue
            try:
                ps = photo_stats(cand, cat)
                product_only = ps["person"] < 0.02 and ps["garment"] >= 0.03
            except Exception:
                product_only = False
            if product_only:
                img, p["image_source_url"], p["photo_kind"] = cand, final, "product_only"
                break
            if fallback is None:
                fallback = (cand, final)
        if img is None and fallback is not None:
            img, p["image_source_url"] = fallback
            p["photo_kind"] = p.get("photo_kind") or "on_model"
    p["image"] = img
    try:
        verify_color(p, info if p.get("product_url") else {}, img)
    except Exception:
        log.exception("color check failed for %s", p.get("name"))
        p["color_verified"] = False
    if img is None:
        p["fetch_error"] = ("no clean product photo (only lifestyle / worn shots)" if p.get("photo_kind") == "none"
                            else "no product photo could be fetched")
    elif p["link_ok"] is False:
        p["fetch_error"] = f"product link broken (HTTP {status})"
    elif p.get("price_mismatch"):
        p["fetch_error"] = p["price_mismatch"]
    if not p.get("fetch_error") and blocked_store(p):
        p["fetch_error"] = blocked_store(p)
    p["fetch_s"] = round(time.time() - t0, 2)
    return p


def photo_stats(img: Image.Image, category: str, max_side: int = 320) -> dict:
    """segformer on a thumbnail: person fraction, garment-of-this-category fraction, white background."""
    from .models_runtime import get_segformer
    import torch
    s = min(1.0, max_side / max(img.size))
    small = img.convert("RGB").resize((max(1, int(img.width * s)), max(1, int(img.height * s)))) if s < 1 else img
    proc, model, mlock = get_segformer()
    with mlock, torch.inference_mode():
        logits = model(**proc(images=small, return_tensors="pt")).logits
        seg = torch.nn.functional.interpolate(logits, size=small.size[::-1], mode="bilinear",
                                              align_corners=False).argmax(1)[0].numpy()
    cat = float(np.isin(seg, list(CATEGORY_CLASSES.get(category, set()))).mean())
    return {"person": float(np.isin(seg, PERSON_CLASSES).mean()), "garment": cat,
            "white_bg": is_white_background(small, frac=0.6)}


def _foreground_frac(img: Image.Image, tol: int = 20) -> float:
    a = np.asarray(img.convert("RGB"))
    return float((a.min(axis=2) < 255 - tol).mean())


def _best_shopify_image(c, js_url: str, category: str, featured: str | None = None, max_images: int = 4):
    """Shopify products usually have several photos. The compat model wants the garment alone, so take the first
    product-only photo (white/light background, no person: flat lay / ghost mannequin); otherwise the on-model photo
    where the garment fills most of the frame (not for shoes: shoes on feet don't cut out cleanly).
    Returns (full-size image, url, kind), "none" if no photo is usable, or None if the photos couldn't be read."""
    st, _, ct, body = _get(c, js_url, "application/json", 3_000_000)
    if st != 200:
        return None
    try:
        urls = [("https:" + u if u.startswith("//") else u) for u in (json.loads(body).get("images") or [])]
    except Exception:
        return None
    if featured:
        f = featured.split("&width=")[0]
        urls = [f] + [u for u in urls if urlparse(u).path != urlparse(f).path]
    thumbs = []
    for u in urls[:max_images]:
        st, _, ct, body = _get(c, u + ("&" if "?" in u else "?") + "width=256", IMG_ACCEPT, 2_000_000)
        if st != 200:
            continue
        try:
            thumbs.append((u, Image.open(io.BytesIO(body)).convert("RGB")))
        except Exception:
            continue
    best = None
    # 1) product-only shots: cheap numpy check first, segformer only to confirm there's no person
    for u, th in thumbs:
        if is_white_background(th, frac=0.6) and _foreground_frac(th) >= 0.05:
            if photo_stats(th, category, max_side=256)["person"] < 0.02:
                best = (u, "product_only")
                break
    # 2) otherwise the on-model photo where the garment is largest (first 2 non-white photos)
    if best is None and category != "shoes":
        top = 0.08
        for u, th in [t for t in thumbs if not is_white_background(t[1], frac=0.6)][:2]:
            g = photo_stats(th, category, max_side=256)["garment"]
            if g >= top:
                best, top = (u, "on_model"), g
    if best is None:
        return "none" if thumbs else None
    full = best[0] + ("&" if "?" in best[0] else "?") + "width=900"
    st, final, ct, body = _get(c, full, IMG_ACCEPT, MAX_IMAGE_BYTES)
    img = _decode_image(ct, body) if st == 200 else None
    return (img, final, best[1]) if img is not None else None


def fetch_products(products: list[dict], grounded: dict) -> list[dict]:
    if not products:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(products))) as ex:
        return list(ex.map(lambda p: fetch_product(p, grounded), products))


# ------------------------------------------------------------------ cutout + item creation (no Gemini)
def is_white_background(img: Image.Image, tol: int = 20, frac: float = 0.85) -> bool:
    a = np.asarray(img.convert("RGB").resize((200, max(20, int(200 * img.height / max(1, img.width))))))
    b = max(2, int(0.03 * min(a.shape[:2])))
    border = np.concatenate([a[:b].reshape(-1, 3), a[-b:].reshape(-1, 3), a[:, :b].reshape(-1, 3),
                             a[:, -b:].reshape(-1, 3)])
    return float((border.min(axis=1) >= 255 - tol).mean()) >= frac


PERSON_CLASSES = [2, 11, 12, 13, 14, 15]  # segformer hair, face, legs, arms -> the product is shown on a model


def product_cutout(img: Image.Image, category: str):
    """(rgba, white_rgb, strategy). Product shots on white / light grey with no model are cut out directly;
    photos on a model or a busy background get a segformer mask of the garment's category (same segment_crop +
    quality gate as uploads), because the compat model expects the garment alone (worn photos saturate it)."""
    from .models_runtime import get_segformer
    import torch
    W, H = img.size
    s = min(1.0, 512 / max(W, H))
    small = img.resize((max(1, int(W * s)), max(1, int(H * s)))) if s < 1 else img
    proc, model, mlock = get_segformer()
    with mlock, torch.inference_mode():
        logits = model(**proc(images=small, return_tensors="pt")).logits
        seg = torch.nn.functional.interpolate(logits, size=small.size[::-1], mode="bilinear",
                                              align_corners=False).argmax(1)[0].numpy()
    on_model = float(np.isin(seg, PERSON_CLASSES).mean()) >= 0.02
    if not on_model and is_white_background(img, frac=0.6):
        rgba, white = white_bg_cutout(img)
        return rgba, white, "white_bg"
    m = np.isin(seg, list(CATEGORY_CLASSES.get(category, set())))
    if m.mean() < 0.02:
        return img.convert("RGBA"), img.convert("RGB"), "fallback_crop"
    if on_model:  # only the garment's own classes (a union would pull in the model's other clothes)
        return _mask_cutout(img, _clean(m), "on_model_category")
    ys, xs = np.where(m)
    x0, x1, y0, y1 = xs.min() / s, (xs.max() + 1) / s, ys.min() / s, (ys.max() + 1) / s
    pw, ph = (x1 - x0) * 0.08 + 4, (y1 - y0) * 0.08 + 4
    box = (int(max(0, x0 - pw)), int(max(0, y0 - ph)), int(min(W, x1 + pw)), int(min(H, y1 + ph)))
    crop = img.crop(box)
    inner = (int(x0 - box[0]), int(y0 - box[1]), int(x1 - box[0]), int(y1 - box[1]))
    rgba, white, info = segment_crop(crop, category, inner=inner)
    return rgba, white, info.get("strategy") or "fallback_crop"


def _mask_cutout(img: Image.Image, mask_small: np.ndarray, strategy: str):
    from PIL import ImageFilter
    alpha = Image.fromarray((mask_small * 255).astype(np.uint8)).resize(img.size, Image.BILINEAR)
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.2))
    rgba = img.convert("RGB").copy()
    rgba.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v > 40 else 0).getbbox()
    if bbox:
        pad = int(0.03 * max(img.size))
        bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(img.width, bbox[2] + pad),
                min(img.height, bbox[3] + pad))
        rgba = rgba.crop(bbox)
    white = Image.new("RGB", rgba.size, (255, 255, 255))
    white.paste(rgba, mask=rgba.split()[3])
    return rgba, white, strategy


def _attrs_from_product(p: dict, cat: str, cand: dict, eid: str, strategy: str) -> dict:
    ca = cand.get("attributes") or {}
    try:
        formality = int(max(1, min(5, int(p.get("formality")))))
    except (TypeError, ValueError):
        formality = int(ca.get("formality") or 2)
    g = p.get("gender_presentation")
    if g not in ("mens", "womens", "unisex"):
        g = ca.get("gender_presentation") or "unisex"
    return {
        "category": cat, "subcategory": p.get("subcategory") or cat, "primary_color": p.get("color") or "unknown",
        "secondary_colors": [], "pattern": p.get("pattern") or "solid", "fabric_guess": p.get("material") or "unknown",
        "formality": formality, "formality_label": gemini.FORMALITY_LABELS[formality], "seasons": [],
        "style_tags": p.get("style_tags") or [], "gender_presentation": g, "brand": p.get("brand"),
        "price": p.get("price"), "currency": "USD", "price_source": "retailer", "description": p["name"],
        "source": "gemini-search", "segmentation": strategy, "suggestion_for": eid,
        "retailer": p.get("retailer"), "product_url": p.get("product_url"),
        "source_image_url": p.get("image_source_url"),
    }


def create_suggestion_item(p: dict, cat: str, cand: dict, eid: str) -> dict:
    img: Image.Image = p["image"]
    rgba, white, strategy = product_cutout(img, cat)
    cover = float((np.asarray(rgba.split()[3]) > 40).sum()) / float(img.width * img.height)
    if strategy == "fallback_crop" or (strategy.startswith("on_model") and (cover < 0.03 or cat == "shoes")):
        raise ValueError("no clean photo of the garment (lifestyle / worn shot)")
    stem = "sg_" + hashlib.sha1(f"{eid}|{p.get('product_url')}|{p['name']}".encode()).hexdigest()[:12]
    d = config.MEDIA_DIR / "suggest"
    d.mkdir(parents=True, exist_ok=True)
    paths = {"crop": d / f"{stem}_orig.jpg", "cutout": d / f"{stem}.png", "white": d / f"{stem}.jpg"}
    img.save(paths["crop"], quality=88)
    rgba.save(paths["cutout"])
    white.save(paths["white"], quality=92)
    attrs = _attrs_from_product(p, cat, cand, eid, strategy)
    if not p.get("color"):  # title/tags name no color (e.g. "| Beech"): fashion-clip zero-shot on the cutout
        from .fallback import COLORS, _best
        from .vectors import embed_images
        attrs["primary_color"] = _best(embed_images([white])[0], "color", COLORS, "a photo of a {} garment")[0]
        attrs["color_source"] = "fashion-clip-zero-shot"
    iid = db.insert_item(status="suggestion", category=cat, attributes=attrs,
                         label=p["name"], crop_path=paths["crop"], cutout_path=paths["cutout"],
                         white_path=paths["white"], source="suggestion")
    return db.get_item(iid)


def delete_suggestion_items(eid: str) -> None:
    with db.get_conn() as c:
        rows = c.execute("SELECT id, crop_path, cutout_path, white_path FROM items WHERE status='suggestion' "
                         "AND json_extract(attributes, '$.suggestion_for')=?", (eid,)).fetchall()
    for r in rows:
        db.delete_item(r["id"])
        for k in ("crop_path", "cutout_path", "white_path"):
            if r[k]:
                Path(r[k]).unlink(missing_ok=True)


# ------------------------------------------------------------------ scoring + ranking (pure pipeline, no LLM)
def similarity_to(a: dict, b: dict, settings: dict) -> float:
    """Same blend as redundancy(): (1-w)*fashion-clip image cosine + w*attribute-text cosine."""
    w = float(settings.get("redundancy_text_weight", 0.3))
    va, vb = ensure_fclip(a), ensure_fclip(b)
    T = text_embeddings([redundancy_text(a), redundancy_text(b)])
    return float((1 - w) * float(va @ vb) + w * float(T[0] @ T[1]))


def score_suggestion(sug: dict, cand: dict, closet: list[dict], mode: str, settings: dict) -> dict:
    """Same pipeline + verdict as an uploaded item. Pairings are scored against the FUTURE closet (closet + the
    candidate, which then also counts toward the price bar and gap fill)."""
    closet_by_id = {c["id"]: c for c in closet}
    red = redundancy(sug, closet_by_id, settings)
    top_item = red.pop("_top_item")
    pool = closet + ([cand] if mode == "pairings" else [])
    outfits, templates = generate_outfits(sug, pool, settings, red["level"])
    counts = {t: 0 for t in templates}
    for o in outfits:
        counts[o["template"]] += 1
    n = len(outfits)
    with_cand = sum(1 for o in outfits if cand["id"] in o["item_ids"])
    price = (sug.get("attributes") or {}).get("price")
    price = float(price) if price not in (None, "") else None
    value, verdict = verdict_for(sug, pool, outfits, price, red, top_item, settings)
    sim_cand = similarity_to(sug, cand, settings) if sug.get("category") == cand.get("category") else None
    return {"outfits": outfits, "templates": templates, "counts": counts, "n": n, "with_candidate": with_cand,
            "redundancy": red, "top_match": top_item, "value": value, "verdict": verdict,
            "similarity_to_candidate": sim_cand,
            "sustainability": sustainability_for(_sus_item(sug, (sug.get("attributes") or {}).get("description")),
                                                 n, red["top_similarity"], price, settings)}


def _sus_item(it: dict, name: str | None) -> dict:
    """For shop products the title is the most specific garment-type signal ("... Boatneck Sweater" whose planned
    subcategory was just "top"), so the sustainability estimate resolves the type from title + subcategory."""
    a = dict(it.get("attributes") or {})
    if name:
        from .sustainability import resolve_garment_type
        typ, how = resolve_garment_type({"subcategory": name}, it.get("category") or a.get("category"))
        if typ and how == "subcategory":  # the title names a garment type -> trust it over the planned subcategory
            a["subcategory"] = name
    return {**it, "attributes": a}


def _sus_score(s: dict) -> int:
    return ((s.get("sustainability") or {}).get("score") or 0)


def _reason(mode: str, s: dict, cand: dict, cand_eval: dict) -> str:
    n = s["n"]
    cpw = s["value"].get("cost_per_wear")
    cpo_txt = f"${cpw:.2f} per wear" if cpw is not None else "no price"
    cname = item_name(cand)
    if mode == "pairings":
        return f"Unlocks {n} outfit{'s' if n != 1 else ''}, {s['with_candidate']} with the {cname}, {cpo_txt}"
    red = s["redundancy"]
    look = ("nothing like it in your closet" if red["level"] == "none"
            else f"only {red['top_similarity']:.2f} similar to your {item_name(s['top_match'])}")
    cn = cand_eval.get("total_new_outfits", 0)
    cv = (cand_eval.get("value") or {}).get("value_score")
    vs = s["value"].get("value_score")
    cmp = (f"{n} outfits vs {cn}" if n > cn else f"score {vs} vs {cv}")
    return f"Unlocks {n} outfit{'s' if n != 1 else ''}, {look}, {cpo_txt} ({cmp} for the {cname})"


def rank(mode: str, scored: list[tuple[dict, dict, dict]], cand: dict, cand_eval: dict, settings: dict
         ) -> tuple[list[tuple[dict, dict, dict]], list[dict]]:
    """scored = [(product, item, score)]. Returns (top <=3, rejected [{name, reason}]). Pure function of the numbers."""
    dup_t = float(settings["redundancy_duplicate_threshold"])
    keep, rejected = [], []
    cn = int(cand_eval.get("total_new_outfits") or 0)
    cv = (cand_eval.get("value") or {}).get("value_score") or 0
    for p, it, s in scored:
        why = None
        if s["redundancy"]["level"] == "near_duplicate":
            why = f"near-duplicate of your {item_name(s['top_match'])} ({s['redundancy']['top_similarity']:.2f})"
        elif s["n"] == 0:
            why = "forms no outfits with your closet"
        elif mode == "alternatives":
            sc = s.get("similarity_to_candidate")
            ccol = ((cand.get("attributes") or {}).get("primary_color") or "").lower()
            if sc is not None and sc >= dup_t:
                why = f"too similar to the item you're considering ({sc:.2f})"
            elif ccol and ((it.get("attributes") or {}).get("primary_color") or "").lower() == ccol:
                why = f"same color ({ccol}) as the item you're considering"
            elif not (s["n"] > cn or (s["value"].get("value_score") or 0) > cv):
                why = f"doesn't beat the considered item ({s['n']} outfits, score {s['value'].get('value_score')})"
        else:
            if s["with_candidate"] == 0:
                why = "no outfits together with the new item"
            elif s["verdict"]["decision"] != "BUY":
                why = (f"its own verdict is {s['verdict']['decision']} ({s['verdict'].get('score')}/100: "
                       + "; ".join(s["verdict"]["reasons"][:2]) + ")")
        if why:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": why})
        else:
            keep.append((p, it, s))
    if mode == "alternatives":
        keep.sort(key=lambda x: (DECISION_RANK.get(x[2]["verdict"]["decision"], 0), x[2]["value"].get("value_score") or 0,
                                 x[2]["n"], _sus_score(x[2])), reverse=True)
        top = keep[:MAX_RETURNED]
    else:
        keep.sort(key=lambda x: (x[2]["with_candidate"], x[2]["n"], x[2]["value"].get("value_score") or 0,
                                 _sus_score(x[2])), reverse=True)  # sustainability = final tiebreaker only
        top, per_cat = [], collections.Counter()
        for x in keep:  # at most 2 per slot while other qualifying slots exist
            if per_cat[x[1]["category"]] < MAX_PER_CATEGORY:
                top.append(x)
                per_cat[x[1]["category"]] += 1
        for x in keep:
            if len(top) >= MAX_RETURNED:
                break
            if x not in top:
                top.append(x)
        top = top[:MAX_RETURNED]
    for x in keep:
        if x not in top:
            rejected.append({"name": x[0]["name"], "retailer": x[0].get("retailer"), "reason": "ranked below the top 3"})
    return top, rejected


def _suggestion_to_api(mode: str, p: dict, it: dict, s: dict, cand: dict, cand_eval: dict, closet: list[dict]) -> dict:
    by_id = {c["id"]: c for c in closet}
    by_id[cand["id"]] = cand
    by_id[it["id"]] = it
    outs = sorted(s["outfits"], key=lambda o: (cand["id"] not in o["item_ids"], -o["score"]))  # with-candidate first
    red = s["redundancy"]
    return {
        "id": it["id"], "item": item_to_api(it), "name": p["name"], "brand": p.get("brand"),
        "retailer": p.get("retailer") or _host(p.get("product_url")), "price": p.get("price"), "currency": "USD",
        "product_url": p.get("product_url"), "link_status": p.get("link_status"), "link_ok": p.get("link_ok"),
        "image_url": item_to_api(it)["image_url"], "photo_url": config.media_url(it.get("crop_path")),
        "source_image_url": p.get("image_source_url"),
        "category": it["category"], "subcategory": (it.get("attributes") or {}).get("subcategory"),
        "color": p.get("color") or (it.get("attributes") or {}).get("primary_color"),
        "color_source": (it.get("attributes") or {}).get("color_source") or "listing",
        "material": p.get("material"),
        "reason": _reason(mode, s, cand, cand_eval),
        "verdict": s["verdict"], "value": s["value"], "total_new_outfits": s["n"],
        "outfits_with_candidate": s["with_candidate"], "outfit_count_by_template": s["counts"],
        "template_names": s["templates"],
        "redundancy": {"level": red["level"], "top_similarity": red["top_similarity"],
                       "closest": item_name(s["top_match"]) if s["top_match"] else None},
        "sustainability": s.get("sustainability"),
        "similarity_to_candidate": (round(s["similarity_to_candidate"], 4)
                                    if s.get("similarity_to_candidate") is not None else None),
        "outfits": [{"template": o["template"], "score": round(o["score"], 4),
                     "raw_score": round(o["raw_score"], 4) if o["raw_score"] is not None else None,
                     "n_items": len(o["item_ids"]), "items": [item_to_api(by_id[i]) for i in o["item_ids"]]}
                    for o in outs[:OUTFITS_PER_SUGGESTION]],
    }


# ------------------------------------------------------------------ orchestration
DECISION_RANK = {"BUY": 2, "CONSIDER": 1, "SKIP": 0}


def mode_for(decision: str) -> str | None:
    """SKIP / CONSIDER -> better alternatives of the same type; BUY -> pairings."""
    return {"SKIP": "alternatives", "CONSIDER": "alternatives", "BUY": "pairings"}.get((decision or "").upper())


def _grounded_response(eid: str, mode: str, prompt: str) -> dict:
    fx = os.environ.get("FITCHECK_SUGGEST_FIXTURE_DIR")
    if fx:
        if (Path(fx) / f"{mode}.json").exists():
            d = json.loads((Path(fx) / f"{mode}.json").read_text())
            d["fixture"] = True
            log.info("suggestions %s: replaying fixture %s/%s.json (no Gemini call)", eid, fx, mode)
            return d
        if (Path(fx) / f"{mode}.plan.json").exists():
            raise gemini.GroundingUnavailable("fixture has a plan, not a grounded response")
    if not gemini.is_configured():
        raise GeminiUnavailable("Gemini isn't configured on this server")
    if os.environ.get("SUGGEST_GROUNDING", "auto").lower() == "off":
        raise gemini.GroundingUnavailable("grounding disabled (SUGGEST_GROUNDING=off)")
    t0 = time.time()
    g = gemini.generate_grounded(prompt)
    g.update(kind="grounded", latency_s=round(time.time() - t0, 2), prompt=prompt, mode=mode)
    _record(eid, g)
    return g


def find_products(eid: str, mode: str, cand: dict, summary: dict, cand_eval: dict) -> tuple[list, list, dict]:
    """-> (products, rejected, meta). Grounded Google Search when the key allows it, else Gemini plan + live store
    search. Exactly one Gemini generate_content request either way (a tier-429 on grounding is rejected upfront
    and remembered for 6 h, so it isn't retried per request)."""
    fx = os.environ.get("FITCHECK_SUGGEST_FIXTURE_DIR")
    if fx and (Path(fx) / f"{mode}.products.json").exists():  # fully offline replay (tests)
        d = json.loads((Path(fx) / f"{mode}.products.json").read_text())
        return d["products"], [], {"source": d.get("source", "fixture"), "model": d.get("model"), "fixture": True,
                                   "queries": d.get("queries") or [], "latency_s": 0.0}
    try:
        g = _grounded_response(eid, mode, build_prompt(cand, mode, summary, cand_eval))
        return parse_products(g.get("text") or "")[:ASK_N + 2], [], {
            "source": "google_search", "model": g.get("model"), "queries": g.get("queries") or [],
            "latency_s": g.get("latency_s", 0.0), "fixture": bool(g.get("fixture")), "grounded": g}
    except gemini.GroundingUnavailable as e:
        log.info("suggestions %s: %s -> Gemini-planned store search", eid, e)
    plan = _plan_queries(eid, mode, cand, summary, cand_eval)
    t0 = time.time()
    products, rejected = shop_search(plan, cand, mode, cand_eval)
    return products, rejected, {"source": "store_search", "model": plan.get("model"),
                                "queries": [q.get("query") for q in plan.get("queries") or []],
                                "latency_s": plan.get("latency_s", 0.0), "search_s": round(time.time() - t0, 2),
                                "fixture": bool(plan.get("fixture")), "stores": [s[1] for s in stores()]}


def compute(eid: str, fetcher=None) -> dict:
    t0 = time.time()
    ev = db.get_evaluation(eid)
    if ev is None:
        raise KeyError(eid)
    res = with_current_verdict(ev["results"])  # old BUY/SKIP rows: same verdict the page shows
    decision = (res.get("verdict") or {}).get("decision")
    mode = mode_for(decision)
    cand = db.get_item(ev["candidate_item_id"])
    base = {"evaluation_id": eid, "mode": mode, "title": MODE_TITLES.get(mode), "candidate_verdict": decision,
            "suggestions": [], "rejected": [], "message": None}
    if mode is None or cand is None or cand.get("category") not in SUPPORTED:
        return {**base, "message": "Suggestions are available for tops, bottoms, outerwear, dresses and shoes."}
    settings = db.get_settings()
    closet = [c for c in db.list_items(status="closet") if c["id"] != cand["id"]]
    summary = closet_summary(closet)
    products, rejected, meta = find_products(eid, mode, cand, summary, res)
    grounded = meta.pop("grounded", None) or {"text": "", "chunks": [], "supports": []}
    t_gemini = time.time()
    products = drop_blocked(products, rejected)
    valid = []
    cg = (cand.get("attributes") or {}).get("gender_presentation")
    for p in products:
        cat = normalize_category(p)
        if is_accessory_item(p):  # clothes and shoes only
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": "accessory"})
        elif mode == "alternatives" and cat != cand["category"]:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": f"not a {cand['category']}"})
        elif mode == "pairings" and (cat not in pairing_slots(cand)):
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": f"category {cat} doesn't pair"})
        elif (cg in ("mens", "womens") and p.get("gender_presentation") in ("mens", "womens")
              and p["gender_presentation"] != cg):
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": f"{p['gender_presentation']} item"})
        elif p.get("price") is None:
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": "no price"})
        else:
            p["_category"] = cat
            valid.append(p)
    fetched = (fetcher or fetch_products)(valid, grounded)
    t_fetch = time.time()
    delete_suggestion_items(eid)  # refresh: drop previous suggestion items of this evaluation
    scored = []
    for p in fetched:
        if p.get("fetch_error"):
            rejected.append({"name": p["name"], "retailer": p.get("retailer"), "reason": p["fetch_error"]})
            continue
        try:
            it = create_suggestion_item(p, p["_category"], cand, eid)
            scored.append((p, it, score_suggestion(it, cand, closet, mode, settings)))
        except Exception as e:  # one bad image must not sink the rest
            if not isinstance(e, ValueError):
                log.exception("suggestion scoring failed for %s", p["name"])
            rejected.append({"name": p["name"], "retailer": p.get("retailer"),
                             "reason": str(e) if isinstance(e, ValueError) else f"pipeline error: {e}"})
    top, rej = rank(mode, scored, cand, res, settings)
    rejected += rej
    keep_ids = {it["id"] for _, it, _ in top}
    for _, it, _ in scored:  # only the returned suggestions stay in the DB
        if it["id"] not in keep_ids:
            delete_suggestion_items_by_id(it)
    out = {**base,
           "suggestions": [_suggestion_to_api(mode, p, it, s, cand, res, closet) for p, it, s in top],
           "rejected": rejected,
           "considered": len(products), "with_photo": len(scored),
           "message": None if top else ("None of the products we found beat this item for your wardrobe."
                                        if mode == "alternatives" else
                                        "None of the products we found cleared your BUY bar with this item."),
           "source": meta.get("source"), "model": meta.get("model"), "search_queries": meta.get("queries") or [],
           "stores": meta.get("stores"), "fixture": bool(meta.get("fixture")),
           "timing_s": {"gemini": meta.get("latency_s", 0.0), "search": meta.get("search_s"),
                        "fetch": round(t_fetch - t_gemini, 2), "pipeline": round(time.time() - t_fetch, 2),
                        "total": round(time.time() - t0, 2)},
           "generated_at": db.now_iso()}
    return out


def delete_suggestion_items_by_id(it: dict) -> None:
    db.delete_item(it["id"])
    for k in ("crop_path", "cutout_path", "white_path"):
        if it.get(k):
            Path(it[k]).unlink(missing_ok=True)


def drop_accessories(result: dict) -> dict:
    """Cached suggestions saved before accessories were removed: never show an accessory product."""
    sugs = result.get("suggestions") or []
    keep = [s for s in sugs if not is_accessory_item({**s, "attributes": (s.get("item") or {}).get("attributes")})
            and not blocked_store(s)]
    return result if len(keep) == len(sugs) else {**result, "suggestions": keep}


def add_sustainability(result: dict) -> dict:
    """Cached suggestions from before the sustainability score: compute it on read from the stored numbers.
    (Also drops any accessory suggestion from old cached results.)"""
    result = drop_accessories(result)
    sugs = result.get("suggestions") or []
    if all("sustainability" in s for s in sugs):
        return result
    settings = db.get_settings()
    out = []
    for s in sugs:
        if "sustainability" not in s:
            it = s.get("item") or {"attributes": {}, "category": s.get("category")}
            s = {**s, "sustainability": sustainability_for(
                _sus_item(it, s.get("name")), s.get("total_new_outfits") or 0, (s.get("redundancy") or {}).get("top_similarity"),
                s.get("price"), settings)}
        out.append(s)
    return {**result, "suggestions": out}


def get_or_create(eid: str, refresh: bool = False) -> dict:
    """Cached per evaluation; concurrent requests for the same evaluation wait for the first (1 Gemini call)."""
    with _locks_guard:
        lock = _locks[eid]
    with lock:
        if not refresh:
            cached = db.get_suggestions(eid)
            if cached is not None:
                return {**add_sustainability(cached), "cached": True}
        res = compute(eid)
        if res.get("mode"):
            db.save_suggestions(eid, res["mode"], res)
        return {**res, "cached": False}


# ------------------------------------------------------------------ free-tier path: Gemini plan + live store search
# Google Search grounding is paid-tier only on Gemini 3.x. Without it, ONE regular (JSON-mode) Gemini call plans
# ~6 targeted product searches (with the attributes each product should have), and the backend runs them live
# against public Shopify storefront Predictive Search (/search/suggest.json, a documented public storefront API)
# of these retailers. Results are real, in-stock products with real product URLs, prices and CDN photos.
DEFAULT_STORES = [  # (domain, display name, genders: w/m)
    ("everlane.com", "Everlane", "wm"), ("petalandpup.com", "Petal & Pup", "w"),
    ("us.princesspolly.com", "Princess Polly", "w"), ("goodamerican.com", "Good American", "w"),
    ("cupshe.com", "Cupshe", "w"), ("marinelayer.com", "Marine Layer", "wm"),
    ("colorfulstandard.com", "Colorful Standard", "wm"), ("oakandfort.com", "Oak + Fort", "wm"),
    ("frankandoak.com", "Frank And Oak", "wm"), ("tentree.com", "tentree", "wm"),
    ("outerknown.com", "Outerknown", "wm"), ("allbirds.com", "Allbirds", "wm"),
    ("fashionnova.com", "Fashion Nova", "wm"), ("thursdayboots.com", "Thursday Boot Co.", "wm"),
    ("fahertybrand.com", "Faherty", "wm"), ("bonobos.com", "Bonobos", "m"),
]


def stores() -> list[tuple[str, str, str]]:
    env = os.environ.get("SUGGEST_STORES")  # "domain|Name|wm,domain2|Name2|w"
    if env:
        out = []
        for part in env.split(","):
            bits = [b.strip() for b in part.split("|")]
            if bits and bits[0]:
                out.append((bits[0], bits[1] if len(bits) > 1 else bits[0], bits[2] if len(bits) > 2 else "wm"))
        return [x for x in out if not blocked_host("https://" + x[0]) and not _blocked_name(x[1])]
    return [x for x in DEFAULT_STORES if not blocked_host("https://" + x[0])]


def _plan_schema():
    from pydantic import BaseModel, Field

    class ShopQuery(BaseModel):
        query: str = Field(description="2-4 word store search query, e.g. 'linen tank', 'sage rib tank', "
                                       "'suede loafers'. Use common words that appear in product titles.")
        category: gemini.Category
        subcategory: str = Field(description="e.g. tank top, t-shirt, blouse, trousers, jeans, skirt, blazer, "
                                             "cardigan, jacket, sneakers, loafers, sandals, boots")
        color: str = Field(description="one simple color name")
        material: str
        pattern: str
        formality: int = Field(description="1-5")
        style_tags: list[str]
        gender_presentation: gemini.Gender
        why: str = Field(description="one short phrase: why it suits this wardrobe")

    class ShopPlan(BaseModel):
        queries: list[ShopQuery]

    return ShopPlan


def build_plan_prompt(cand: dict, mode: str, summary: dict, cand_eval: dict) -> str:
    """Same wardrobe/candidate/task description as the grounded prompt, but asks for search queries."""
    base = build_prompt(cand, mode, summary, cand_eval).split("\nRULES:")[0]
    base = base.replace("You are a personal shopper with Google Search. Find REAL products that are currently sold "
                        "online in the US.", "You are a personal shopper planning an online shopping search.")
    return base + (
        f"\nInstead of products, return {ASK_N + 2} search plans (one per product to look for), each with a short "
        "store search query and the attributes the product should have. Queries are run on the search boxes of "
        "mid-priced US fashion retailers (Everlane, Marine Layer, Petal & Pup, Princess Polly, Good American, "
        "Frank And Oak, Outerknown, Allbirds, ...), so use plain product-title words (no brand names, no "
        "adjectives like 'stylish'). Make the plans varied (different colors/materials/types).")


def _plan_queries(eid: str, mode: str, cand: dict, summary: dict, cand_eval: dict) -> dict:
    fx = os.environ.get("FITCHECK_SUGGEST_FIXTURE_DIR")
    if fx and (Path(fx) / f"{mode}.plan.json").exists():
        d = json.loads((Path(fx) / f"{mode}.plan.json").read_text())
        d["fixture"] = True
        return d
    prompt = build_plan_prompt(cand, mode, summary, cand_eval)
    t0 = time.time()
    try:
        plan = gemini._generate([prompt], _plan_schema())
    except Exception as e:
        if gemini._is_status(e, 404, 429, 500, 503):
            raise GeminiUnavailable("Gemini is out of free quota or busy right now (quota resets 3 AM ET)") from e
        raise
    d = {"kind": "plan", "mode": mode, "model": gemini._model_name, "prompt": prompt,
         "latency_s": round(time.time() - t0, 2), "queries": json.loads(plan.model_dump_json())["queries"]}
    _record(eid, d)
    return d


def _record(eid: str, d: dict) -> None:
    try:
        p = config.DATA_DIR / "suggest_raw"
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{eid}.{d.get('kind', 'grounded')}.json").write_text(json.dumps(d, indent=1))
    except OSError:
        pass


COLOR_WORDS = {  # product-title color word -> simple color (the palette Gemini uses for closet items)
    "black": "black", "jet": "black", "onyx": "black", "white": "white", "optic white": "white", "grey": "grey",
    "gray": "grey", "heather": "grey", "charcoal": "grey", "slate": "grey", "navy": "navy", "indigo": "navy",
    "blue": "blue", "cobalt": "blue", "denim": "blue", "light blue": "light blue", "sky": "light blue",
    "powder blue": "light blue", "baby blue": "light blue", "red": "red", "cherry": "red", "burgundy": "burgundy",
    "wine": "burgundy", "maroon": "burgundy", "oxblood": "burgundy", "pink": "pink", "blush": "pink", "rose": "pink",
    "fuchsia": "pink", "orange": "orange", "rust": "orange", "terracotta": "orange", "yellow": "yellow",
    "butter": "yellow", "mustard": "yellow", "green": "green", "sage": "green", "emerald": "green", "mint": "green",
    "forest": "green", "olive": "olive", "khaki": "khaki", "beige": "beige", "sand": "beige", "stone": "beige",
    "taupe": "beige", "oat": "beige", "oatmeal": "beige", "natural": "cream", "cream": "cream", "ivory": "cream",
    "ecru": "cream", "bone": "cream", "vanilla": "cream", "brown": "brown", "chocolate": "brown", "espresso": "brown",
    "mocha": "brown", "coffee": "brown", "tan": "tan", "camel": "tan", "cognac": "tan", "caramel": "tan",
    "purple": "purple", "plum": "purple", "lavender": "lavender", "lilac": "lavender", "silver": "grey",
    "gold": "yellow", "leopard": "brown", "multi": "multicolor",
}
MATERIAL_WORDS = ["linen", "cotton", "silk", "satin", "cashmere", "merino", "wool", "denim", "leather", "suede",
                  "canvas", "jersey", "poplin", "twill", "fleece", "nylon", "mesh", "lace", "velvet", "corduroy",
                  "knit", "rib", "ribbed", "modal", "tencel", "chiffon", "crochet", "seersucker", "gauze"]
PATTERN_WORDS = {"stripe": "striped", "striped": "striped", "floral": "floral", "plaid": "plaid", "check": "checked",
                 "gingham": "checked", "polka": "polka dot", "leopard": "animal print", "print": "print",
                 "graphic": "graphic"}
NEGATIVE_WORDS = ["gift card", "bundle", " set", "2-pack", "3-pack", "pack of", "sock", " bag", "tote", "swim",
                  "bikini", "bralette", " bra", "underwear", "brief", "boxer", "necklace", "earring", "hat", " cap",
                  "belt", "candle", "blanket", "mask", "kids", "baby", "toddler", "girls", "boys", "e-gift", "sample",
                  "insurance", "shipping", "laces", "insole", "cleaner"]


def _words_in(text: str, words) -> list[str]:
    return [w for w in words if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text)]


def product_color(text: str) -> str | None:
    """Simple color from a product title/tags ('Tissue Boatneck Tank | Beech' has none -> None)."""
    hits = _words_in(text, sorted(COLOR_WORDS, key=len, reverse=True))
    return COLOR_WORDS[hits[0]] if hits else None


def _gender_of(text: str) -> str | None:
    w = bool(re.search(r"\bwomen'?s?\b|\bwomens\b|\bfemale\b|\bladies\b", text))
    m = bool(re.search(r"(?<!wo)\bmen'?s?\b|(?<!wo)\bmens\b|(?<!fe)\bmale\b", text))
    return "womens" if w and not m else "mens" if m and not w else None


_store_sems: dict[str, threading.Semaphore] = collections.defaultdict(lambda: threading.Semaphore(2))
_store_cache: dict[tuple[str, str, int], tuple[float, list]] = {}


def search_store(domain: str, query: str, limit: int = 8) -> list[dict]:
    """Public Shopify predictive search. Gentle on the storefronts (they rate-limit bursts with 429): at most 2
    requests per store at a time, one retry after a 429, and results cached for 30 min."""
    from urllib.parse import quote
    key = (domain, query.lower().strip(), limit)
    hit = _store_cache.get(key)
    if hit and time.time() - hit[0] < PAGE_CACHE_TTL_S:
        return hit[1]
    url = (f"https://{domain}/search/suggest.json?q={quote(query)}&resources%5Btype%5D=product"
           f"&resources%5Blimit%5D={limit}")
    with _store_sems[domain], _client() as c:
        st, final, ct, body = _get(c, url, "application/json", 2_000_000)
        if st == 429:
            time.sleep(2.0)
            st, final, ct, body = _get(c, url, "application/json", 2_000_000)
    if st != 200 or not body:
        return []
    try:
        res = json.loads(body)["resources"]["results"]["products"] or []
    except Exception:
        return []
    _store_cache[key] = (time.time(), res)
    return res


def _score_hit(h: dict, q: dict, cand_gender: str | None, price_range: tuple[float, float],
               avoid_color: str | None = None) -> tuple[float, str]:
    """Relevance of one store search hit to one plan query (text matching only; outfit fit comes later from
    OutfitTransformer). Returns (score, reason_if_rejected)."""
    title = (h.get("title") or "").lower()
    tags = " ".join(str(t) for t in (h.get("tags") or [])).lower()
    text = f"{title} {(h.get('type') or '').lower()} {tags} {(h.get('handle') or '').replace('-', ' ')}"
    if not h.get("available", True):
        return -1, "sold out"
    price = _to_price(h.get("price"))
    if price is None:
        return -1, "no price"
    if not (price_range[0] <= price <= price_range[1]):
        return -1, f"price ${price:.0f} outside ${price_range[0]:.0f}-${price_range[1]:.0f}"
    if any(w in f" {title} " for w in NEGATIVE_WORDS):
        return -1, "not a garment"
    if is_accessory(None, None, title) or is_accessory(None, h.get("type") or "", None):
        return -1, "accessory"
    g = _gender_of(text)
    if cand_gender in ("mens", "womens") and g and g != cand_gender:
        return -1, f"{g} item"
    guess = guess_category_from_label(f"{title} {(h.get('type') or '').lower()}")
    if guess and guess != q["category"]:
        return -1, f"looks like {guess}"
    sub_tokens = [t for t in re.split(r"[\s/-]+", (q.get("subcategory") or "").lower()) if len(t) > 2
                  and t not in ("top", "shirt")] or [(q.get("subcategory") or "").lower()]
    score = 0.0
    if any(t and (t in title or t in (h.get("type") or "").lower()) for t in sub_tokens):
        score += 3
    elif guess is None:
        return -1, "type not in title"
    qcolor = COLOR_WORDS.get((q.get("color") or "").lower(), (q.get("color") or "").lower())
    pcolor = product_color(f"{title} {tags}")
    if pcolor and avoid_color and pcolor == avoid_color:
        return -1, f"same color ({pcolor}) as the item you're skipping"
    if pcolor and pcolor == qcolor:
        score += 2
    elif pcolor:
        score -= 2  # a different color than planned (e.g. the store returned the black variant)
    if (q.get("material") or "").lower() and (q.get("material") or "").lower().split()[0] in text:
        score += 1
    score += 0.5 * len([t for t in (q.get("query") or "").lower().split() if len(t) > 2 and t in title])
    return score, ""


def shop_search(plan: dict, cand: dict, mode: str, cand_eval: dict) -> tuple[list[dict], list[dict]]:
    """Run every plan query on every matching store (concurrently), pick the best hit per query (distinct
    products, spread across stores). Returns (products in the grounded-product format, rejected)."""
    cg = (cand.get("attributes") or {}).get("gender_presentation")
    gchar = {"womens": "w", "mens": "m"}.get(cg or "", "")
    sts = [s for s in stores() if not gchar or gchar in s[2]]
    queries = [q for q in plan.get("queries") or [] if q.get("query")][:ASK_N + 2]
    cp = (cand_eval.get("value") or {}).get("price") or (cand.get("attributes") or {}).get("price")
    price_range = ((max(5.0, float(cp) * 0.3), max(40.0, float(cp) * 2.0)) if (mode == "alternatives" and cp)
                   else (5.0, 120.0))
    avoid = (((cand.get("attributes") or {}).get("primary_color") or "").lower() or None) \
        if mode == "alternatives" else None
    jobs = [(qi, s) for qi in range(len(queries)) for s in sts]
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda j: search_store(j[1][0], queries[j[0]]["query"]), jobs))
    per_q: dict[int, list] = collections.defaultdict(list)
    rejected: list[dict] = []
    for (qi, s), hits in zip(jobs, results):
        for pos, h in enumerate(hits):
            sc, why = _score_hit(h, queries[qi], cg, price_range, avoid)
            if sc >= 0:
                per_q[qi].append((sc - 0.15 * pos, s, h))
    chosen, used_handles, used_stores = [], set(), collections.Counter()
    for qi, q in enumerate(queries):
        opts = sorted(per_q.get(qi, []), key=lambda x: -(x[0] - 1.0 * used_stores[x[1][0]]))
        pick = next((o for o in opts if (o[1][0], o[2].get("handle")) not in used_handles), None)
        if pick is None:
            rejected.append({"name": f"search '{q['query']}'", "retailer": None, "reason": "no matching product"})
            continue
        sc, (domain, sname, _), h = pick
        used_handles.add((domain, h.get("handle")))
        used_stores[domain] += 1
        title = h.get("title") or ""
        tags = " ".join(str(t) for t in (h.get("tags") or [])).lower()
        text = f"{title.lower()} {tags}"
        mats = _words_in(text, MATERIAL_WORDS)
        pats = _words_in(title.lower(), list(PATTERN_WORDS))
        img = h.get("image") or (h.get("featured_image") or {}).get("url")
        if img and "cdn.shopify.com" in img:
            img += ("&" if "?" in img else "?") + "width=900"
        chosen.append({
            "name": title, "brand": sname, "retailer": sname, "price": _to_price(h.get("price")),
            "product_url": f"https://{domain}/products/{h.get('handle')}", "image_url": img,
            "category": q["category"], "subcategory": q.get("subcategory"),
            "color": product_color(text), "material": (mats[0] if mats else None),
            "pattern": PATTERN_WORDS[pats[0]] if pats else "solid", "formality": q.get("formality"),
            "style_tags": q.get("style_tags") or [], "gender_presentation": _gender_of(text) or cg,
            "search_query": q["query"], "planned": {k: q.get(k) for k in ("color", "material", "why")},
            "match_score": round(sc, 2), "_span": (0, 0),
            "shopify_js": f"https://{domain}/products/{h.get('handle')}.js",
        })
    return chosen, rejected
