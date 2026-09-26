"""Gemini (google-genai) calls: Stage 1a bounding boxes and Stage 2 structured attributes.

The API key comes from GEMINI_API_KEY and is never logged. Model is config.GEMINI_MODEL; "auto"
lists models and picks the newest Flash model (non lite/image/tts/live variants).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
from enum import Enum
from typing import Optional

from PIL import Image
from pydantic import BaseModel, Field

from . import config

log = logging.getLogger("fitcheck.gemini")

FORMALITY_LABELS = {1: "very casual", 2: "casual", 3: "smart casual", 4: "business", 5: "formal"}


# ------------------------------------------------------------------ schemas
class Box(BaseModel):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] normalized to 0-1000")
    label: str = Field(description="short label, e.g. 'navy crew-neck t-shirt'")


class Boxes(BaseModel):
    boxes: list[Box]


class Category(str, Enum):
    # clothes and shoes only: accessories (bags, jewellery, hats, belts...) are not part of FitCheck (app/accessories.py)
    top = "top"; bottom = "bottom"; dress = "dress"; outerwear = "outerwear"; shoes = "shoes"


from .garment_schema import GarmentDetails  # noqa: E402


class Gender(str, Enum):
    mens = "mens"; womens = "womens"; unisex = "unisex"


class Attributes(BaseModel):
    category: Category = Field(description="top = shirts/tees/blouses/sweaters/hoodies worn as the base upper layer; "
                                           "outerwear = anything worn OVER a top: jackets (incl. hooded/zip/denim/"
                                           "bomber/windbreaker), coats, blazers, suit jackets, cardigans, vests; "
                                           "bottom = pants/jeans/skirts/shorts; dress = dresses/jumpsuits; "
                                           "shoes = any footwear")
    subcategory: str = Field(description="specific type, e.g. tops: t-shirt, long-sleeve tee, polo, button-down "
                                         "shirt, blouse, sweater, hoodie, tank top; bottoms: jeans, chinos, trousers, "
                                         "skirt, shorts, leggings; dress: dress, maxi dress, jumpsuit; outerwear: "
                                         "blazer, jacket, coat, cardigan, vest; shoes: sneakers, boots, heels, flats, "
                                         "loafers, sandals")
    primary_color: str = Field(description="dominant garment color as ONE simple name; look carefully: dark navy is navy, not black (black, white, grey, navy, blue, "
                                           "light blue, red, burgundy, pink, orange, yellow, green, olive, khaki, "
                                           "beige, cream, brown, tan, purple, lavender, multicolor); ignore background")
    secondary_colors: list[str]
    pattern: str = Field(description="solid, striped, plaid, checked, floral, graphic, polka dot, camo, ...")
    fabric_guess: str
    formality: int = Field(description="1=very casual, 2=casual, 3=smart casual, 4=business, 5=formal")
    seasons: list[str] = Field(description="subset of spring, summer, fall, winter")
    style_tags: list[str] = Field(description="3-6 short style tags, e.g. minimalist, streetwear, preppy, athleisure")
    gender_presentation: Gender
    brand: Optional[str] = Field(default=None, description="only if a logo/label is clearly visible, else null")
    price: Optional[float] = Field(default=None, description="ONLY if a price tag is visible in the context photo, else null")
    currency: Optional[str] = Field(default=None, description="ISO code like USD if a price is visible, else null")
    estimated_price_usd: Optional[float] = Field(
        default=None, description="ALWAYS fill: typical NEW full retail price in USD for this brand + garment type + "
                                  "material; if the brand is unknown, a typical US mid-market price for this type")
    price_confidence: Optional[str] = Field(
        default=None, description="confidence in estimated_price_usd: 'high' only when the brand is clearly "
                                  "identified, 'medium', or 'low'")
    description: str = Field(description="one-line description of the item")


# ------------------------------------------------------------------ client / model
_client = None
_model_name: str | None = None
_lock = threading.Lock()


def _key_from_dotenv(names: tuple[str, ...] = ("GEMINI_API_KEY=", "GOOGLE_API_KEY=")) -> str | None:
    """Allow dropping GEMINI_API_KEY into backend/.env or fitcheck/.env without restarting the server."""
    for p in (config.BACKEND_DIR / ".env", config.BACKEND_DIR.parent / ".env"):
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith(names):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except OSError:
            continue
    return None


def api_key() -> str | None:
    if os.environ.get("FITCHECK_GEMINI_OFF") == "1":  # tests / offline runs: never spend quota
        return None
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or _key_from_dotenv()
    return k.strip() if k and k.strip() else None


def is_configured() -> bool:
    return api_key() is not None


def image_api_key() -> tuple[str | None, str]:
    """(key, source) for image generation (render option 1): GEMINI_IMAGE_API_KEY (env or backend/.env) -- e.g. a
    billing-enabled key -- else the main key. source is 'image' | 'main' | 'none'. Never logged."""
    if os.environ.get("FITCHECK_GEMINI_OFF") == "1":
        return None, "none"
    k = os.environ.get("GEMINI_IMAGE_API_KEY") or _key_from_dotenv(("GEMINI_IMAGE_API_KEY=",))
    if k and k.strip():
        return k.strip(), "image"
    k = api_key()
    return (k, "main") if k else (None, "none")


def key_fingerprint(key: str | None) -> str | None:
    """Short sha256 prefix identifying a key (safe to persist / show; the key itself never is)."""
    if not key:
        return None
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:12]


_image_client = None
_image_client_fp = None


def image_client():
    """google-genai client for image generation (separate key if GEMINI_IMAGE_API_KEY is set)."""
    global _image_client, _image_client_fp
    key, src = image_api_key()
    if key is None:
        raise RuntimeError("no Gemini key configured for image generation")
    if src == "main":
        return client()
    with _lock:
        fp = key_fingerprint(key)
        if _image_client is None or fp != _image_client_fp:
            from google import genai
            from google.genai import types
            _image_client = genai.Client(api_key=key,
                                         http_options=types.HttpOptions(timeout=int(config.GEMINI_TIMEOUT_S * 1000)))
            _image_client_fp = fp
        return _image_client


_client_key_hash = None


def client():
    global _client, _client_key_hash
    with _lock:
        kh = hash(api_key())
        if _client is not None and kh != _client_key_hash:
            _client = None  # key changed
        if _client is None:
            _client_key_hash = kh
            from google import genai
            from google.genai import types
            if not is_configured():
                raise RuntimeError("GEMINI_API_KEY is not set")
            _client = genai.Client(api_key=api_key(),
                                   http_options=types.HttpOptions(timeout=int(config.GEMINI_TIMEOUT_S * 1000)))
        return _client


_FLASH_RE = re.compile(r"^(?:models/)?gemini-(\d+(?:\.\d+)?)-flash(-preview)?(?:-(\d{2}-\d{4}|\d{3}))?$")


def pick_newest_flash(names: list[str]) -> str | None:
    best, best_key = None, None
    for n in names:
        m = _FLASH_RE.match(n)
        if not m:
            continue
        ver = float(m.group(1))
        stable = m.group(2) is None
        key = (ver, stable, m.group(3) or "")
        if best_key is None or key > best_key:
            best, best_key = n.split("/")[-1], key
    return best


_chain: list[str] | None = None
_exhausted: dict[str, float] = {}  # model -> unix time when its daily free-tier quota resets


def _flash_sort_key(n: str):
    m = _FLASH_RE.match(n)
    return (float(m.group(1)), m.group(2) is None, m.group(3) or "") if m else (0.0, False, "")


def model_chain() -> list[str]:
    """Models to try in order. Free-tier keys get ~20 requests/day *per model*, so when one model's daily
    quota is exhausted we fail over to the next Flash model instead of dropping to the offline fallback."""
    global _chain
    if _chain is not None:
        return _chain
    listed: list[str] = []
    try:
        for m in client().models.list():
            actions = getattr(m, "supported_actions", None) or []
            if (not actions or "generateContent" in actions) and _FLASH_RE.match(m.name):
                listed.append(m.name.split("/")[-1])
    except Exception as e:
        log.warning("Could not list Gemini models (%s)", type(e).__name__)
    listed = sorted(set(listed), key=_flash_sort_key, reverse=True)
    first = [] if config.GEMINI_MODEL == "auto" else [config.GEMINI_MODEL]
    chain = first + [n for n in listed if n not in first] + [config.GEMINI_FALLBACK_MODEL]
    _chain = list(dict.fromkeys(chain))[: config.GEMINI_CHAIN_MAX]
    if config.GEMINI_FALLBACK_MODEL not in _chain:
        _chain.append(config.GEMINI_FALLBACK_MODEL)
    log.info("Gemini model chain: %s", _chain)
    return _chain


def _available(name: str) -> bool:
    import time as _time
    until = _exhausted.get(name)
    return until is None or _time.time() >= until


def model_name() -> str:
    """The model currently in use (first model in the chain whose daily quota isn't exhausted)."""
    global _model_name
    for n in model_chain():
        if _available(n):
            _model_name = n
            return n
    _model_name = model_chain()[0]
    return _model_name


def _mark_exhausted(name: str, err: Exception) -> None:
    import datetime as _dt
    import time as _time
    now = _dt.datetime.now(_dt.timezone.utc)
    reset = now.replace(hour=7, minute=0, second=0, microsecond=0)  # quotas reset at midnight Pacific
    if reset <= now:
        reset += _dt.timedelta(days=1)
    per_day = "PerDay" in str(err)
    _exhausted[name] = reset.timestamp() if per_day else _time.time() + 60
    log.warning("Gemini model %s quota exhausted (%s); failing over", name, "daily" if per_day else "per-minute")


def _img_part(img: Image.Image, fmt: str = "JPEG"):
    from google.genai import types
    buf = io.BytesIO()
    im = img.convert("RGB") if fmt == "JPEG" else img
    im.save(buf, format=fmt, quality=90)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg" if fmt == "JPEG" else "image/png")


_working_variant: dict[str, int] = {}  # model -> index of the thinking config it accepts
_RETRY_DELAY_RE = re.compile(r"retry(?:Delay)?['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.I)


def _is_status(e: Exception, *codes: int) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code in codes or any(f"{c} " in str(e)[:12] for c in codes)


def _call(name: str, contents, cfg):
    """generate_content with short retries on transient 503/500 and per-minute 429s.
    A daily-quota 429 is raised immediately (the caller fails over to the next model)."""
    import time as _time
    last = None
    for attempt in range(config.GEMINI_RETRIES + 1):
        try:
            return client().models.generate_content(model=name, contents=contents, config=cfg)
        except Exception as e:
            last = e
            if (not _is_status(e, 429, 500, 503) or attempt == config.GEMINI_RETRIES
                    or (_is_status(e, 429) and "PerDay" in str(e))):
                raise
            m = _RETRY_DELAY_RE.search(str(e))
            delay = min(float(m.group(1)) + 0.5, 20.0) if m else min(2.0 * (2 ** attempt), 10.0)
            log.warning("Gemini %s busy/rate-limited (%s); retry %d in %.1fs", name, type(e).__name__, attempt + 1, delay)
            _time.sleep(delay)
    raise last


def _thinking_variants(name: str, low_thinking: bool):
    from google.genai import types
    v = []
    if low_thinking:
        if re.search(r"gemini-2\.", name):
            v.append(dict(thinking_config=types.ThinkingConfig(thinking_budget=0)))
        else:
            v.append(dict(thinking_config=types.ThinkingConfig(thinking_level="minimal")))
            v.append(dict(thinking_config=types.ThinkingConfig(thinking_level="low")))
    v.append({})
    return v


def _generate(contents, schema, low_thinking: bool = True):
    from google.genai import types
    base = dict(response_mime_type="application/json", response_schema=schema, temperature=0.2)
    last = None
    tried = set()
    while True:
        name = model_name()
        if name in tried:
            break
        tried.add(name)
        variants = _thinking_variants(name, low_thinking)
        start = min(_working_variant.get(name, 0), len(variants) - 1)
        failover = False
        for n in range(start, len(variants)):
            try:
                resp = _call(name, contents, types.GenerateContentConfig(**base, **variants[n]))
                _working_variant[name] = n
                if getattr(resp, "parsed", None) is not None:
                    return resp.parsed
                return schema.model_validate_json(resp.text)
            except Exception as e:
                last = e
                if _is_status(e, 404, 429) or (_is_status(e, 500, 503)):
                    # model gone / quota exhausted / still overloaded after retries -> next model
                    if _is_status(e, 429):
                        _mark_exhausted(name, e)
                    else:
                        import time as _time
                        _exhausted[name] = _time.time() + (86400 if _is_status(e, 404) else 30)
                        log.warning("Gemini model %s unavailable (%s); failing over", name, str(e)[:80])
                    failover = True
                    break
                log.warning("Gemini call failed with %s (%s); retrying variant", type(e).__name__, str(e)[:200])
        if not failover:
            break
    raise last if last else RuntimeError("no Gemini model available")


# ------------------------------------------------------------------ Stage 1a
DETECT_PROMPT = """You are a fashion vision system. Detect EVERY distinct clothing item in this photo:
tops, bottoms, dresses, outerwear and shoes ONLY.
IGNORE ALL ACCESSORIES: bags, backpacks, wallets, jewellery (necklaces, bracelets, rings, earrings, watches), hats,
caps, beanies, belts, scarves, sunglasses/glasses, gloves, ties and socks. Never return a box for an accessory. If the
photo contains only accessories, return an empty list.
Photos may be flat lays of several garments on a floor or bed, items on hangers/racks, or items being worn.
Rules:
- One box per garment. A pair of shoes is ONE item. Do not box people, faces, hangers, furniture, tags or accessories.
- If a garment is worn, box only that garment (e.g. shirt and pants separately).
- Boxes must tightly contain the whole visible garment, including parts partly covered by other items.
Return box_2d as [ymin, xmin, ymax, xmax] normalized to 0-1000 and a short descriptive label (color + type)."""


def detect_boxes(img: Image.Image, dropped: list | None = None) -> list[dict]:
    """Two-stage mode boxes. Accessory boxes (by label) are dropped defensively; their labels go to `dropped`."""
    from .accessories import is_accessory
    res: Boxes = _generate([DETECT_PROMPT, _img_part(img)], Boxes)
    out = []
    for b in res.boxes[:25]:
        if is_accessory(label=b.label):
            if dropped is not None:
                dropped.append(b.label.strip())
            continue
        if len(b.box_2d) != 4:
            continue
        y0, x0, y1, x1 = [max(0, min(1000, int(v))) for v in b.box_2d]
        if y1 <= y0 or x1 <= x0:
            continue
        if (y1 - y0) * (x1 - x0) < 400:  # < 0.04% of the image: noise
            continue
        out.append({"box_2d": [y0, x0, y1, x1], "label": b.label.strip()})
    return out


# ------------------------------------------------------------------ Stage 2
IDENTIFY_PROMPT = """You are a fashion cataloguing assistant. Image 1 is a cutout of ONE clothing item
(detected as: "{label}"). Image 2 is the surrounding region of the original photo, for context only
(use it to read price tags / brand labels; describe only the item in image 1).
Return the item's attributes. Only fill price/currency if a price tag for this item is clearly readable
in the photos; otherwise null. Only fill brand if clearly visible; otherwise null.
Always estimate estimated_price_usd (typical new US retail for this brand + type + material; mid-market for the type
if the brand is unknown) and price_confidence (high only if the brand is clearly identified, else medium or low)."""


def identify(cutout_white: Image.Image, context: Image.Image, label: str) -> dict:
    res: Attributes = _generate([IDENTIFY_PROMPT.format(label=label), _img_part(cutout_white), _img_part(context)],
                                Attributes)
    d = json.loads(res.model_dump_json())
    d["formality"] = int(max(1, min(5, d.get("formality") or 2)))
    d["formality_label"] = FORMALITY_LABELS[d["formality"]]
    d["source"] = "gemini"
    from .pricing import normalize_estimate
    normalize_estimate(d, d.get("category"))  # validate the estimate; table fallback if the model omitted it
    return d


# ------------------------------------------------------------------ Stage 1+2 in one call
class DetectedItem(Attributes):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] normalized to 0-1000, tight around the garment")
    label: str = Field(description="short label, color + type, e.g. 'navy hooded zip jacket'")
    details: Optional[GarmentDetails] = Field(
        default=None, description="construction details for the product-image renderer; graphics box_2d are "
                                  "normalised 0-1000 relative to the WHOLE photo")


class DetectedItems(BaseModel):
    items: list[DetectedItem]


DETECT_DESCRIBE_PROMPT = DETECT_PROMPT + """
For EACH detected item also return its catalogue attributes (category, subcategory, colors, pattern, fabric,
formality 1-5, seasons, 3-6 style tags, gender presentation, brand only if a logo/label is clearly readable,
price+currency ONLY if a price tag attached to that item is clearly readable, else null, one-line description,
estimated_price_usd = typical new US retail price for that brand + type + material (typical mid-market price for the
type if the brand is unknown), price_confidence = high only if the brand is clearly identified, else medium or low).
Jackets, blazers, coats, cardigans and vests are category "outerwear", never "top".
Also fill "details" for each garment: garment_type, which side faces the camera (front/back), sleeve length, neckline,
closure, hood, front pockets, ribbed cuffs/hem, fit, length, hood-lining / hardware / contrast-stitch colours, and
EVERY logo / printed or embroidered text / graphic / brand patch on it (tight box_2d relative to the whole photo, exact
text, position on the garment from the wearer's point of view, clockwise degrees to make it upright). Never invent
logos."""


def detect_items(img: Image.Image, dropped: list | None = None) -> list[dict]:
    """One Gemini call: boxes + full attributes for every garment (saves 1 request per item vs detect+identify).
    Accessories are dropped defensively even though the schema/prompt exclude them; their labels go to `dropped`."""
    from .accessories import is_accessory
    res: DetectedItems = _generate([DETECT_DESCRIBE_PROMPT, _img_part(img)], DetectedItems)
    out = []
    for it in res.items[:25]:
        cat = it.category.value if isinstance(it.category, Enum) else str(it.category or "")
        if is_accessory(cat, it.subcategory, it.label):
            log.info("Dropping accessory from detection: %s (%s)", it.label, it.subcategory)
            if dropped is not None:
                dropped.append((it.label or it.subcategory or "accessory").strip())
            continue
        if len(it.box_2d) != 4:
            continue
        y0, x0, y1, x1 = [max(0, min(1000, int(v))) for v in it.box_2d]
        if y1 <= y0 or x1 <= x0 or (y1 - y0) * (x1 - x0) < 400:
            continue
        d = json.loads(it.model_dump_json())
        d.pop("box_2d", None)
        label = d.pop("label", "").strip() or d.get("description", "clothing item")
        d["formality"] = int(max(1, min(5, d.get("formality") or 2)))
        d["formality_label"] = FORMALITY_LABELS[d["formality"]]
        d["source"] = "gemini"
        from .pricing import normalize_estimate
        normalize_estimate(d, d.get("category"))
        out.append({"box_2d": [y0, x0, y1, x1], "label": label, "attributes": d})
    return out


# ------------------------------------------------------------------ Grounded search (shopping suggestions)
_no_grounding: set[str] = set()  # models that rejected the google_search tool (400) in this process
_grounding_blocked_until = 0.0   # set when the key's tier has no search grounding (free tier on Gemini 3.x)


class GeminiUnavailable(RuntimeError):
    """Every model in the chain is out of quota / unavailable (message is user-presentable)."""


class GroundingUnavailable(RuntimeError):
    """Google Search grounding isn't available for this API key (e.g. free tier) -- caller should fall back."""


def grounding_available() -> bool:
    import time as _time
    return _time.time() >= _grounding_blocked_until


def generate_grounded(prompt: str, low_thinking: bool = True) -> dict:
    """ONE generate_content call with the Google Search grounding tool, failing over along model_chain().
    Grounded calls can't use response_mime_type=JSON on every model, so the caller parses JSON from the text.
    Returns a plain dict {"text", "model", "chunks": [{"uri","title","domain"}], "supports": [{"start","end",
    "text","chunks"}], "queries"} so it can be saved as a fixture and replayed.

    Quota handling differs from _generate on purpose: a 429 WITHOUT a per-day/per-minute quota metric means
    "search grounding isn't available on this tier" (free tier on Gemini 3.x) -> GroundingUnavailable, and the
    models are NOT marked exhausted (normal detection calls still work). A per-day 429 marks that model exhausted."""
    import time as _time
    global _grounding_blocked_until
    from google.genai import types
    if not grounding_available():
        raise GroundingUnavailable("Google Search grounding isn't available for this API key")
    last = None
    for name in [n for n in model_chain() if _available(n) and n not in _no_grounding]:
        variants = _thinking_variants(name, low_thinking)
        start = min(_working_variant.get(name, 0), len(variants) - 1)
        next_model = False
        for n in range(start, len(variants)):
            cfg = types.GenerateContentConfig(temperature=0.4, tools=[types.Tool(google_search=types.GoogleSearch())],
                                              **variants[n])
            for attempt in range(2):
                try:
                    resp = client().models.generate_content(model=name, contents=[prompt], config=cfg)
                    _working_variant[name] = n
                    return _grounded_to_dict(resp, name)
                except Exception as e:
                    last = e
                    msg = str(e)
                    if _is_status(e, 429):
                        if "PerDay" in msg:
                            _mark_exhausted(name, e)
                            next_model = True
                        elif "PerMinute" in msg or _RETRY_DELAY_RE.search(msg):
                            if attempt == 0:
                                m = _RETRY_DELAY_RE.search(msg)
                                _time.sleep(min(float(m.group(1)) + 0.5, 15.0) if m else 5.0)
                                continue
                            next_model = True
                        else:  # "check your plan and billing": no grounding on this tier, same for every model
                            _grounding_blocked_until = _time.time() + 6 * 3600
                            log.warning("Google Search grounding not available for this key (%s); using fallback",
                                        msg[:90])
                            raise GroundingUnavailable("Google Search grounding isn't available for this API key "
                                                       "(free tier)") from e
                    elif _is_status(e, 500, 503):
                        if attempt == 0:
                            _time.sleep(2.0)
                            continue
                        next_model = True
                    elif _is_status(e, 404):
                        next_model = True
                    elif _is_status(e, 400) and re.search(r"search|tool|grounding", msg, re.I) \
                            and "thinking" not in msg.lower():
                        log.warning("Gemini %s rejected google_search grounding (%s); failing over", name, msg[:120])
                        _no_grounding.add(name)
                        next_model = True
                    break  # other errors (e.g. thinking variant rejected): next variant
            if next_model:
                break
    if last is None or _is_status(last, 404, 429, 500, 503):
        raise GeminiUnavailable("Gemini is out of free quota or busy right now (quota resets 3 AM ET)") from last
    raise last


def _grounded_to_dict(resp, model: str) -> dict:
    text = ""
    try:
        text = resp.text or ""
    except Exception:
        pass
    if not text:
        try:
            text = "".join(p.text or "" for p in resp.candidates[0].content.parts if getattr(p, "text", None))
        except Exception:
            text = ""
    chunks, supports, queries = [], [], []
    try:
        gm = resp.candidates[0].grounding_metadata
    except Exception:
        gm = None
    if gm is not None:
        for c in gm.grounding_chunks or []:
            w = getattr(c, "web", None)
            if w is not None:
                chunks.append({"uri": w.uri, "title": w.title, "domain": getattr(w, "domain", None)})
            else:
                chunks.append({"uri": None, "title": None, "domain": None})
        for s in gm.grounding_supports or []:
            seg = s.segment
            supports.append({"start": getattr(seg, "start_index", None) or 0, "end": getattr(seg, "end_index", None) or 0,
                             "text": getattr(seg, "text", None), "chunks": list(s.grounding_chunk_indices or [])})
        queries = list(gm.web_search_queries or [])
    return {"text": text, "model": model, "chunks": chunks, "supports": supports, "queries": queries}
