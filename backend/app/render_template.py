"""Canonical TEMPLATE renderer (clean_method='template'): brand-catalogue product images without image generation.

Per item: pick a procedural garment template (app/garment_templates.py) from the garment details -> recolour it with
the garment's real fabric colour (robust LAB median over segformer garment pixels, logos/shadows/highlights
excluded) + a tile of the real fabric texture -> transplant the REAL logo/graphic pixels from the photo (deskewed
upright, scaled, placed at the right spot) -> template shading multiplied on top -> 1024 x 1024 white canvas.
Display only: embeddings keep using the original cutout.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from functools import lru_cache

from PIL import Image

from . import config, db
from .garment_schema import BATCH_PROMPT, BatchDetails

log = logging.getLogger("fitcheck.render_template")

# ------------------------------------------------------------------ details storage
_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS item_render_details (
    item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    details TEXT NOT NULL,
    frame TEXT NOT NULL,        -- 'crop' (boxes relative to the item crop) | 'photo' (relative to the whole photo)
    model TEXT,
    created_at TEXT NOT NULL
);
"""
_ready: set[str] = set()


def _ensure() -> None:
    k = str(config.DB_PATH)
    if k not in _ready:
        with db.get_conn() as c:
            c.executescript(_DETAILS_SQL)
        _ready.add(k)


def get_details(item: dict) -> tuple[dict | None, str | None]:
    """(details, frame). Stored batch details win; else details that came with the detection call."""
    try:
        _ensure()
        with db.get_conn() as c:
            row = c.execute("SELECT details, frame FROM item_render_details WHERE item_id=?", (item["id"],)).fetchone()
        if row:
            return json.loads(row["details"]), row["frame"]
    except Exception as e:
        log.warning("details lookup failed: %s", e)
    d = (item.get("attributes") or {}).get("details")
    if isinstance(d, dict) and d.get("garment_type"):
        return d, "photo"
    return None, None


def save_details(item_id: str, details: dict, frame: str, model: str | None = None) -> None:
    _ensure()
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO item_render_details(item_id, details, frame, model, created_at) "
                  "VALUES (?,?,?,?,?)", (item_id, json.dumps(details), frame, model, db.now_iso()))


def forget_details(item_id: str) -> None:
    _ensure()
    with db.get_conn() as c:
        c.execute("DELETE FROM item_render_details WHERE item_id=?", (item_id,))


def fetch_details(items: list[dict], force: bool = False) -> dict[str, dict]:
    """ONE batched Gemini vision call (text/vision model, no image generation) for all items lacking details.
    Returns {item_id: details}; {} if Gemini is unavailable (callers fall back to attribute heuristics)."""
    from . import gemini
    todo = [it for it in items if (force or get_details(it)[0] is None) and it.get("crop_path")
            and Path(it["crop_path"]).exists()]
    if not todo or not gemini.is_configured():
        return {}
    contents: list = [BATCH_PROMPT]
    for n, it in enumerate(todo, start=1):
        a = it.get("attributes") or {}
        contents.append(f"Item {n}: {it.get('label') or a.get('subcategory')} - {a.get('description') or ''}")
        with Image.open(it["crop_path"]) as im:
            contents.append(gemini._img_part(im.convert("RGB")))
    res: BatchDetails = gemini._generate(contents, BatchDetails, low_thinking=True)
    out = {}
    for d in res.items:
        if 1 <= d.index <= len(todo):
            it = todo[d.index - 1]
            dd = json.loads(d.model_dump_json())
            dd.pop("index", None)
            save_details(it["id"], dd, "crop", gemini._model_name)
            out[it["id"]] = dd
    log.info("Fetched garment details for %d/%d items in one call", len(out), len(todo))
    return out


# ------------------------------------------------------------------ template selection
import math
import re

import cv2
from PIL import ImageFilter
from scipy import ndimage

from . import garment_templates as gt

TEMPLATE_MIN_CONF = 0.5
_WORD = lambda w: re.compile(rf"\b{w}\b")


def _item_text(item: dict) -> str:
    a = item.get("attributes") or {}
    return " ".join(str(x) for x in (item.get("label"), a.get("subcategory"), a.get("category"), item.get("category"),
                                      a.get("description"), a.get("material")) if x).lower()


def _guess_type(text: str) -> tuple[str | None, float]:
    """Garment type from free text (no Gemini details). Returns (type, confidence)."""
    rules = [
        (r"\b(zip|zipper|zip-up|full-zip)\b.*\bhood|\bhood.*\b(zip|zipper|zip-up|full-zip)\b", "zip_hoodie", 0.8),
        (r"\bhood(ie|ed|y)?\b", "hoodie", 0.8),
        (r"\bpolo\b", "polo", 0.8),
        (r"\b(long[- ]sleeve|longsleeve)\b.*\b(tee|t-shirt|tshirt|top)\b", "longsleeve_tee", 0.7),
        (r"\b(t-shirt|tshirt|tee)\b", "tshirt", 0.8),
        (r"\b(sweater|sweatshirt|jumper|pullover|crewneck|knit top)\b", "sweater", 0.7),
        (r"\b(jeans|denim pants|denim trousers)\b", "jeans", 0.85),
        (r"\b(trousers|pants|chinos|slacks|joggers|sweatpants)\b", "trousers", 0.7),
        (r"\bshorts\b", "shorts", 0.75),
        (r"\bskirt\b", "skirt", 0.75),
        (r"\bdress\b", "dress", 0.6),
        (r"\b(track jacket|zip jacket|windbreaker|bomber)\b", "jacket", 0.55),
    ]
    for pat, name, conf in rules:
        if re.search(pat, text):
            return name, conf
    return None, 0.0


