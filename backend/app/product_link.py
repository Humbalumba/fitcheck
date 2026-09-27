"""Product links for "Should I buy?": paste a retailer URL instead of taking a photo.

detect_from_url(url) fetches the product page, pulls out the product photo (plus title / brand / price / color /
description when the page has them) and then runs the SAME Stage 1-2 pipeline as an uploaded photo
(pipeline.detect: Gemini boxes + attributes -> segformer cutout). The scraped listing data is merged into the
matching detected item's attributes (brand if missing, `price` with price_source "listing" when the page states a
USD price, `product_url`, `listing_*` keys), so evaluation, verdict, outfits, suggestions, sustainability and
"I bought it" work exactly as for a photo.

Sources, in order of trust: Shopify's public `<product-url>.js` endpoint (any Shopify store, even when the HTML page
is bot-walled), JSON-LD `Product` schema, og:/product: meta tags (via the helpers app.suggest already uses for
suggestion pages). A direct image URL also works.

Safety: http/https only, standard ports only, no credentials in the URL, and every hop (including each redirect and
every image URL) must resolve to public IP addresses only (no localhost / private / link-local / metadata IPs), so
the endpoint can't be used to reach internal services (SSRF). Browser-like User-Agent, short timeouts, size caps.
"""
from __future__ import annotations

import html as _html
import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

from . import db
from .suggest import (HEADERS, IMG_ACCEPT, MAX_IMAGE_BYTES, PAGE_ACCEPT, _decode_image, _jsonld_objects, _is_product,
                      _meta, _page_images, _to_price, is_bad_landing, is_white_background, page_product_info,
                      product_color)

log = logging.getLogger("fitcheck.product_link")

MAX_URL_LEN = 2048
MAX_PAGE_BYTES = 3_000_000
MAX_REDIRECTS = 6
TIMEOUT_S = 10.0
MAX_IMAGES_TRIED = 5
ALLOWED_PORTS = (None, 80, 443)
UPLOAD_HINT = "Try uploading a photo or screenshot of the item instead."
BOT_WALL_RE = re.compile(r"captcha|px-captcha|cf-chl|challenge-platform|access denied|are you a (?:human|robot)|"
                         r"verify you are human|request unsuccessful|pardon our interruption|bm-verify|botfailover|"
                         r"_incapsula_|perimeterx|datadome|hang tight", re.I)


class LinkError(ValueError):
    """User-facing problem with a pasted link (API -> `status` with this message as `detail`)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ URL validation (SSRF guard)
def normalize_url(raw: str) -> str:
    """Trim, add https:// to a bare 'store.com/products/x', and reject anything that isn't a plain web URL."""
    u = (raw or "").strip()
    if not u:
        raise LinkError("Paste a link to a product page.", 400)
    if len(u) > MAX_URL_LEN:
        raise LinkError("That link is too long.", 400)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not re.match(r"^[^/:]+:\d", u):  # mailto:, javascript:, ...
            raise LinkError("Only web links (http or https) are supported.", 400)
        u = "https://" + u.lstrip("/")
    p = urlparse(u)
    if p.scheme.lower() not in ("http", "https"):
        raise LinkError("Only web links (http or https) are supported.", 400)
    if not p.hostname:
        raise LinkError("That doesn't look like a valid link.", 400)
    if p.username or p.password:
        raise LinkError("Links with a username or password aren't supported.", 400)
    try:
        port = p.port
    except ValueError:
        raise LinkError("That doesn't look like a valid link.", 400)
    if port not in ALLOWED_PORTS:
        raise LinkError("Only standard web links are supported (no custom ports).", 400)
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.params, p.query, ""))


