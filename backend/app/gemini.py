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
    top = "top"; bottom = "bottom"; dress = "dress"; outerwear = "outerwear"; shoes = "shoes"; accessory = "accessory"


class Gender(str, Enum):
    mens = "mens"; womens = "womens"; unisex = "unisex"


class Attributes(BaseModel):
    category: Category
    subcategory: str = Field(description="e.g. t-shirt, polo, button-down shirt, blouse, sweater, hoodie, tank top, "
                                         "jeans, chinos, trousers, skirt, shorts, leggings, blazer, jacket, coat, "
                                         "cardigan, sneakers, boots, heels, bag, belt, hat")
    primary_color: str = Field(description="simple color name, e.g. navy, white, black, olive, beige")
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
    description: str = Field(description="one-line description of the item")


# ------------------------------------------------------------------ client / model
_client = None
_model_name: str | None = None
_lock = threading.Lock()


def _key_from_dotenv() -> str | None:
    """Allow dropping GEMINI_API_KEY into backend/.env or fitcheck/.env without restarting the server."""
    for p in (config.BACKEND_DIR / ".env", config.BACKEND_DIR.parent / ".env"):
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY=")):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except OSError:
            continue
    return None


def api_key() -> str | None:
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or _key_from_dotenv()
    return k.strip() if k and k.strip() else None


def is_configured() -> bool:
    return api_key() is not None


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


def model_name() -> str:
    global _model_name
    if _model_name:
        return _model_name
    if config.GEMINI_MODEL != "auto":
        _model_name = config.GEMINI_MODEL
        return _model_name
    try:
        names = []
        for m in client().models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append(m.name)
        _model_name = pick_newest_flash(names) or config.GEMINI_FALLBACK_MODEL
        log.info("Gemini model auto-selected: %s", _model_name)
    except Exception as e:
        log.warning("Could not list Gemini models (%s); using %s", type(e).__name__, config.GEMINI_FALLBACK_MODEL)
        _model_name = config.GEMINI_FALLBACK_MODEL
    return _model_name


def _img_part(img: Image.Image, fmt: str = "JPEG"):
    from google.genai import types
    buf = io.BytesIO()
    im = img.convert("RGB") if fmt == "JPEG" else img
    im.save(buf, format=fmt, quality=90)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg" if fmt == "JPEG" else "image/png")


_working_variant: int | None = None


def _generate(contents, schema, low_thinking: bool = True):
    from google.genai import types
    name = model_name()
    base = dict(response_mime_type="application/json", response_schema=schema, temperature=0.2)
    attempts = []
    if low_thinking:
        if re.search(r"gemini-2\.", name):
            attempts.append(dict(thinking_config=types.ThinkingConfig(thinking_budget=0)))
        else:
            attempts.append(dict(thinking_config=types.ThinkingConfig(thinking_level="minimal")))
            attempts.append(dict(thinking_config=types.ThinkingConfig(thinking_level="low")))
    attempts.append({})
    global _working_variant
    if _working_variant is not None and _working_variant < len(attempts):
        attempts = attempts[_working_variant:]
        offset = _working_variant
    else:
        offset = 0
    last = None
    for n, extra in enumerate(attempts):
        try:
            resp = client().models.generate_content(
                model=name, contents=contents, config=types.GenerateContentConfig(**base, **extra))
            _working_variant = offset + n  # remember which thinking config this model accepts
            if getattr(resp, "parsed", None) is not None:
                return resp.parsed
            return schema.model_validate_json(resp.text)
        except Exception as e:  # unsupported thinking config etc. -> try next variant
            last = e
            log.warning("Gemini call failed with %s (%s); retrying variant", type(e).__name__, str(e)[:200])
    raise last


# ------------------------------------------------------------------ Stage 1a
DETECT_PROMPT = """You are a fashion vision system. Detect EVERY distinct clothing item in this photo:
tops, bottoms, dresses, outerwear, shoes and accessories (bags, hats, belts, scarves).
Photos may be flat lays of several garments on a floor or bed, items on hangers/racks, or items being worn.
Rules:
- One box per garment. A pair of shoes is ONE item. Do not box people, faces, hangers, furniture, or tags.
- If a garment is worn, box only that garment (e.g. shirt and pants separately).
- Boxes must tightly contain the whole garment.
Return box_2d as [ymin, xmin, ymax, xmax] normalized to 0-1000 and a short descriptive label (color + type)."""


def detect_boxes(img: Image.Image) -> list[dict]:
    res: Boxes = _generate([DETECT_PROMPT, _img_part(img)], Boxes)
    out = []
    for b in res.boxes[:25]:
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
in the photos; otherwise null. Only fill brand if clearly visible; otherwise null."""


def identify(cutout_white: Image.Image, context: Image.Image, label: str) -> dict:
    res: Attributes = _generate([IDENTIFY_PROMPT.format(label=label), _img_part(cutout_white), _img_part(context)],
                                Attributes)
    d = json.loads(res.model_dump_json())
    d["formality"] = int(max(1, min(5, d.get("formality") or 2)))
    d["formality_label"] = FORMALITY_LABELS[d["formality"]]
    d["source"] = "gemini"
    return d