_PATTERNED = re.compile(r"\b(stripe[sd]?|striped|plaid|check(ed)?|tartan|gingham|floral|flower|camo|camouflage|polka|"
                        r"leopard|zebra|animal|tie[- ]dye|paisley|colou?r[- ]?block|houndstooth|argyle|cable|"
                        r"fair ?isle|sequin|lace|crochet|mesh|sheer|ruffle|tiered|pleated|distressed|ripped|"
                        r"all[- ]?over|two[- ]tone|raglan|contrast|patchwork|embellish\w*|studs?|destroyed|detroyed|worn|"
                        r"whiskered|acid[- ]wash|bleach\w*|ruched|velvet|leather|suede|fur|sherpa|faux|wide[- ]leg|palazzo|"
                        r"culottes?|flared?|bootcut|pleats?|sash|bow|belted|cargo|paperbag|karate|taffeta|satin|"
                        r"sequins?|metallic)\b")
_LOGO_WORDS = re.compile(r"logo|text|print|graphic|letter|slogan|embroider|word|lettering|script|number|monogram|"
                         r"patch|badge|emblem", re.I)
_COLOR_TOKENS = ("white|black|grey|gray|red|blue|navy|green|pink|brown|beige|tan|yellow|orange|purple|cream|"
                 "olive|khaki|burgundy|maroon|charcoal")
_MULTI_COLOR = re.compile(rf"\b({_COLOR_TOKENS})\b\s*(and|&|/|-|with)\s*(light |dark )?\b({_COLOR_TOKENS})\b")


def template_blocker(item: dict, details: dict | None) -> str | None:
    """Why a flat, single-colour template would misrepresent this item (-> cleanup fallback), else None."""
    a = item.get("attributes") or {}
    text = _item_text(item)
    pattern = str(a.get("pattern") or "").lower()
    if _PATTERNED.search(pattern) or _PATTERNED.search(text):
        return "patterned / textured fabric"
    if _MULTI_COLOR.search(text):
        return "multi-colour garment"
    if not details and (_LOGO_WORDS.search(" ".join([pattern, str(a.get("description") or ""),
                                                     str(item.get("label") or "")]))):
        return "graphic/logo present but no graphic locations"
    if re.search(r"\b(maxi|slip dress|gown|cowl|wrap dress|bodycon|one[- ]shoulder|strapless)\b", text):
        return "dress style not covered by templates"
    if details and str(details.get("length") or "").lower() in ("maxi",) and details.get("garment_type") == "dress":
        return "dress style not covered by templates"
    return None


def select_template(item: dict, details: dict | None) -> tuple[str, str, float] | None:
    """(template name, view, confidence) or None when no template fits (shoes, accessories, shirts, coats,
    patterned / multi-colour garments...)."""
    text = _item_text(item)
    if template_blocker(item, details):
        return None
    d = details or {}
    gtype = d.get("garment_type")
    conf = 0.9 if gtype else 0.0
    if not gtype:
        gtype, conf = _guess_type(text)
    if not gtype:
        return None
    sleeve = (d.get("sleeve_length") or "").lower()
    closure = (d.get("closure") or "").lower()
    hood = d.get("has_hood")
    if hood is None:
        hood = bool(re.search(r"\bhood", text))
    zipped = closure == "full_zip" or (not closure and bool(re.search(r"\b(zip|zipper|zip-up|full-zip)\b", text)))
    name = None
    if gtype == "tshirt":
        name = "longsleeve_tee" if sleeve == "long" else "tshirt"
    elif gtype == "longsleeve_tee":
        name = "longsleeve_tee"
    elif gtype in ("sweater", "sweatshirt"):
        name = ("zip_hoodie" if zipped else "hoodie") if hood else ("jacket" if zipped else "sweater")
    elif gtype == "polo":
        name = "polo"
    elif gtype == "hoodie":
        name = "zip_hoodie" if zipped else "hoodie"
    elif gtype == "zip_hoodie":
        name = "zip_hoodie" if hood or d.get("has_hood") is None else "jacket"
    elif gtype == "jacket":
        if zipped or closure in ("", "none", "half_zip"):
            name = "zip_hoodie" if hood else "jacket"
            conf = min(conf, 0.6)
    elif gtype in ("jeans", "trousers", "skirt"):
        name = gtype
    elif gtype == "shorts":
        name = "shorts"
    elif gtype == "dress":
        name = "dress" if sleeve in ("", "sleeveless", "none") else ("dress_short_sleeve" if sleeve == "short" else None)
    neck = (d.get("neckline") or "").lower()
    if not neck and re.search(r"\bv[- ]?neck", text):
        neck = "v_neck"
    if neck == "v_neck" and name in ("tshirt", "sweater"):
        name = name + "_vneck"
    elif neck == "v_neck" and name == "longsleeve_tee":
        name = "longsleeve_vneck"
    # the template family must agree with the item's category (e.g. "CK Jeans ... trainers" are shoes)
    cat = str(item.get("category") or (item.get("attributes") or {}).get("category") or "").lower()
    fam = {"jeans": "bottom", "trousers": "bottom", "shorts": "bottom", "skirt": "bottom", "dress": "dress",
           "dress_short_sleeve": "dress"}.get(name or "", "top")
    if cat and name and not (fam == cat or (fam == "top" and cat == "outerwear")):
        return None
    if name in ("hoodie", "zip_hoodie") and d.get("has_drawstrings") is False:
        name += "_nostrings"
    if name is None or name not in gt.TEMPLATE_NAMES:
        return None
    view = (d.get("view") or "front").lower()
    view = "back" if view == "back" else "front"
    return name, view, conf