def _resolve(host: str) -> list[str]:
    """All IP addresses a hostname resolves to (monkeypatched in tests)."""
    return list({ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def _extra_allowed_nets() -> list:
    """FITCHECK_LINK_ALLOW_NETS="198.18.0.0/15,...": extra ranges treated as public. Only for machines whose DNS
    answers with proxy "fake IPs" (e.g. a transparent proxy mapping every host into 198.18.0.0/15); leave unset."""
    out = []
    for c in os.environ.get("FITCHECK_LINK_ALLOW_NETS", "").split(","):
        try:
            out.append(ipaddress.ip_network(c.strip(), strict=False)) if c.strip() else None
        except ValueError:
            log.warning("ignoring bad FITCHECK_LINK_ALLOW_NETS entry %r", c)
    return out


def _is_public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
        return False
    return a.is_global or any(a in n for n in _extra_allowed_nets())


def check_public_url(url: str) -> str:
    """normalize_url + the host must resolve only to public addresses. Returns the normalized URL."""
    u = normalize_url(url)
    host = urlparse(u).hostname or ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise LinkError("That link points to a private address, which isn't allowed.", 400)
    try:
        ips = [host] if _is_ip_literal(host) else _resolve(host)
    except (socket.gaierror, UnicodeError, OSError):
        raise LinkError(f"Couldn't find the website {host}. Check the link and try again.", 400)
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise LinkError("That link points to a private address, which isn't allowed.", 400)
    return u


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ fetching (every hop re-validated)
def make_client(transport=None):
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=False, transport=transport,
                        timeout=httpx.Timeout(TIMEOUT_S, connect=5.0))


def fetch(client, url: str, accept: str, max_bytes: int, *, raise_errors: bool = True):
    """GET following redirects by hand so each hop passes check_public_url. Returns (status, final_url,
    content_type, body). Network problems raise LinkError (or return (None, url, None, b'') if raise_errors=False)."""
    import httpx
    cur = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            cur = check_public_url(cur)
            with client.stream("GET", cur, headers={"Accept": accept}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    cur = urljoin(cur, r.headers["location"])
                    continue
                ct = (r.headers.get("content-type") or "").lower()
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        break
                return r.status_code, cur, ct, bytes(body)
        raise LinkError("That link redirects too many times.", 502)
    except LinkError:
        if raise_errors:
            raise
    except httpx.TimeoutException:
        if raise_errors:
            raise LinkError(f"{_host(url)} took too long to respond. {UPLOAD_HINT}", 504)
    except httpx.HTTPError as e:
        log.info("link fetch failed %s: %s", url[:120], type(e).__name__)
        if raise_errors:
            raise LinkError(f"Couldn't open {_host(url)}. {UPLOAD_HINT}", 502)
    return None, cur, None, b""


def _host(u: str) -> str:
    h = (urlparse(u).hostname or u).lower()
    return h[4:] if h.startswith("www.") else h


# ------------------------------------------------------------------ extraction
def _strip_tags(s: str | None, limit: int = 400) -> str | None:
    if not s:
        return None
    t = re.sub(r"<[^>]+>", " ", str(s))
    t = re.sub(r"\s+", " ", _html.unescape(t)).strip()
    if not t:
        return None
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def _abs(u: str, base: str) -> str:
    u = u.strip()
    return "https:" + u if u.startswith("//") else urljoin(base, u)


def page_currency(html_text: str) -> str | None:
    for o in _jsonld_objects(html_text):
        if not _is_product(o):
            continue
        offers = o.get("offers")
        for off in (offers if isinstance(offers, list) else [offers]):
            if isinstance(off, dict) and isinstance(off.get("priceCurrency"), str):
                return off["priceCurrency"].upper()
    c = _meta(html_text, "product:price:currency", "og:price:currency", "priceCurrency")
    if c:
        return c.upper()
    m = re.search(r'Shopify\.currency\s*=\s*\{\s*"active"\s*:\s*"([A-Z]{3})"', html_text[:1_500_000])
    return m.group(1) if m else None


def page_description(html_text: str) -> str | None:
    for o in _jsonld_objects(html_text):
        if _is_product(o) and o.get("description"):
            return _strip_tags(o["description"])
    return _strip_tags(_meta(html_text, "og:description", "description", "twitter:description"))


def shopify_js_url(url: str) -> str | None:
    """'https://store.com/products/linen-shirt?variant=1' -> 'https://store.com/products/linen-shirt.js'."""
    p = urlparse(url)
    m = re.search(r"^(.*?/products/[^/?#]+?)(?:\.js|\.json|\.oembed)?/?$", p.path)
    if not m:
        return None
    return urlunparse((p.scheme, p.netloc, m.group(1) + ".js", "", "", ""))


def _variant_id(url: str) -> str | None:
    m = re.search(r"[?&]variant=(\d+)", url)
    return m.group(1) if m else None


def parse_shopify_js(body: bytes, url: str) -> dict | None:
    """Shopify's public product JSON (`/products/<handle>.js`): prices are integer cents of the shop currency."""
    try:
        d = json.loads(body)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("title"):
        return None
    variants = d.get("variants") or []
    vid = _variant_id(url)
    var = next((v for v in variants if str(v.get("id")) == vid), None) or (variants[0] if variants else {})
    price = var.get("price", d.get("price"))
    price = price / 100 if isinstance(price, (int, float)) and price > 0 else _to_price(price)
    color = None
    opts = d.get("options") or []
    for i, o in enumerate(opts):
        name = (o.get("name") if isinstance(o, dict) else str(o)) or ""
        if re.search(r"colou?r", name, re.I) and var.get(f"option{i + 1}"):
            color = str(var[f"option{i + 1}"])
    images = []
    fi = var.get("featured_image")
    if isinstance(fi, dict) and fi.get("src"):
        images.append(fi["src"])
    if d.get("featured_image"):
        images.append(d["featured_image"])
    for im in d.get("images") or []:
        images.append(im.get("src") if isinstance(im, dict) else im)
    images = [_abs(u, url) for u in images if isinstance(u, str) and u.strip()]
    return {"title": _strip_tags(d.get("title"), 200), "brand": (d.get("vendor") or "").strip() or None,
            "price": price, "color": color, "description": _strip_tags(d.get("description")),
            "product_type": (d.get("type") or d.get("product_type") or "").strip() or None,
            "images": list(dict.fromkeys(images))}


def extract_listing(html_text: str, page_url: str, shopify: dict | None = None) -> dict:
    """Title / brand / price (USD only) / currency / color / description / candidate image URLs from a product page
    (and the Shopify product JSON when available). Missing pieces are None; images may be empty."""
    info = page_product_info(html_text) if html_text else {}
    currency = page_currency(html_text) if html_text else None
    shopify = shopify or {}
    title = shopify.get("title") or _strip_tags(info.get("name"), 200)
    brand = shopify.get("brand") or info.get("brand")
    price = info.get("price")  # page_product_info only returns USD prices
    if shopify.get("price") and currency in (None, "USD"):  # Shopify JSON knows the selected variant's price
        price = shopify["price"]
    if price is not None:
        currency = "USD"
    images = list(shopify.get("images") or [])
    for u in [_abs(x, page_url) for x in info.get("images") or []] + (_page_images(html_text, page_url) if html_text else []):
        if u not in images:
            images.append(u)
    color = shopify.get("color") or info.get("color") or None
    if color and len(str(color)) > 30:  # e.g. a variant named like the whole product: keep just the color word
        color = product_color(str(color).lower())
    if not color and title:
        color = product_color(title.lower())
    site = _meta(html_text, "og:site_name") if html_text else None
    return {"title": title, "brand": brand, "retailer": site or _host(page_url), "price": price,
            "currency": currency, "color": color,
            "description": shopify.get("description") or (page_description(html_text) if html_text else None),
            "product_type": shopify.get("product_type") or ((info.get("types") or [None])[0]),
            "images": [u for u in images if re.match(r"^https?://", u)],
            "source": "shopify" if shopify else ("json-ld" if any(_is_product(o) for o in _jsonld_objects(html_text))
                                                 else "meta") if html_text else "none"}


def pick_image(client, urls: list[str]):
    """First decodable product photo, preferring a product-only shot (white/light background) among the first few.
    Returns (bytes, url, kind) or None."""
    first = None
    for u in urls[:MAX_IMAGES_TRIED]:
        try:
            check_public_url(u)
        except LinkError:
            continue
        st, final, ct, body = fetch(client, u, IMG_ACCEPT, MAX_IMAGE_BYTES, raise_errors=False)
        if st != 200:
            continue
        img = _decode_image(ct, body)
        if img is None:
            continue
        if is_white_background(img, frac=0.6):
            return body, final, "product_only"
        if first is None:
            first = (body, final, "photo")
    return first


# ------------------------------------------------------------------ main entry
def scrape(url: str, client=None) -> dict:
    """Fetch + parse a product link. Returns {"url", "final_url", "listing", "image_bytes", "image_source_url",
    "photo_kind"}. Raises LinkError with a friendly message when there's nothing usable."""
    u = check_public_url(url)
    own = client is None
    client = client or make_client()
    try:
        st, final, ct, body = fetch(client, u, PAGE_ACCEPT, MAX_PAGE_BYTES)
        host = _host(final)
        if st == 200 and ct.startswith("image/") and _decode_image(ct, body) is not None:  # a direct image link
            return {"url": u, "final_url": final, "image_bytes": body, "image_source_url": final, "photo_kind": "photo",
                    "listing": {"title": None, "brand": None, "retailer": host, "price": None, "currency": None,
                                "color": None, "description": None, "product_type": None, "images": [final],
                                "source": "image"}}
        if st == 200 and is_bad_landing(final):  # e.g. a sold-out product redirecting to search / home / 404 page
            raise LinkError(f"That link doesn't open a single product on {host} (it led to a search, home or error "
                            f"page). Copy the link from the product's own page.", 422)
        html_text = body.decode("utf-8", "ignore") if st == 200 and ("html" in ct or not ct) else ""
        shopify = None
        js = shopify_js_url(final) or shopify_js_url(u)
        if js:
            s2, f2, ct2, b2 = fetch(client, js, "application/json", MAX_PAGE_BYTES, raise_errors=False)
            if s2 == 200:
                shopify = parse_shopify_js(b2, final)
        if not html_text and not shopify:
            if st in (404, 410):
                raise LinkError(f"That page doesn't exist on {host} (HTTP {st}). Check the link.", 422)
            if st in (401, 403, 429, 503) or (body and BOT_WALL_RE.search(body[:20000].decode("utf-8", "ignore"))):
                raise LinkError(f"{host} blocks automated access, so we can't read that page. {UPLOAD_HINT}", 422)
            raise LinkError(f"Couldn't read that page on {host} (HTTP {st}). {UPLOAD_HINT}", 422)
        listing = extract_listing(html_text, final, shopify)
        if not listing["images"]:
            if html_text and BOT_WALL_RE.search(html_text[:30000]):
                raise LinkError(f"{host} blocks automated access, so we can't read that page. {UPLOAD_HINT}", 422)
            raise LinkError(f"Couldn't find a product photo on that {host} page. {UPLOAD_HINT}", 422)
        picked = pick_image(client, listing["images"])
        if picked is None:
            raise LinkError(f"Couldn't download a usable product photo from {host}. {UPLOAD_HINT}", 422)
        img_bytes, img_url, kind = picked
        return {"url": u, "final_url": final, "listing": listing, "image_bytes": img_bytes,
                "image_source_url": img_url, "photo_kind": kind}
    finally:
        if own:
            client.close()


def _match_score(item: dict, listing: dict) -> int:
    """How well a detected item matches the listing's garment type (higher = better)."""
    from .segment import guess_category_from_label
    text = " ".join(x for x in (listing.get("title"), listing.get("product_type")) if x)
    want = guess_category_from_label(text) if text else None
    a = item.get("attributes") or {}
    s = 0
    if want and (item.get("category") or a.get("category")) == want:
        s += 10
    sub = str(a.get("subcategory") or "").lower()
    if sub and text and sub in text.lower():
        s += 5
    bbox = item.get("bbox") or [0, 0, 0, 0]
    s += int(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / 250_000)  # bigger box = more likely the product (0-4)
    return s


def listing_attributes(listing: dict, url: str, attrs: dict) -> dict:
    """Scraped listing data merged into a detected item's attributes (never overrides a price already on the item)."""
    a = dict(attrs)
    a["product_url"] = url
    for k in ("title", "retailer", "description", "color"):
        if listing.get(k):
            a[f"listing_{k}"] = listing[k]
    if listing.get("brand") and not a.get("brand"):
        a["brand"] = listing["brand"]
    if listing.get("price") is not None:
        a["listing_price"] = listing["price"]
        a["listing_currency"] = listing.get("currency") or "USD"
        if a.get("price") in (None, ""):
            a["price"], a["price_source"], a["currency"] = listing["price"], "listing", "USD"
    if listing.get("color") and a.get("source") != "gemini":  # offline detector: the listing's color is better
        c = product_color(str(listing["color"]).lower())
        if c:
            a["primary_color"] = c
    return a


def detect_from_url(url: str, purpose: str | None = "candidate", client=None) -> dict:
    """POST /api/detect-url: scrape the product link, then the normal photo pipeline + listing data."""
    from . import pipeline
    s = scrape(url, client=client)
    res = pipeline.detect(s["image_bytes"], purpose, source="url")
    listing = s["listing"]
    items = res.get("items") or []
    best = max(items, key=lambda it: _match_score(it, listing)) if items else None
    if best is not None:
        it = db.get_item(best["id"])
        a = listing_attributes(listing, s["final_url"], it.get("attributes") or {})
        db.update_item(best["id"], attributes=a)
        new = pipeline.item_to_api(db.get_item(best["id"]))
        items = [new] + [i for i in items if i["id"] != best["id"]]  # the listed product first
    product = {k: v for k, v in listing.items() if k != "images"}
    product.update({"url": s["url"], "final_url": s["final_url"], "image_source_url": s["image_source_url"],
                    "photo_kind": s["photo_kind"]})
    return {**res, "items": items, "source": "url", "product": product,
            "suggested_item_id": best["id"] if best else None}