# ------------------------------------------------------------------ colour helpers
def _lab(rgb):
    from .render import rgb_to_lab
    return rgb_to_lab(rgb)


def _rgb(lab):
    from .render import lab_to_rgb
    return lab_to_rgb(lab)


def srgb_to_lin(c):
    c = np.asarray(c, np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def lin_to_srgb(c):
    c = np.clip(np.asarray(c, np.float32), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055).astype(np.float32)


def _lab_lin(lab) -> np.ndarray:
    return srgb_to_lin(_rgb(np.asarray(lab, np.float64)).astype(np.float32) / 255.0)


NAMED_LAB = {
    "white": (95, 0, 1.5), "off-white": (92, 0.5, 6), "off white": (92, 0.5, 6), "cream": (91, 1, 10),
    "ivory": (93, 0, 8), "beige": (80, 3, 15), "tan": (68, 8, 24), "khaki": (66, 2, 22), "grey": (62, 0, 0),
    "gray": (62, 0, 0), "light grey": (78, 0, 0), "heather grey": (68, 0, 0), "charcoal": (32, 0, 0),
    "black": (15, 0, 0), "brown": (38, 10, 20), "navy": (22, 4, -22), "blue": (45, 5, -40), "red": (45, 60, 40),
    "pink": (75, 25, 2), "green": (50, -35, 25), "silver": (80, 0, -1), "gold": (70, 5, 45), "brass": (66, 6, 40),
    "copper": (55, 20, 30), "gunmetal": (40, 0, -2), "bronze": (50, 10, 28),
    "light blue": (76, -3, -16), "light wash": (76, -3, -16), "sky blue": (75, -6, -22), "baby blue": (80, -4, -14),
    "dark blue": (30, 3, -25), "indigo": (30, 5, -28), "olive": (48, -8, 26), "burgundy": (30, 35, 12),
    "maroon": (28, 30, 12), "purple": (38, 35, -35), "yellow": (85, 0, 70), "orange": (65, 40, 60),
}


def named_lab(name: str | None):
    if not name:
        return None
    s = name.lower().strip()
    if s in NAMED_LAB:
        return np.array(NAMED_LAB[s], float)
    for k in sorted(NAMED_LAB, key=len, reverse=True):
        if re.search(rf"\b{k}\b", s):
            return np.array(NAMED_LAB[k], float)
    return None


def sample_fabric(arr: np.ndarray, fg: np.ndarray, exclude: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Robust fabric colour: dominant LAB cluster of eroded garment pixels (logos excluded), then the median of its
    mid-lightness pixels (drops folds' shadows and specular highlights)."""
    from .render import dominant_lab
    er = max(1, int(round(max(fg.shape) / 160)))
    m = ndimage.binary_erosion(fg, iterations=er) if fg.sum() > 4000 else fg.copy()
    if exclude is not None:
        m &= ~exclude
    if m.sum() < 200:
        m = fg
    px = arr[m]
    if len(px) > 60000:
        px = px[np.linspace(0, len(px) - 1, 60000).astype(int)]
    lab = _lab(px)
    dom, clusters = dominant_lab(px, k=3)
    d = np.sqrt(((lab - dom) ** 2).sum(1))
    near = lab[d <= max(12.0, np.percentile(d, 55))]
    L = near[:, 0]
    lo, hi = np.percentile(L, 20), np.percentile(L, 85)
    core = near[(L >= lo) & (L <= hi)]
    med = np.median(core if len(core) > 50 else near, axis=0)
    return med, {"fabric_lab": [round(float(x), 1) for x in med], "clusters": clusters, "n_px": int(len(px))}


def _color_words(item: dict) -> str:
    a = item.get("attributes") or {}
    cols = a.get("colors") or a.get("color") or ""
    if isinstance(cols, list):
        cols = " ".join(map(str, cols))
    return f"{item.get('label') or ''} {cols}".lower()


def normalize_fabric(lab: np.ndarray, item: dict) -> tuple[np.ndarray, dict]:
    """Studio exposure: a garment named white/cream reads as clean white (the photo's grey/green cast removed);
    everything else keeps its measured colour with a small lift (catalogue lighting is brighter than phones)."""
    lab = np.asarray(lab, float).copy()
    words = _color_words(item)
    info = {}
    if re.search(r"\b(white|off-white|cream|ivory)\b", words) and lab[0] > 55 and math.hypot(lab[1], lab[2]) < 20:
        tgt = named_lab("cream" if re.search(r"\b(cream|ivory|off-white)\b", words) else "white")
        info["exposure"] = "white_snap"
        gain = tgt[0] - lab[0]
        lab = np.array([tgt[0], lab[1] * 0.25 + tgt[1] * 0.75, lab[2] * 0.25 + tgt[2] * 0.75])
        info["L_gain"] = round(float(gain), 1)
        return lab, info
    if re.search(r"\bblack\b", words) and lab[0] < 35:
        info["exposure"] = "black"
        lab[0] = min(max(lab[0], 14.0), 24.0)
        lab[1:] *= 0.6
        info["L_gain"] = 0.0
        return lab, info
    # colour-name prior: a near-neutral measurement of a garment the user/Gemini calls e.g. "light blue" is a colour
    # cast (warm sunlight, wooden floor): pull the hue towards the named colour, keep the measured lightness
    named = None
    for k in sorted(NAMED_LAB, key=len, reverse=True):
        if k not in ("silver", "gold", "brass", "copper", "gunmetal", "bronze") and re.search(rf"\b{k}\b", words):
            named = np.array(NAMED_LAB[k], float)
            info["named"] = k
            break
    chroma = math.hypot(lab[1], lab[2])
    if named is not None and math.hypot(named[1], named[2]) < 3 and chroma < 14:
        lab[1:] *= 0.3  # "grey"/"charcoal" garment under warm light
        info["exposure"] = "name_prior"
    elif named is not None and chroma < 10 and math.hypot(named[1], named[2]) > 10:
        tgt_ab = named[1:] * min(1.0, 13.0 / math.hypot(named[1], named[2]))
        lab[1:] = 0.35 * lab[1:] + 0.65 * tgt_ab
        info["exposure"] = "name_prior"
    if re.search(r"\b(denim|jeans?)\b", _item_text(item)) and lab[0] > 80:
        info["denim_L_cap"] = round(float(lab[0]), 1)
        lab[0] = 80.0  # an over-exposed phone photo of light-wash denim
    lift = float(np.clip((80 - lab[0]) * 0.06, 0, 3))
    lab[0] += lift
    info.setdefault("exposure", "lift")
    info["L_gain"] = round(lift, 1)
    return lab, info


# ------------------------------------------------------------------ procedural fabric textures
@lru_cache(maxsize=8)
def fabric_texture(kind: str) -> np.ndarray:
    """Multiplicative micro-texture around 1.0 (S x S), deterministic."""
    S = gt.S
    rng = np.random.default_rng({"jersey": 1, "fleece": 2, "pique": 3, "denim": 4, "woven": 5, "knit": 6}.get(kind, 7))
    yy, xx = np.mgrid[:S, :S].astype(np.float32)
    noise = ndimage.gaussian_filter(rng.standard_normal((S, S)).astype(np.float32), 0.7)
    noise /= noise.std() + 1e-6
    if kind == "denim":
        twill = np.sin(2 * np.pi * (xx * 0.45 + yy * 0.9) / 4.2)
        warp = ndimage.gaussian_filter(rng.standard_normal((1, S)).astype(np.float32), (0, 0.8))
        warp = np.repeat(warp / (warp.std() + 1e-6), S, 0)
        slub = ndimage.gaussian_filter(rng.standard_normal((S, S)).astype(np.float32), (40, 1.2))
        slub /= slub.std() + 1e-6
        t = 1 + 0.045 * twill + 0.035 * warp + 0.03 * slub + 0.025 * noise
    elif kind == "fleece":
        soft = ndimage.gaussian_filter(rng.standard_normal((S, S)).astype(np.float32), 2.0)
        soft /= soft.std() + 1e-6
        t = 1 + 0.018 * noise + 0.012 * soft
    elif kind == "pique":
        cell = np.sin(2 * np.pi * xx / 4.0) * np.sin(2 * np.pi * yy / 4.0)
        t = 1 + 0.03 * cell + 0.018 * noise
    elif kind == "knit":
        wale = np.abs(np.sin(np.pi * xx / 5.0))
        t = 1 + 0.04 * (wale - 0.6) + 0.02 * noise
    else:  # jersey / woven
        wale = np.sin(2 * np.pi * xx / 3.0)
        t = 1 + 0.012 * wale + 0.02 * noise
    return t.astype(np.float32)


def _fabric_kind(tname: str, item: dict) -> str:
    text = _item_text(item)
    if tname in ("jeans",) or re.search(r"\b(denim|jean)", text):
        return "denim"
    if tname.startswith(("hoodie", "zip_hoodie", "sweater")) and not re.search(r"\bknit\b", text):
        return "fleece"
    if re.search(r"\b(knit|cable)\b", text):
        return "knit"
    if tname == "polo":
        return "pique"
    if tname in ("trousers", "skirt", "dress", "dress_short_sleeve", "jacket", "shorts"):
        return "woven"
    return "jersey"


# ------------------------------------------------------------------ geometry: boxes / cutout offset
def cutout_offset(crop: Image.Image | None, rgb: Image.Image) -> tuple[int, int]:
    """Where the (tightly cropped) segformer cutout sits inside the item crop (pixels are identical)."""
    if crop is None or crop.size == rgb.size:
        return 0, 0
    c = np.asarray(crop.convert("L"), np.float32)
    r = np.asarray(rgb.convert("L"), np.float32)
    if r.shape[0] > c.shape[0] or r.shape[1] > c.shape[1]:
        return 0, 0
    res = cv2.matchTemplate(c, r, cv2.TM_SQDIFF)
    _, _, loc, _ = cv2.minMaxLoc(res)
    return int(loc[0]), int(loc[1])


def graphic_box_px(g: dict, frame: str | None, item: dict, crop_size: tuple[int, int],
                   offset: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Graphic box (details) -> pixel box in the prepared-garment frame."""
    b = g.get("box_2d") or []
    if len(b) != 4:
        return None
    y0, x0, y1, x1 = [float(v) for v in b]
    if y1 <= y0 or x1 <= x0:
        return None
    cw, ch = crop_size
    if frame == "photo":
        try:
            from .pipeline import _box_px
            with db.get_conn() as c:
                row = c.execute("SELECT width, height FROM photos WHERE id=?", (item.get("photo_id"),)).fetchone()
            W, H = int(row["width"]), int(row["height"])
            bx0, by0, bx1, by1 = _box_px(item["bbox"], W, H, config.CROP_PAD_FRAC)
            X0, X1 = x0 / 1000 * W - bx0, x1 / 1000 * W - bx0
            Y0, Y1 = y0 / 1000 * H - by0, y1 / 1000 * H - by0
        except Exception:
            return None
    else:
        X0, X1, Y0, Y1 = x0 / 1000 * cw, x1 / 1000 * cw, y0 / 1000 * ch, y1 / 1000 * ch
    ox, oy = offset
    return int(round(X0 - ox)), int(round(Y0 - oy)), int(round(X1 - ox)), int(round(Y1 - oy))


# ------------------------------------------------------------------ logo / graphic transplant
def _rotate_float(a: np.ndarray, deg_cw: float, border=0.0) -> np.ndarray:
    """Rotate an HxW(xC) float array clockwise by deg (expanding the canvas)."""
    if abs(deg_cw) < 0.5:
        return a
    h, w = a.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), -deg_cw, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(math.ceil(h * sin + w * cos)), int(math.ceil(h * cos + w * sin))
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(a.astype(np.float32), M, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=border)


def stroke_orientation(L: np.ndarray, weight: np.ndarray) -> tuple[float | None, float]:
    """Dominant straight-edge direction of a mark, modulo 90 deg. Returns (rotate_cw in (-45, 45] that makes those
    edges axis-aligned, peak strength); (None, s) when there is no clear dominant direction (round/organic marks)."""
    gx = cv2.Sobel(L.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(L.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy) * weight
    if mag.sum() <= 1e-6:
        return None, 0.0
    ang = (np.degrees(np.arctan2(gy, gx)) % 90.0)
    hist, _ = np.histogram(ang, bins=90, range=(0, 90), weights=mag)
    k = np.exp(-0.5 * (np.arange(-6, 7) / 2.0) ** 2)
    hs = np.convolve(np.concatenate([hist[-6:], hist, hist[:6]]), k / k.sum(), mode="same")[6:-6]
    peak = int(hs.argmax())
    strength = float(hs[peak] / max(1e-6, hs.mean()))
    l, r = hs[(peak - 1) % 90], hs[(peak + 1) % 90]
    den = l - 2 * hs[peak] + r
    theta = peak + 0.5 + (0.5 * (l - r) / den if abs(den) > 1e-9 else 0.0)  # edge direction mod 90 (cw-positive)
    if theta > 45:
        theta -= 90
    if strength < 1.45:
        return None, round(strength, 2)
    return -theta, round(strength, 2)


def _norm_blur(x: np.ndarray, m: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of x using only pixels where m (normalised convolution)."""
    m = m.astype(np.float32)
    num = ndimage.gaussian_filter(x * m, sigma)
    den = ndimage.gaussian_filter(m, sigma)
    return num / np.maximum(den, 1e-4)


TONAL_CONTRAST = 22.0


def extract_graphic(lab_img: np.ndarray, fg: np.ndarray, box: tuple[int, int, int, int], fabric_lab: np.ndarray,
                    rotate_cw: float = 0.0, allow_tonal: bool = True) -> dict | None:
    """Real graphic pixels as a LAB *deviation from the surrounding fabric* + soft alpha, deskewed upright.
    Deviation transfer keeps the logo's true colours on the recoloured template while dropping the photo's local
    lighting (shadows over the box). Tonal marks (white-on-white embroidery) transfer as high-pass relief."""
    H, W = fg.shape
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if bw < 4 or bh < 4:
        return None
    mx, my = int(bw * 0.15) + 3, int(bh * 0.15) + 3
    X0, Y0, X1, Y1 = max(0, x0 - mx), max(0, y0 - my), min(W, x1 + mx), min(H, y1 + my)
    if X1 - X0 < 4 or Y1 - Y0 < 4:
        return None
    sub = lab_img[Y0:Y1, X0:X1].astype(np.float32)
    sfg = fg[Y0:Y1, X0:X1]
    efg = ndimage.binary_erosion(sfg, iterations=2)
    tight = np.zeros(sfg.shape, bool)
    tight[max(0, y0 - Y0):y1 - Y0, max(0, x0 - X0):x1 - X0] = True
    ring = efg & ~tight
    if ring.sum() > 30:
        rp = sub[ring]
        dd = np.sqrt(((rp - fabric_lab) ** 2).sum(1))
        close = rp[dd <= max(10.0, np.percentile(dd, 50))]
        local = np.median(close, 0) if len(close) > 10 else np.asarray(fabric_lab, np.float32)
    else:
        local = np.asarray(fabric_lab, np.float32)
    dev = sub - local
    mag = np.sqrt((0.8 * dev[..., 0]) ** 2 + dev[..., 1] ** 2 + dev[..., 2] ** 2)
    inside = tight & efg
    strength = float(np.percentile(mag[inside], 95)) if inside.sum() > 10 else 0.0
    win = ndimage.gaussian_filter(ndimage.binary_dilation(tight, iterations=max(2, int(0.06 * max(bw, bh))))
                                  .astype(np.float32), max(1.0, 0.03 * max(bw, bh)))
    tonal = strength < TONAL_CONTRAST
    if tonal:
        if not allow_tonal or strength < 3:
            return None
        # relief only: lightness high-pass inside the garment, chroma dropped
        sig = max(2.0, 0.12 * min(bw, bh))
        Lhp = dev[..., 0] - _norm_blur(dev[..., 0], efg, sig)
        dev = np.dstack([np.clip(Lhp * 1.25, -30, 30), np.zeros_like(Lhp), np.zeros_like(Lhp)])
        a = win * ndimage.gaussian_filter(efg.astype(np.float32), 1.0) * ndimage.gaussian_filter(
            tight.astype(np.float32), max(1.0, 0.05 * min(bw, bh)))
        a = np.clip(a * 1.4, 0, 1)
        cover = float(inside.mean())
        orient_src, orient_w = Lhp, (a > 0.5).astype(np.float32)
    else:
        a = np.clip((mag - 5.0) / 10.0, 0, 1)
        a = a * a * (3 - 2 * a)
        a *= win * efg
        hard = ndimage.binary_closing(a > 0.35, iterations=2)
        lbl, n = ndimage.label(hard)
        if n:
            # background leaking through the garment mask sits on the mask boundary; real graphics don't
            er = max(4, int(0.012 * max(fg.shape)))
            near_edge = (fg & ~ndimage.binary_erosion(fg, iterations=er))[Y0:Y1, X0:X1]
            idx = range(1, n + 1)
            sizes = ndimage.sum(hard, lbl, idx)
            edge_frac = ndimage.sum(near_edge, lbl, idx) / np.maximum(sizes, 1)
            ok = [i + 1 for i, (s_, e_) in enumerate(zip(sizes, edge_frac)) if e_ < 0.25]
            if ok:
                big = max(sizes[i - 1] for i in ok)
                keep = np.isin(lbl, [i for i in ok if sizes[i - 1] >= max(6, 0.02 * big)])
                a *= ndimage.binary_dilation(keep, iterations=2)
            else:
                return None
        cover = float((a > 0.5).sum()) / max(1, tight.sum())
        if cover < 0.01:
            return None
        a = ndimage.gaussian_filter(a, 0.6)
        orient_src, orient_w = dev[..., 0], ndimage.binary_dilation(a > 0.3, iterations=2).astype(np.float32)
    # deskew: 90-degree steps from Gemini (sideways / upside-down marks), the fine angle from the pixels
    coarse = 90.0 * round(float(rotate_cw or 0.0) / 90.0)
    fine, ostr = stroke_orientation(orient_src, orient_w)
    if fine is None:
        ang, how = (float(rotate_cw or 0.0) if abs(float(rotate_cw or 0.0) - coarse) <= 20 else coarse), "gemini"
    else:
        ang, how = coarse + fine, "strokes"
    dev_r = np.dstack([_rotate_float(dev[..., i], ang) for i in range(3)]) if abs(ang) >= 0.5 else dev
    a_r = np.clip(_rotate_float(a, ang), 0, 1)
    ys, xs = np.nonzero(a_r > 0.08)
    if len(xs) == 0:
        return None
    cy0, cy1, cx0, cx1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return {"dev": dev_r[cy0:cy1, cx0:cx1], "alpha": a_r[cy0:cy1, cx0:cx1], "angle_cw": round(ang, 1),
            "angle_source": how, "orient_strength": ostr, "tonal": tonal,
            "local_lab": [round(float(v), 1) for v in local], "coverage": round(cover, 3),
            "contrast": round(strength, 1)}


def _garment_width(fg: np.ndarray, rotate_cw: float) -> float:
    m = fg.astype(np.float32)
    if abs(rotate_cw) >= 0.5:
        m = _rotate_float(m, rotate_cw)
    xs = np.nonzero((m > 0.5).any(0))[0]
    return float(xs.max() - xs.min() + 1) if len(xs) else float(fg.shape[1])


def place_graphic(t: gt.Template, g: dict, gr: dict, box: tuple, fg: np.ndarray, rgb_bbox: tuple) -> dict | None:
    """Scale the deskewed graphic proportionally to the garment and anchor it on the template.
    Returns {'dev': S x S x 3, 'alpha': S x S, 'box': (x0, y0, x1, y1)} in template pixels."""
    S = gt.S
    pos = (g.get("position") or "").lower()
    tb = t.bbox
    tw = tb[2] - tb[0]
    if pos in t.anchors:
        cx, cy, max_w = t.anchors[pos]
    else:  # relative placement: map the box centre inside the garment bbox onto the template bbox
        gx0, gy0, gx1, gy1 = rgb_bbox
        rx = ((box[0] + box[2]) / 2 - gx0) / max(1, gx1 - gx0)
        ry = ((box[1] + box[3]) / 2 - gy0) / max(1, gy1 - gy0)
        cx, cy, max_w = tb[0] + rx * tw, tb[1] + ry * (tb[3] - tb[1]), 0.3 * tw
        pos = "relative"
    gh, gw = gr["alpha"].shape
    garment_w = _garment_width(fg, gr["angle_cw"])
    target_w = gw / max(1.0, garment_w) * tw
    target_w = float(np.clip(target_w, 0.35 * max_w, max_w))
    s = target_w / max(1, gw)
    nw, nh = max(2, int(round(gw * s))), max(2, int(round(gh * s)))
    if nh > 1.3 * max_w * 1.2:  # very tall marks: cap the height too
        s2 = (1.3 * max_w * 1.2) / nh
        nw, nh = max(2, int(nw * s2)), max(2, int(nh * s2))
    dev = cv2.resize(gr["dev"].astype(np.float32), (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    a = np.clip(cv2.resize(gr["alpha"].astype(np.float32), (nw, nh), interpolation=cv2.INTER_AREA if s < 1
                           else cv2.INTER_LINEAR), 0, 1)
    x0, y0 = int(round(cx - nw / 2)), int(round(cy - nh / 2))
    D = np.zeros((S, S, 3), np.float32)
    A = np.zeros((S, S), np.float32)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(S, x0 + nw), min(S, y0 + nh)
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    D[sy0:sy1, sx0:sx1] = dev[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
    A[sy0:sy1, sx0:sx1] = a[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
    A *= np.clip(t.mask - t.inner, 0, 1)
    return {"dev": D, "alpha": A, "box": (sx0, sy0, sx1, sy1), "position": pos, "scale": round(s, 3)}


# ------------------------------------------------------------------ compositing
_HW_DEFAULT = {"metal": "silver", "eyelet": "silver", "aglet": "silver"}


def composite(t: gt.Template, base_lab: np.ndarray, kind: str, placed: list[dict], lining_lab=None,
              stitch_lab=None, hardware_lab=None, string_lab=None, button_lab=None) -> np.ndarray:
    """Recolour the template (linear light): albedo = colour x micro-texture, logos as real-pixel deviations,
    stitches/hardware on top, then the template's shading multiplied and highlights screened. -> S x S x 3 uint8."""
    S = gt.S
    base_lab = np.asarray(base_lab, float)
    tex = fabric_texture(kind)
    base_lin = _lab_lin(base_lab)
    alb = np.empty((S, S, 3), np.float32)
    alb[:] = base_lin
    if t.denim and t.fade is not None:
        faded = base_lab + np.array([9.0, 0.0, 2.5])
        faded[1:] *= 0.85
        f = t.fade[..., None]
        alb = alb * (1 - f) + _lab_lin(faded) * f
    # rib areas: slightly deeper colour
    alb *= (1 - 0.04 * t.rib)[..., None]
    alb *= tex[..., None]
    if lining_lab is not None:
        li = t.inner[..., None]
        alb = alb * (1 - li) + (_lab_lin(lining_lab) * fabric_texture("jersey")[..., None]) * li
    # logos (real pixels): base colour + measured deviation
    la = np.zeros((S, S), np.float32)
    for p in placed:
        logo_lab = base_lab[None, None, :] + p["dev"].astype(np.float64)
        x0, y0, x1, y1 = p["box"]
        sub = _lab_lin(logo_lab[y0:y1, x0:x1])
        a = p["alpha"][y0:y1, x0:x1, None]
        alb[y0:y1, x0:x1] = alb[y0:y1, x0:x1] * (1 - a) + sub * a
        la = np.maximum(la, p["alpha"])
    # stitches
    st_lin = _lab_lin(stitch_lab) if stitch_lab is not None else None
    for a, k in t.stitches:
        if k == "contrast" and st_lin is not None:
            alb = alb * (1 - 0.85 * a[..., None]) + st_lin * (0.85 * a[..., None])
        else:
            alb *= (1 - 0.30 * a)[..., None]
    # hardware
    hw_all = np.zeros((S, S), np.float32)
    for a, k, _ in t.hardware:
        if k in ("metal", "eyelet", "aglet"):
            c = hardware_lab if hardware_lab is not None else named_lab(_HW_DEFAULT[k])
            col = _lab_lin(c)
            # simple metallic look: lighter top, darker bottom inside the part
            dd = ndimage.distance_transform_edt(a > 0.5).astype(np.float32)
            rim = np.clip(1 - dd / 3.0, 0, 1)
            colmap = col * (1 - 0.35 * rim)[..., None]
        elif k == "button":
            c = button_lab if button_lab is not None else base_lab + np.array([2.0, 0, 0])
            dd = ndimage.distance_transform_edt(a > 0.5).astype(np.float32)
            rim = np.clip(1 - dd / 2.5, 0, 1)
            colmap = _lab_lin(c) * (1 - 0.25 * rim)[..., None]
            # two thread holes
            ys, xs = np.nonzero(a > 0.5)
            if len(xs):
                cx, cy = xs.mean(), ys.mean()
                yy, xx = np.ogrid[:S, :S]
                holes = np.zeros((S, S), np.float32)
                for dx in (-2.2, 2.2):
                    holes = np.maximum(holes, np.exp(-(((xx - cx - dx * gt.K) ** 2 + (yy - cy) ** 2) / (1.1 * gt.K) ** 2)))
                colmap = colmap * (1 - 0.45 * holes)[..., None]
        elif k == "string":
            c = string_lab if string_lab is not None else base_lab + np.array([4.0, 0, 0])
            colmap = _lab_lin(c) * fabric_texture("jersey")[..., None]
        elif k == "zip_tape":
            colmap = alb * 0.72
        else:
            colmap = alb * 0.9
        alb = alb * (1 - a[..., None]) + colmap * a[..., None]
        if k != "zip_tape":
            hw_all = np.maximum(hw_all, a)
    shade = t.shade.copy()
    # hardware casts a tiny contact shadow
    if hw_all.any():
        sh = ndimage.gaussian_filter(hw_all, 1.6 * gt.K)
        shade *= 1 - 0.35 * np.clip(sh - hw_all, 0, 1)
    shade = np.power(np.clip(shade, 1e-3, None), 1 - 0.25 * la)
    lin = alb * shade[..., None]
    srgb = lin_to_srgb(np.clip(lin, 0, 1))
    # highlights: screen in perceptual (sRGB) space so dark fabrics get a soft sheen, not a plastic streak
    srgb = 1 - (1 - srgb) * (1 - 0.30 * t.light[..., None])
    return np.clip(srgb * 255 + 0.5, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ top level
def render_template(item: dict, rgb: Image.Image, fg: np.ndarray, crop: Image.Image | None = None,
                    details: dict | None = None, frame: str | None = None,
                    size: int | None = None) -> tuple[Image.Image | None, dict]:
    """Returns (image, info). image is None when the item type is unsupported / confidence too low / the sanity
    colour check fails (caller falls back to the cleanup render)."""
    from . import render as R
    size = size or R.RENDER_SIZE
    info: dict = {"method": "template"}
    if details is None:
        details, frame = get_details(item)
    blk = template_blocker(item, details)
    sel = None if blk else select_template(item, details)
    if sel is None:
        info["skipped"] = blk or "no template for this garment type"
        return None, info
    tname, view, conf = sel
    info.update(template=tname, view=view, confidence=conf, details_source=frame or "heuristic")
    if conf < TEMPLATE_MIN_CONF:
        info["skipped"] = "low confidence"
        return None, info
    if fg.sum() < 500:
        info["skipped"] = "no garment mask"
        return None, info
    t = gt.get_template(tname, view)
    arr = np.asarray(rgb.convert("RGB")).copy()
    arr, _wb = R._white_balance(arr, fg)
    lab_img = _lab(arr).astype(np.float32)
    off = cutout_offset(crop, rgb)
    crop_size = crop.size if crop is not None else rgb.size
    graphics = [g for g in (details or {}).get("graphics") or [] if isinstance(g, dict)]
    boxes = []
    excl = np.zeros(fg.shape, bool)
    H, W = fg.shape
    for g in graphics:
        b = graphic_box_px(g, frame, item, crop_size, off)
        if b is None:
            continue
        x0, y0, x1, y1 = max(0, b[0]), max(0, b[1]), min(W, b[2]), min(H, b[3])
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        boxes.append((g, (x0, y0, x1, y1)))
        px, py = int(0.2 * (x1 - x0)) + 2, int(0.2 * (y1 - y0)) + 2
        excl[max(0, y0 - py):y1 + py, max(0, x0 - px):x1 + px] = True
    fab_raw, finfo = sample_fabric(arr, fg, excl)
    fab, einfo = normalize_fabric(fab_raw, item)
    info.update(fabric_raw_lab=finfo["fabric_lab"], fabric_lab=[round(float(v), 1) for v in fab], exposure=einfo)
    kind = _fabric_kind(tname, item)
    info["fabric"] = kind
    # graphics
    ys, xs = np.nonzero(fg)
    gb = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    placed, ginfo = [], []
    L_gain = float(einfo.get("L_gain") or 0.0)
    for g, b in boxes:
        pos = (g.get("position") or "").lower()
        if str(g.get("kind") or "").lower() == "label" and pos in ("other", "", "hem"):
            ginfo.append({"text": g.get("text"), "skipped": "inside/care label"}); continue
        if view == "front" and pos in ("upper_back", "back_center", "back_waistband_right", "back_waistband_left",
                                       "back_pocket"):
            ginfo.append({"text": g.get("text"), "skipped": "on the other side"}); continue
        gr = extract_graphic(lab_img, fg, b, fab_raw, g.get("rotate_cw_deg") or 0,
                             allow_tonal=pos not in ("sleeve", "hem", "other", ""))
        if gr is None:
            ginfo.append({"text": g.get("text"), "position": pos, "skipped": "too faint / tonal"}); continue
        # the logo gets the same exposure change as the fabric: dev is relative, but a white snap compresses
        # the headroom -> scale lightness deviations to fit
        if einfo.get("exposure") == "white_snap":
            gr["dev"][..., 0] *= float(np.clip((100 - fab[0]) / max(1.0, 100 - fab_raw[0]), 0.4, 1.0)) \
                if np.median(gr["dev"][..., 0][gr["alpha"] > 0.5] if (gr["alpha"] > 0.5).any() else [0]) > 0 else 1.0
        p = place_graphic(t, g, gr, b, fg, gb)
        if p is None:
            ginfo.append({"text": g.get("text"), "skipped": "placement failed"}); continue
        placed.append(p)
        ginfo.append({"text": g.get("text"), "kind": g.get("kind"), "position": p["position"],
                      "angle_cw": gr["angle_cw"], "angle_source": gr["angle_source"], "tonal": gr["tonal"],
                      "scale": p["scale"],
                      "box": list(map(int, p["box"])), "contrast": gr["contrast"]})
    info["graphics"] = ginfo
    # secondary colours
    d = details or {}
    lining = None
    hooded = tname.startswith(("hoodie", "zip_hoodie"))
    if t.inner.any() and hooded:
        want = named_lab(d.get("lining_color"))
        if want is not None:
            lining = want
            try:  # prefer the measured lining colour: the photo cluster nearest the named colour
                for c in finfo["clusters"]:
                    if R.color_distance(c["lab"], want) < 22 and R.color_distance(c["lab"], fab_raw) > 12:
                        lining = np.array(c["lab"], float)
                        lining[0] = max(lining[0], want[0] - 6)
                        break
            except Exception:
                pass
        else:
            lining = fab.copy()
    stitch = named_lab(d.get("stitch_color")) if d.get("stitch_color") else None
    if stitch is None and t.denim:
        stitch = named_lab("gold") + np.array([-4.0, 4.0, -8.0])
    hardware = named_lab(d.get("hardware_color")) if d.get("hardware_color") and d.get("hardware_color") != "tonal" else None
    if hardware is None and t.denim:
        hardware = named_lab("copper") + np.array([10, -6, 0])
    button = named_lab(d.get("hardware_color")) if tname == "polo" and d.get("hardware_color") else None
    if button is not None and fab[0] > 80:
        button = np.array([min(97, fab[0] + 2), fab[1], fab[2]])
    string = lining if lining is not None and hooded and d.get("lining_color") else None
    img = composite(t, fab, kind, placed, lining_lab=lining, stitch_lab=stitch, hardware_lab=hardware,
                    string_lab=string, button_lab=button)
    alpha = Image.fromarray((np.clip(t.mask, 0, 1) * 255).astype(np.uint8))
    out = R.compose_square(Image.fromarray(img), alpha, size=size)
    # sanity: the render's dominant colour must match the photo's garment colour
    ref = arr[fg & ~excl] if (fg & ~excl).sum() > 200 else arr[fg]
    chk = R.color_check(ref, out)
    info["color_check"] = chk
    thr = R.RENDER_COLOR_MAX_DE + (10.0 if einfo.get("exposure") in ("white_snap", "name_prior") else 0.0)
    if chk["distance"] > thr:
        info["skipped"] = f"colour check failed ({chk['distance']} > {thr})"
        return None, info
    info["size"] = size
    return out, info
