"""Clean, polished product images for closet items ("renders").

After an item is saved to the closet, a background task produces an isolated product photo:

  Option 1 (Gemini image redraw, ``method='gemini'``): the segmented garment + the original photo crop go to a Gemini
  image model with a strict "same garment, e-commerce product photo" prompt. The render is then VERIFIED:
    (a) fashion-clip image-image cosine(render, deterministic cleanup of the cutout) >= RENDER_CLIP_MIN
    (b) dominant garment colour (LAB k-means) distance <= RENDER_COLOR_MAX_DE
    (c) one Gemini *vision* call on a different (flash-lite) model comparing photo vs render ->
        {same_garment, has_text_or_logo, text_matches, color_matches, added_elements, issues}
  If a check fails it retries once with the issues appended (only while quota allows), else falls back to option 2.
  Image generation auto-disables on quota / permission errors (persisted in DATA_DIR/render_state.json).

  Option 2 (deterministic cleanup, ``method='cleanup'``, no generative AI): refined segformer mask (or GrabCut when the
  cutout had no mask), small-tilt correction, gentle white balance / tone normalisation, subtle edge-preserving
  smoothing, centred on a square white canvas with a soft drop shadow. Pixels are never synthesised, so logos/text
  are kept as photographed.

Results live in the ``item_renders`` table (created here) + ``media/clean/``. Nothing here may break adding an item:
every entry point catches its own errors.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel, Field
from scipy import ndimage

from . import config, db

log = logging.getLogger("fitcheck.render")

# ------------------------------------------------------------------ configuration (env-overridable)
def _env_list(name: str, default: str) -> list[str]:
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


# Background renders after add-to-closet. Off by default under pytest (other tests add items to the closet; their
# background renders would write into the shared test-data media dir); tests/test_render.py turns it on.
RENDER_ENABLED = os.environ.get("RENDER_ENABLED", "0" if "pytest" in __import__("sys").modules else "1") != "0"
# auto = try Gemini image generation until a quota/permission error auto-disables it; off = cleanup only;
# on = like auto but ignores a persisted auto-disable (still stops after errors within this process).
RENDER_GEMINI = os.environ.get("RENDER_GEMINI", "auto").strip().lower()
RENDER_MODEL = os.environ.get("RENDER_MODEL", "gemini-3.1-flash-image")
RENDER_FALLBACK_MODELS = _env_list("RENDER_FALLBACK_MODELS", "gemini-2.5-flash-image,gemini-3.1-flash-image-preview")
RENDER_MAX_MODELS = int(os.environ.get("RENDER_MAX_MODELS", "2"))  # image models tried per item before giving up
# Verification vision call: a different (lite) text model so it doesn't eat the detection model's free-tier quota.
RENDER_VERIFY_MODELS = _env_list("RENDER_VERIFY_MODELS", "gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-flash-lite-latest")
RENDER_VERIFY_GEMINI = os.environ.get("RENDER_VERIFY_GEMINI", "always").strip().lower()  # always | text | off
# Tuned on 49 closet items (40 Polyvore seeds + 9 floor-photo cutouts): cosine(cleanup, own original) min 0.74 /
# median 0.875; same-category DIFFERENT items p95 0.66, p99 0.72, max 0.81. So clip is a coarse gate (wrong
# item type / garbage); identity + logo/text fidelity is the Gemini vision check's job.
RENDER_CLIP_MIN = float(os.environ.get("RENDER_CLIP_MIN", "0.75"))
# Lightness-discounted LAB distance of dominant colours: same item max 8.8, different items median 26.8.
RENDER_COLOR_MAX_DE = float(os.environ.get("RENDER_COLOR_MAX_DE", "15"))
RENDER_SIZE = int(os.environ.get("RENDER_SIZE", "1024"))
RENDER_PAD_FRAC = float(os.environ.get("RENDER_PAD_FRAC", "0.08"))
RENDER_SHADOW = os.environ.get("RENDER_SHADOW", "1") != "0"
RENDER_MAX_TILT = float(os.environ.get("RENDER_MAX_TILT", "30"))
# Use the clean image for fashion-clip / OutfitTransformer embeddings only for verified Gemini renders
# (OutfitTransformer was trained on clean Polyvore product shots; a deterministic cleanup is the same pixels).
RENDER_EMBED_FROM_CLEAN = os.environ.get("RENDER_EMBED_FROM_CLEAN", "1") != "0"
SYNC = os.environ.get("RENDER_SYNC") == "1"  # run renders inline (tests)

CLEAN_SUBDIR = "clean"


def clean_dir() -> Path:
    d = config.MEDIA_DIR / CLEAN_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return Path(os.environ.get("RENDER_STATE_PATH", config.DATA_DIR / "render_state.json"))


# ------------------------------------------------------------------ storage
_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS item_renders (
    item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    status TEXT NOT NULL,          -- pending | done | failed
    method TEXT,                   -- gemini | cleanup
    clean_path TEXT,
    model TEXT,
    checks TEXT,
    embed_source TEXT,             -- clean | cutout
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_tables_ready: set[str] = set()
_cache: dict[str, dict[str, dict]] = {}  # db path -> item_id -> record (this process is the only writer)
_cache_lock = threading.RLock()


def _dbkey() -> str:
    return str(config.DB_PATH)


def _ensure_table() -> None:
    k = _dbkey()
    if k in _tables_ready:
        return
    with db.get_conn() as c:
        c.executescript(_TABLE_SQL)
    _tables_ready.add(k)


def _row(r) -> dict:
    d = dict(r)
    try:
        d["checks"] = json.loads(d.get("checks") or "null")
    except Exception:
        d["checks"] = None
    return d


def _all_records() -> dict[str, dict]:
    k = _dbkey()
    with _cache_lock:
        if k not in _cache:
            _ensure_table()
            with db.get_conn() as c:
                rows = c.execute("SELECT * FROM item_renders").fetchall()
            _cache[k] = {r["item_id"]: _row(r) for r in rows}
        return _cache[k]


def get_render(item_id: str) -> dict | None:
    try:
        return _all_records().get(item_id)
    except Exception as e:  # never break item listing
        log.warning("render record lookup failed: %s", e)
        return None


def _save(item_id: str, **fields) -> dict:
    _ensure_table()
    now = db.now_iso()
    with _cache_lock:
        cur = dict(_all_records().get(item_id) or {"item_id": item_id, "created_at": now})
        cur.update(fields)
        cur["updated_at"] = now
        with db.get_conn() as c:
            if c.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                return cur  # item deleted meanwhile
            c.execute("""INSERT OR REPLACE INTO item_renders(item_id,status,method,clean_path,model,checks,embed_source,
                         error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (item_id, cur.get("status") or "pending", cur.get("method"), cur.get("clean_path"),
                       cur.get("model"), json.dumps(cur.get("checks")), cur.get("embed_source"), cur.get("error"),
                       cur["created_at"], cur["updated_at"]))
        _all_records()[item_id] = cur
        return cur


def forget(item_id: str) -> None:
    """Item deleted: drop the render record + clean files."""
    try:
        rec = get_render(item_id)
        for p in clean_dir().glob(f"{item_id}_*"):
            p.unlink(missing_ok=True)
        if rec and rec.get("clean_path"):
            Path(rec["clean_path"]).unlink(missing_ok=True)
        with _cache_lock:
            _all_records().pop(item_id, None)
            with db.get_conn() as c:
                c.execute("DELETE FROM item_renders WHERE item_id=?", (item_id,))
    except Exception as e:
        log.warning("render cleanup for %s failed: %s", item_id, e)


STALE_PENDING_S = 600


def api_fields(item: dict) -> dict:
    """Extra fields for pipeline.item_to_api. Never raises."""
    out = {"clean_image_url": None, "clean_method": None, "render_status": None, "render_checks": None}
    try:
        rec = get_render(item["id"])
        if not rec:
            return out
        status = rec.get("status")
        if status == "pending":
            try:
                age = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(rec["updated_at"])).total_seconds()
                if age > STALE_PENDING_S and item["id"] not in _inflight:
                    status = "failed"  # server restarted mid-render
            except Exception:
                pass
        clean = config.media_url(rec.get("clean_path")) if rec.get("clean_path") and Path(rec["clean_path"]).exists() else None
        out.update(clean_image_url=clean, clean_method=rec.get("method") if clean else None, render_status=status,
                   render_checks=rec.get("checks"))
    except Exception as e:
        log.warning("render api_fields failed: %s", e)
    return out


def embedding_image_path(item: dict) -> str | None:
    """Image to embed for fashion-clip / OutfitTransformer: the clean render iff it's a verified Gemini render."""
    try:
        rec = get_render(item["id"])
        if rec and rec.get("embed_source") == "clean" and rec.get("clean_path") and Path(rec["clean_path"]).exists():
            return rec["clean_path"]
    except Exception:
        pass
    return item.get("white_path") or item.get("cutout_path") or item.get("crop_path")


# ------------------------------------------------------------------ colour helpers
_M_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                       [0.0193339, 0.1191920, 0.9503041]], dtype=np.float64)
_WHITE = np.array([0.95047, 1.0, 1.08883])


def rgb_to_lab(rgb) -> np.ndarray:
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M_RGB2XYZ.T / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def lab_to_rgb(lab) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16) / 116
    fx, fz = fy + lab[..., 1] / 500, fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], -1)
    xyz = np.where(f > 0.206893, f ** 3, (f - 16 / 116) / 7.787) * _WHITE
    lin = xyz @ np.linalg.inv(_M_RGB2XYZ).T
    lin = np.clip(lin, 0, 1)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(c * 255 + 0.5, 0, 255).astype(np.uint8)


def dominant_lab(pixels_rgb: np.ndarray, k: int = 3, max_px: int = 20000) -> tuple[np.ndarray, list[dict]]:
    """Deterministic k-means in LAB. Returns (dominant centre, clusters sorted by weight)."""
    px = np.asarray(pixels_rgb).reshape(-1, 3)
    if len(px) == 0:
        return np.array([100.0, 0, 0]), []
    if len(px) > max_px:
        px = px[np.linspace(0, len(px) - 1, max_px).astype(int)]
    lab = rgb_to_lab(px)
    k = min(k, len(lab))
    order = np.argsort(lab[:, 0])
    centres = lab[order[np.linspace(0, len(lab) - 1, k + 2)[1:-1].astype(int)]].copy()
    for _ in range(12):
        d = ((lab[:, None, :] - centres[None]) ** 2).sum(-1)
        lbl = d.argmin(1)
        for j in range(k):
            if (lbl == j).any():
                centres[j] = lab[lbl == j].mean(0)
    w = np.bincount(lbl, minlength=k) / len(lab)
    cl = sorted([{"lab": centres[j].round(1).tolist(), "weight": round(float(w[j]), 3)} for j in range(k)],
                key=lambda c: -c["weight"])
    return np.array(cl[0]["lab"]), cl


def color_distance(lab1, lab2) -> float:
    """Lightness-discounted LAB distance (studio relighting changes L more than hue/chroma)."""
    d = np.asarray(lab1, float) - np.asarray(lab2, float)
    return float(np.sqrt((0.5 * d[0]) ** 2 + d[1] ** 2 + d[2] ** 2))


# ------------------------------------------------------------------ mask helpers
def refine_mask(mask: np.ndarray, small_hole_frac: float = 0.015) -> np.ndarray:
    """Close gaps, keep the main component(s), fill small holes (big holes may be real), drop specks."""
    m = np.asarray(mask, bool)
    if not m.any():
        return m
    m = ndimage.binary_closing(m, structure=np.ones((3, 3)), iterations=2)
    lab, n = ndimage.label(m)
    if n > 1:
        sizes = ndimage.sum(m, lab, range(1, n + 1))
        m = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 0.25 * sizes.max()])
    holes = ndimage.binary_fill_holes(m) & ~m
    if holes.any():
        hl, hn = ndimage.label(holes)
        hs = ndimage.sum(holes, hl, range(1, hn + 1))
        small = [i + 1 for i, s in enumerate(hs) if s <= small_hole_frac * m.sum()]
        m = m | np.isin(hl, small)
    return ndimage.binary_opening(m, structure=np.ones((3, 3)))


def _fill_small_notches(fg: np.ndarray, max_frac: float = 0.02) -> np.ndarray:
    """GrabCut can notch out garment parts that look like the background (e.g. an orange leather label on jeans
    lying on a wooden floor). Fill small, compact concavities (convex-hull deficit components < max_frac of the
    garment area) that the garment encloses on ~3 sides; large concavities (gap between trouser legs, under
    sleeves) and shallow dents are left alone. Only used for GrabCut masks (plain-crop items)."""
    try:
        import cv2
    except Exception:
        return fg
    cnts, _ = cv2.findContours(fg.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return fg
    hull = np.zeros(fg.shape, np.uint8)
    for c in cnts:
        if cv2.contourArea(c) >= 0.2 * fg.sum():
            cv2.fillPoly(hull, [cv2.convexHull(c)], 1)
    deficit = ndimage.binary_opening(hull.astype(bool) & ~fg, structure=np.ones((3, 3)))
    lab, n = ndimage.label(deficit)
    if n == 0:
        return fg
    out = fg.copy()
    area = fg.sum()
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        comp = lab[sl] == i
        a = comp.sum()
        bh, bw = comp.shape
        if a > max_frac * area or a / float(bh * bw) < 0.35 or max(bh, bw) > 4 * min(bh, bw):
            continue
        full = lab == i
        ring = ndimage.binary_dilation(full, iterations=3) & ~full
        if (ring & fg).sum() >= 0.57 * ring.sum():  # enclosed by garment on ~3 sides (a notch, not a shallow dent)
            out |= full
    return out


def grabcut_mask(rgb: Image.Image, iters: int = 5, seed: np.ndarray | None = None) -> np.ndarray | None:
    """Foreground mask for a plain crop (segformer rejected the mask). The crop is the detector box + ~6% padding,
    so the border is background. Returns None if OpenCV is missing or the result looks implausible."""
    try:
        import cv2
    except Exception:
        return None
    W, H = rgb.size
    s = min(1.0, 512 / max(W, H))
    small = rgb.resize((max(8, int(W * s)), max(8, int(H * s))), Image.BILINEAR) if s < 1 else rgb
    img = np.ascontiguousarray(np.asarray(small.convert("RGB"))[:, :, ::-1])
    h, w = img.shape[:2]
    mx, my = max(2, int(0.04 * w)), max(2, int(0.04 * h))
    mask = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    if seed is not None:  # segformer's garment pixels as probable foreground (keeps neighbouring items out)
        sd = np.asarray(Image.fromarray(seed.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 127
        if sd.mean() < 0.05:
            seed = None
        else:
            mask[:] = cv2.GC_PR_BGD
            mask[sd] = cv2.GC_PR_FGD
            core = ndimage.binary_erosion(sd, iterations=max(2, int(0.02 * max(w, h))))
            mask[core] = cv2.GC_FGD
            mask[:my, :], mask[-my:, :], mask[:, :mx], mask[:, -mx:] = (cv2.GC_BGD,) * 4
    try:
        cv2.setRNGSeed(0)  # GrabCut's GMM init uses OpenCV's global RNG: seed it for deterministic output
        if seed is not None:
            cv2.grabCut(img, mask, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)
        else:
            cv2.grabCut(img, mask, (mx, my, w - 2 * mx, h - 2 * my), bgd, fgd, iters, cv2.GC_INIT_WITH_RECT)
    except Exception as e:
        log.info("grabcut failed: %s", e)
        return None
    fg = (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)
    fg = _fill_small_notches(refine_mask(fg))
    frac = fg.mean()
    if not (0.12 <= frac <= 0.93):
        return None
    lab, n = ndimage.label(fg)
    if n and ndimage.sum(fg, lab, range(1, n + 1)).max() < 0.7 * fg.sum():
        return None
    return np.asarray(Image.fromarray(fg.astype(np.uint8) * 255).resize((W, H), Image.NEAREST)) > 127


def _minrect_tilt(pts: np.ndarray) -> tuple[float, float]:
    """(angle in [-45,45] minimising the bounding-box area of pts, relative area reduction vs 0 degrees)."""
    def area(a):
        r = np.radians(a)
        p = pts @ np.array([[np.cos(r), -np.sin(r)], [np.sin(r), np.cos(r)]]).T
        return np.ptp(p[:, 0]) * np.ptp(p[:, 1])
    a0 = area(0.0)
    angles = np.arange(-45, 45.01, 1.0)
    areas = [area(a) for a in angles]
    i = int(np.argmin(areas))
    return float(angles[i]), float(1 - areas[i] / max(a0, 1e-9))


def estimate_tilt(mask: np.ndarray) -> float:
    """Degrees to rotate (PIL convention: positive = counter-clockwise) so the garment is straight.
    Conservative on purpose: crumpled/irregular shapes have no meaningful axis, so we only correct when the shape is
    clearly elongated (principal-axis ratio >= 1.6), the principal axis and the minimum-area rectangle agree within
    6 degrees, straightening shrinks the bounding box by >= 8%, and the tilt is small (<= RENDER_MAX_TILT).
    Never guesses 90/180 degree flips."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        return 0.0
    if len(xs) > 30000:
        idx = np.linspace(0, len(xs) - 1, 30000).astype(int)
        xs, ys = xs[idx], ys[idx]
    x = xs - xs.mean()
    y = -(ys - ys.mean())  # y up
    evals, evecs = np.linalg.eigh(np.cov(np.stack([x, y])))
    elong = float(np.sqrt(max(evals[1], 1e-9) / max(evals[0], 1e-9)))
    if elong < 1.6:
        return 0.0
    vx, vy = evecs[:, 1]
    ang = float(np.degrees(np.arctan2(vy, vx)))  # major-axis angle from +x (CCW)
    pca_tilt = -(((ang + 45) % 90) - 45)
    rect_tilt, reduction = _minrect_tilt(np.stack([x, y], 1))  # rotating by rect_tilt (CCW) straightens it
    if abs(pca_tilt - rect_tilt) > 6 or reduction < 0.08:
        return 0.0
    tilt = 0.5 * (pca_tilt + rect_tilt)
    if abs(tilt) < 1.5 or abs(tilt) > RENDER_MAX_TILT:
        return 0.0
    return float(round(tilt, 1))


# ------------------------------------------------------------------ option 2: deterministic cleanup
def _guided_smooth(rgb: np.ndarray, r: int, eps: float, strength: float) -> np.ndarray:
    """Self-guided filter per channel (He et al.): smooths low-contrast shading, keeps high-contrast edges
    (print, text, seams). Blended with the original at `strength`."""
    I = rgb.astype(np.float64) / 255.0
    size = 2 * r + 1
    mean = lambda a: ndimage.uniform_filter(a, size=size, mode="reflect")  # noqa: E731
    out = np.empty_like(I)
    for ch in range(3):
        p = I[..., ch]
        mp = mean(p)
        var = mean(p * p) - mp * mp
        a = var / (var + eps)
        b = mp - a * mp
        out[..., ch] = mean(a) * p + mean(b)
    res = (1 - strength) * I + strength * out
    return np.clip(res * 255 + 0.5, 0, 255).astype(np.uint8)


def _white_balance(rgb: np.ndarray, fg: np.ndarray) -> tuple[np.ndarray, dict]:
    """Neutralise a colour cast only when the photo background (outside the garment) is a bright near-neutral
    surface (white sheet / wall); gains are blended at 60% and capped to +-10%. Otherwise untouched."""
    bg = ~fg
    info = {"applied": False}
    if bg.sum() < 500:
        return rgb, info
    bgpx = rgb[bg]
    lab = rgb_to_lab(bgpx.mean(0))
    chroma = float(np.hypot(lab[1], lab[2]))
    info.update(bg_L=round(float(lab[0]), 1), bg_chroma=round(chroma, 1))
    if lab[0] < 45 or chroma > 10 or chroma < 1.5:
        return rgb, info
    means = bgpx.reshape(-1, 3).astype(np.float64).mean(0)
    gains = np.clip(1 + 0.6 * (means.mean() / np.clip(means, 1, None) - 1), 0.9, 1.1)
    out = np.clip(rgb.astype(np.float64) * gains + 0.5, 0, 255).astype(np.uint8)
    info.update(applied=True, gains=[round(float(g), 3) for g in gains])
    return out, info


def _tone(rgb: np.ndarray, fg: np.ndarray) -> tuple[np.ndarray, dict]:
    """Gentle lightness stretch on garment pixels (LAB L only: hue/chroma untouched, max shift ~8 L)."""
    if fg.sum() < 100:
        return rgb, {"applied": False}
    lab = rgb_to_lab(rgb[fg])
    L = lab[:, 0]
    p1, p99 = np.percentile(L, 1), np.percentile(L, 99)
    if p99 - p1 < 10:
        return rgb, {"applied": False}
    lo = p1 - min(4.0, 0.15 * p1)
    hi = p99 + min(8.0, 0.15 * max(0.0, 97 - p99))
    lab[:, 0] = np.clip(lo + (L - p1) * (hi - lo) / (p99 - p1), 0, 100)
    out = rgb.copy()
    out[fg] = lab_to_rgb(lab)
    return out, {"applied": True, "L_in": [round(float(p1), 1), round(float(p99), 1)],
                 "L_out": [round(float(lo), 1), round(float(hi), 1)]}


def compose_square(rgb: Image.Image, alpha: Image.Image, size: int = RENDER_SIZE, pad_frac: float = RENDER_PAD_FRAC,
                   shadow: bool = RENDER_SHADOW) -> Image.Image:
    """Crop to the alpha bbox, scale into a size x size white canvas with padding, optional soft drop shadow."""
    bbox = alpha.point(lambda v: 255 if v > 24 else 0).getbbox() or (0, 0, *alpha.size)
    rgb, alpha = rgb.crop(bbox), alpha.crop(bbox)
    inner = int(round(size * (1 - 2 * pad_frac)))
    s = inner / max(rgb.size)
    nw, nh = max(1, int(round(rgb.width * s))), max(1, int(round(rgb.height * s)))
    rgb = rgb.resize((nw, nh), Image.LANCZOS)
    alpha = alpha.resize((nw, nh), Image.LANCZOS)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas = np.full((size, size, 3), 255.0)
    if shadow:
        sh = Image.new("L", (size, size), 0)
        sh.paste(alpha, (ox, oy + max(2, int(0.012 * size))))
        sh = np.asarray(sh.filter(ImageFilter.GaussianBlur(max(2, int(0.018 * size)))), dtype=np.float64) / 255.0
        canvas *= (1 - 0.16 * sh)[..., None]
    a = np.zeros((size, size), np.float64)
    a[oy:oy + nh, ox:ox + nw] = np.asarray(alpha, dtype=np.float64) / 255.0
    fgimg = np.zeros((size, size, 3), np.float64)
    fgimg[oy:oy + nh, ox:ox + nw] = np.asarray(rgb, dtype=np.float64)
    canvas = canvas * (1 - a[..., None]) + fgimg * a[..., None]
    return Image.fromarray(np.clip(canvas + 0.5, 0, 255).astype(np.uint8))


def segformer_seed(rgb: Image.Image, category: str | None) -> np.ndarray | None:
    """Pixels segformer assigns to the item's category classes (used to seed GrabCut). None if unavailable."""
    try:
        from .segment import CATEGORY_CLASSES
        from .models_runtime import get_segformer
        import torch
        wanted = CATEGORY_CLASSES.get((category or "").lower())
        if not wanted:
            return None
        W, H = rgb.size
        s = min(1.0, 512 / max(W, H))
        small = rgb.resize((max(8, int(W * s)), max(8, int(H * s))), Image.BILINEAR) if s < 1 else rgb
        proc, model, mlock = get_segformer()
        with mlock, torch.inference_mode():
            logits = model(**proc(images=small.convert("RGB"), return_tensors="pt")).logits
            seg = torch.nn.functional.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False)
            seg = seg.argmax(dim=1)[0].numpy()
        m = np.isin(seg, list(wanted))
        return m if m.mean() >= 0.05 else None
    except Exception as e:
        log.info("segformer seed unavailable: %s", e)
        return None


def prepare_garment(cutout: Image.Image, crop: Image.Image | None = None,
                    category: str | None = None) -> tuple[Image.Image, np.ndarray, dict]:
    """(RGB image incl. background pixels, refined boolean garment mask, info)."""
    rgba = cutout.convert("RGBA")
    rgb = rgba.convert("RGB")
    alpha = np.asarray(rgba.split()[3])
    info: dict = {}
    if (alpha > 250).mean() > 0.985:  # no segmentation mask (segformer fell back to the plain crop)
        src = crop.convert("RGB") if crop is not None and crop.size == rgba.size else rgb
        seed = segformer_seed(src, category) if category else None
        gm = grabcut_mask(src, seed=seed)
        if gm is not None:
            info["mask"] = "grabcut_seeded" if seed is not None else "grabcut"
            return src, gm, info
        info["mask"] = "none"
        return src, np.ones(alpha.shape, bool), info
    info["mask"] = "segformer"
    return rgb, refine_mask(alpha > 127), info


def cleanup_render(cutout: Image.Image, crop: Image.Image | None = None, size: int = RENDER_SIZE,
                   category: str | None = None) -> tuple[Image.Image, dict]:
    """Option 2: deterministic product-style image. Returns (RGB size x size image, info)."""
    rgb, fg, info = prepare_garment(cutout, crop, category)
    return cleanup_from_mask(rgb, fg, info, size)


def cleanup_from_mask(rgb: Image.Image, fg: np.ndarray, info: dict, size: int = RENDER_SIZE) -> tuple[Image.Image, dict]:
    info = dict(info)
    arr = np.asarray(rgb.convert("RGB")).copy()
    arr, info["white_balance"] = _white_balance(arr, fg)
    arr, info["tone"] = _tone(arr, fg)
    r = max(2, int(round(max(arr.shape[:2]) / 300)))
    arr = _guided_smooth(arr, r=r, eps=0.02 ** 2, strength=0.5)
    info["smooth"] = {"filter": "guided", "r": r, "eps": 0.0004, "strength": 0.5}
    tilt = estimate_tilt(fg) if info["mask"] != "none" else 0.0
    info["tilt_deg"] = tilt
    # soft alpha: shrink 1px (drops background fringe), feather
    hard = ndimage.binary_erosion(fg, iterations=1) if fg.sum() > 2000 and info["mask"] != "none" else fg
    alpha = Image.fromarray(hard.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(1.0))
    img = Image.fromarray(arr)
    if tilt:
        img = img.rotate(tilt, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
        alpha = alpha.rotate(tilt, resample=Image.BILINEAR, expand=True, fillcolor=0)
    out = compose_square(img, alpha, size=size, shadow=RENDER_SHADOW and info["mask"] != "none")
    info["size"] = size
    return out, info


def garment_mask_on_white(img: Image.Image, tol: int = 12) -> np.ndarray:
    """Garment pixels of a product image on a (near-)white background: flood-fill near-white from the border."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    near_white = a.min(axis=2) >= 255 - tol
    lab, _ = ndimage.label(near_white)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    fg = ndimage.binary_fill_holes(~np.isin(lab, list(border)))
    fg = ndimage.binary_opening(fg, structure=np.ones((3, 3)))
    if fg.mean() < 0.01:
        fg = ~near_white
    return fg


def normalize_render(img: Image.Image, size: int = RENDER_SIZE) -> Image.Image:
    """Gemini output -> the same canvas as option 2 (pure white bg, same padding/size, our own soft shadow)."""
    img = img.convert("RGB")
    fg = refine_mask(garment_mask_on_white(img))
    if fg.mean() < 0.01:
        return compose_square(img, Image.new("L", img.size, 255), size=size, shadow=False)
    alpha = Image.fromarray(fg.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(1.0))
    return compose_square(img, alpha, size=size)


# ------------------------------------------------------------------ verification
def clip_similarity(a: Image.Image, b: Image.Image) -> float:
    from .vectors import embed_images
    v = embed_images([a.convert("RGB"), b.convert("RGB")])
    return float(v[0] @ v[1])


def color_check(ref_rgb: np.ndarray, render: Image.Image) -> dict:
    """Dominant LAB colour of the original garment pixels vs the render's garment pixels."""
    rp = np.asarray(render.convert("RGB"))[garment_mask_on_white(render)]
    d_ref, cl_ref = dominant_lab(ref_rgb)
    d_ren, cl_ren = dominant_lab(rp)
    dist = color_distance(d_ref, d_ren)
    # also accept if the render's dominant colour matches any substantial original cluster (e.g. two-tone items)
    alt = min([color_distance(c["lab"], d_ren) for c in cl_ref if c["weight"] >= 0.25] or [dist])
    best = min(dist, alt)
    return {"passed": bool(best <= RENDER_COLOR_MAX_DE), "distance": round(best, 2), "threshold": RENDER_COLOR_MAX_DE,
            "ref_dominant_lab": [round(float(x), 1) for x in d_ref],
            "render_dominant_lab": [round(float(x), 1) for x in d_ren]}


_TEXT_WORDS = re.compile(r"logo|text|print|graphic|letter|slogan|embroider|word|lettering|brand|script|number|monogram",
                         re.I)


def text_suspected(item: dict) -> bool:
    a = item.get("attributes") or {}
    if a.get("brand"):
        return True
    blob = " ".join(str(x) for x in [a.get("pattern"), a.get("description"), item.get("label"),
                                     " ".join(a.get("style_tags") or [])] if x)
    return bool(_TEXT_WORDS.search(blob))


# ------------------------------------------------------------------ Gemini: availability / quota state
class RenderUnavailable(RuntimeError):
    pass


_state_lock = threading.Lock()
_state: dict | None = None
_model_blocked: dict[str, float] = {}


def _load_state() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(_state_path().read_text())
        except Exception:
            _state = {}
    return _state


def _save_state() -> None:
    try:
        _state_path().write_text(json.dumps(_state or {}, indent=1))
    except Exception as e:
        log.warning("could not persist render state: %s", e)


def _next_quota_reset() -> float:
    now = _dt.datetime.now(_dt.timezone.utc)
    reset = now.replace(hour=7, minute=0, second=0, microsecond=0)  # midnight Pacific == 3 AM ET
    if reset <= now:
        reset += _dt.timedelta(days=1)
    return reset.timestamp()


def gemini_status() -> dict:
    st = _load_state()
    until = float(st.get("disabled_until") or 0)
    return {"mode": RENDER_GEMINI, "model": RENDER_MODEL, "fallbacks": RENDER_FALLBACK_MODELS,
            "configured": _gemini_configured(),
            "disabled": RENDER_GEMINI == "off" or (RENDER_GEMINI == "auto" and time.time() < until),
            "disabled_until": until or None, "reason": st.get("reason"), "last_error": st.get("error"),
            "working_model": st.get("working_model")}


def _gemini_configured() -> bool:
    try:
        from . import gemini
        return gemini.is_configured()
    except Exception:
        return False


def gemini_render_available() -> bool:
    if RENDER_GEMINI == "off" or not _gemini_configured():
        return False
    st = _load_state()
    if RENDER_GEMINI == "auto" and time.time() < float(st.get("disabled_until") or 0):
        return False
    return any(time.time() >= _model_blocked.get(m, 0) for m in _image_models())


def _image_models() -> list[str]:
    return list(dict.fromkeys([RENDER_MODEL, *RENDER_FALLBACK_MODELS]))


def _disable(reason: str, err: str, until: float) -> None:
    with _state_lock:
        st = _load_state()
        st.update(disabled_until=until, reason=reason, error=err[:600],
                  disabled_at=db.now_iso())
        _save_state()
    log.warning("Gemini image rendering auto-disabled until %s (%s): %s",
                _dt.datetime.fromtimestamp(until).isoformat(timespec="minutes"), reason, err[:200])


def classify_error(e: Exception) -> str:
    """'no_access' (free tier has no quota for this model / permission / billing), 'daily', 'minute',
    'unavailable' (404 / unsupported model), 'transient' (5xx/timeouts), 'other'."""
    msg = str(e)
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    head = msg[:16]

    def has(c):
        return code == c or head.startswith(f"{c} ") or f"{c} " in head

    if has(429):
        if re.search(r"limit['\"]?\s*[:=]\s*['\"]?0\b", msg) or "free_tier" in msg.lower() and "limit: 0" in msg:
            return "no_access"
        if "PerDay" in msg or "per day" in msg.lower():
            return "daily"
        if "PerMinute" in msg or "retryDelay" in msg or "per minute" in msg.lower():
            return "minute"
        return "no_access"  # plan/billing
    if has(403) or "PERMISSION_DENIED" in msg:
        return "no_access"
    if has(404):
        return "unavailable"
    if has(400) and re.search(r"not supported|unsupported|billing|free tier|not available|modalit", msg, re.I):
        return "unavailable" if "modalit" in msg.lower() or "not supported" in msg.lower() else "no_access"
    if has(500) or has(503) or has(504) or "timed out" in msg.lower() or "deadline" in msg.lower():
        return "transient"
    return "other"


# ------------------------------------------------------------------ Gemini: image redraw + vision verify
RENDER_PROMPT = """You are a professional e-commerce product photo retoucher.
Image 1 is ONE garment segmented from a customer's photo (background removed). It may be wrinkled, crumpled,
folded, tilted, partly occluded or cut off. Image 2 is the original photo region for context only (other items
may be visible there: ignore them; use it to read colours, logos and printed text more accurately).
Item: {desc}

Create a professional e-commerce product photo of THIS EXACT garment:
- {style}, front view, centered, the whole garment in frame with margin, on a PURE WHITE (#FFFFFF) background,
  soft even studio lighting, no harsh shadows.
- Fully smoothed and pressed: NO wrinkles or creases, symmetric, neatly laid out, sleeves/legs straightened.
- COMPLETE any parts that are hidden, folded under, cropped or occluded (e.g. a sleeve tucked under, part of the
  garment out of frame) consistently with the visible parts.
MUST PRESERVE EXACTLY (this is a real item the customer owns): colour and shade, fabric texture, pattern, every
logo, printed text (identical spelling and font style), graphics and their size and position, buttons, zippers,
pockets, drawstrings, collar/hood, neckline, sleeve length, hem length, cut and silhouette.
DO NOT add anything that is not in the photo: no new logos, text, labels or tags, no model or person, no hanger,
no mannequin stand, no props, no watermark. Do not change the colour. Output only the image."""

_STYLE_BY_CAT = {
    "top": "ghost-mannequin (invisible mannequin) style",
    "outerwear": "ghost-mannequin (invisible mannequin) style",
    "dress": "ghost-mannequin (invisible mannequin) style",
    "bottom": "neat flat-lay",
    "shoes": "clean studio product shot (pair side by side, 3/4 side view)",
    "accessory": "clean studio product shot",
}


def _desc(item: dict) -> str:
    a = item.get("attributes") or {}
    bits = [a.get("description") or item.get("label") or "clothing item"]
    extra = []
    for k in ("category", "subcategory", "primary_color", "pattern", "fabric_guess", "brand"):
        if a.get(k):
            extra.append(f"{k.replace('_', ' ')}: {a[k]}")
    if a.get("secondary_colors"):
        extra.append("secondary colours: " + ", ".join(map(str, a["secondary_colors"])))
    return bits[0] + (" (" + "; ".join(extra) + ")" if extra else "")


def _img_part(img: Image.Image, max_side: int = 1024):
    from google.genai import types
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


def _gemini_image_call(model: str, prompt: str, garment: Image.Image, context: Image.Image | None) -> Image.Image:
    """ONE generate_content call on an image model. Returns the generated PIL image (raises on anything else)."""
    from google.genai import types
    from . import gemini
    contents = [prompt, _img_part(garment)] + ([_img_part(context)] if context is not None else [])
    kw = dict(response_modalities=["IMAGE"])
    try:
        kw["image_config"] = types.ImageConfig(aspect_ratio="1:1")
    except Exception:
        pass
    resp = gemini.client().models.generate_content(model=model, contents=contents,
                                                   config=types.GenerateContentConfig(**kw))
    texts = []
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if data:
                return Image.open(io.BytesIO(data)).convert("RGB")
            if getattr(part, "text", None):
                texts.append(part.text)
    fr = None
    try:
        fr = resp.candidates[0].finish_reason
    except Exception:
        pass
    raise RenderUnavailable(f"no image in response (finish_reason={fr}; text={' '.join(texts)[:160]!r})")


class RenderVerdict(BaseModel):
    same_garment: bool = Field(description="image 2 depicts the same physical garment as image 1")
    has_text_or_logo: bool = Field(description="image 1 shows any logo, printed text, lettering or graphic")
    text_matches: bool = Field(description="all logos/text/graphics of image 1 appear in image 2 with the same "
                                           "spelling, style, size and position (true if image 1 has none)")
    color_matches: bool = Field(description="same colour and shade (allowing for studio lighting)")
    added_elements: bool = Field(description="image 2 adds something not in image 1: new logo/text, person, hanger, "
                                             "props, different details")
    issues: list[str] = Field(description="short, concrete differences to fix; empty if none")


VERIFY_PROMPT = """You are a strict QA checker for a closet app. Image 1 is a customer's photo of their garment (it may
be wrinkled, folded or partly hidden; other items may be visible: only consider the main garment{hint}).
Image 2 is an AI-generated product photo that must depict EXACTLY the same garment, cleaned up.
Parts hidden in image 1 may be completed plausibly in image 2: do not penalise that. Wrinkles removed is expected.
Compare: colour/shade, pattern, fabric, every logo and printed text (exact spelling), graphics and their size and
position, buttons, zippers, pockets, collar/hood, neckline, sleeve length, hem length, cut/silhouette.
Return JSON only."""


def _gemini_verify_call(original: Image.Image, render: Image.Image, item: dict) -> dict:
    """ONE vision call (first available lite model) -> RenderVerdict dict + model name."""
    from google.genai import types
    from . import gemini
    label = (item.get("attributes") or {}).get("description") or item.get("label")
    prompt = VERIFY_PROMPT.format(hint=f": {label}" if label else "")
    last = None
    for m in RENDER_VERIFY_MODELS:
        if time.time() < _model_blocked.get(m, 0):
            continue
        try:
            resp = gemini.client().models.generate_content(
                model=m, contents=[prompt, _img_part(original), _img_part(render)],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_schema=RenderVerdict, temperature=0.0))
            parsed = getattr(resp, "parsed", None)
            v = parsed if parsed is not None else RenderVerdict.model_validate_json(resp.text)
            return {**json.loads(v.model_dump_json()), "model": m}
        except Exception as e:
            last = e
            kind = classify_error(e)
            if kind in ("no_access", "daily", "unavailable"):
                _model_blocked[m] = _next_quota_reset() if kind != "unavailable" else time.time() + 86400
                continue
            if kind in ("minute", "transient"):
                continue
            raise
    raise RenderUnavailable(f"no verification model available ({type(last).__name__ if last else 'none'}: "
                            f"{str(last)[:160] if last else ''})")


def verify_render(item: dict, render: Image.Image, reference: Image.Image, ref_pixels: np.ndarray,
                  photo: Image.Image | None) -> dict:
    """Checks (a) clip cosine, (b) dominant colour, (c) Gemini vision compare. Returns {passed, issues, ...}."""
    checks: dict = {"issues": []}
    try:
        sim = clip_similarity(render, reference)
    except Exception as e:
        sim = None
        checks["issues"].append(f"clip check failed: {type(e).__name__}")
    checks["clip"] = {"similarity": None if sim is None else round(sim, 4), "threshold": RENDER_CLIP_MIN,
                      "passed": bool(sim is not None and sim >= RENDER_CLIP_MIN)}
    if not checks["clip"]["passed"] and sim is not None:
        checks["issues"].append(f"overall look differs from the photo (similarity {sim:.2f})")
    try:
        checks["color"] = color_check(ref_pixels, render)
    except Exception as e:
        checks["color"] = {"passed": False, "error": str(e)[:120]}
    if not checks["color"]["passed"]:
        checks["issues"].append("garment colour/shade differs from the photo; keep the exact original colour")
    txt = text_suspected(item)
    checks["text_expected"] = txt
    want_llm = RENDER_VERIFY_GEMINI == "always" or (RENDER_VERIFY_GEMINI == "text" and txt)
    llm_ok = True
    if want_llm:
        try:
            v = _gemini_verify_call(photo if photo is not None else reference, render, item)
            checks["gemini"] = v
            llm_ok = bool(v["same_garment"] and not v["added_elements"]
                          and (not v["has_text_or_logo"] or v["text_matches"]) and v["color_matches"])
            if not llm_ok:
                checks["issues"] += [str(i) for i in (v.get("issues") or [])][:6] or ["QA model rejected the render"]
        except Exception as e:
            checks["gemini"] = {"skipped": True, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
            if txt:  # can't verify logos/text without the vision check -> don't trust the render
                llm_ok = False
                checks["issues"].append("could not verify logo/text fidelity")
    else:
        checks["gemini"] = {"skipped": True, "reason": f"RENDER_VERIFY_GEMINI={RENDER_VERIFY_GEMINI}"}
    checks["passed"] = bool(checks["clip"]["passed"] and checks["color"]["passed"] and llm_ok)
    return checks


NO_ACCESS_DISABLE_S = float(os.environ.get("RENDER_NO_ACCESS_DISABLE_H", "72")) * 3600


def _generate_once(prompt: str, garment: Image.Image, context: Image.Image | None, rec: dict,
                   only_model: str | None = None) -> tuple[Image.Image | None, str | None]:
    """Try image models in order (at most RENDER_MAX_MODELS). Quota/permission errors block that model and move on;
    anything else stops Gemini for this item. When every tried model failed on quota/permission, image generation is
    auto-disabled (persisted) so we stop spending calls."""
    tried, quota_errs = 0, []
    for m in ([only_model] if only_model else _image_models()):
        if time.time() < _model_blocked.get(m, 0):
            continue
        if tried >= RENDER_MAX_MODELS:
            break
        tried += 1
        t0 = time.time()
        try:
            img = _gemini_image_call(m, prompt, garment, context)
        except Exception as e:
            kind = "no_image" if isinstance(e, RenderUnavailable) else classify_error(e)
            err = f"{type(e).__name__}: {str(e)[:400]}"
            rec["attempts"].append({"model": m, "ok": False, "error_kind": kind, "error": err})
            log.warning("Gemini render with %s failed (%s): %s", m, kind, str(e)[:200])
            if kind in ("no_access", "daily", "unavailable"):
                _model_blocked[m] = _next_quota_reset() if kind == "daily" else time.time() + NO_ACCESS_DISABLE_S
                quota_errs.append((kind, err))
                continue
            if kind == "minute":
                _model_blocked[m] = time.time() + 65
            break
        rec["attempts"].append({"model": m, "ok": True, "seconds": round(time.time() - t0, 1)})
        with _state_lock:
            st = _load_state()
            st["working_model"] = m
            _save_state()
        return img, m
    if quota_errs and len(quota_errs) == tried and (
            tried >= RENDER_MAX_MODELS or not any(time.time() >= _model_blocked.get(x, 0) for x in _image_models())):
        kinds = {k for k, _ in quota_errs}
        if kinds <= {"daily"}:
            _disable("free-tier daily quota exhausted", quota_errs[-1][1], _next_quota_reset())
        else:
            _disable("image models not available on this API key (free tier limit: 0)", quota_errs[-1][1],
                     time.time() + NO_ACCESS_DISABLE_S)
    return None, None


def _try_gemini(item: dict, cutout_white: Image.Image, crop: Image.Image | None, reference: Image.Image,
                ref_pixels: np.ndarray) -> tuple[Image.Image | None, dict]:
    """Render + verify; at most one retry (same model) with the verification issues appended.
    Returns (verified, normalized render | None, record)."""
    rec: dict = {"attempts": [], "verifications": []}
    cat = (item.get("category") or (item.get("attributes") or {}).get("category") or "").lower()
    base_prompt = RENDER_PROMPT.format(desc=_desc(item), style=_STYLE_BY_CAT.get(cat, "ghost-mannequin or neat flat-lay"))
    img, model = _generate_once(base_prompt, cutout_white, crop, rec)
    for attempt in range(2):
        if img is None:
            break
        norm = normalize_render(img)
        checks = verify_render(item, norm, reference, ref_pixels, crop)
        checks["model"] = model
        rec["verifications"].append(checks)
        if checks["passed"]:
            rec["model"] = model
            return norm, rec
        if attempt == 1 or not gemini_render_available():
            break
        issues = checks["issues"] or ["render did not match the original garment"]
        prompt = base_prompt + "\n\nA previous attempt had these problems, FIX them:\n- " + "\n- ".join(issues)
        img, _ = _generate_once(prompt, cutout_white, crop, rec, only_model=model)
    return None, rec


# ------------------------------------------------------------------ orchestration
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
_inflight: set[str] = set()


def _open(p) -> Image.Image | None:
    try:
        if p and Path(p).exists():
            with Image.open(p) as im:
                return ImageOps.exif_transpose(im).copy()
    except Exception:
        pass
    return None


def _refresh_embeddings(item_id: str) -> bool:
    """Recompute fashion-clip + compat embeddings from embedding_image_path() and refresh FAISS / compat caches."""
    try:
        from .scoring import COMPAT_KIND, ensure_compat_embeddings
        from .vectors import FCLIP_KIND, closet_index, ensure_fclip
        with db.get_conn() as c:
            c.execute("DELETE FROM item_embeddings WHERE item_id=? AND kind IN (?,?)", (item_id, FCLIP_KIND, COMPAT_KIND))
        db.invalidate_compat(item_id)
        it = db.get_item(item_id)
        if it is None:
            return True
        ensure_fclip(it)
        if it["status"] == "closet":
            closet_index.remove(it["id"])
            closet_index.add(it)
            ensure_compat_embeddings([it])
        return True
    except Exception as e:
        log.warning("embedding refresh for %s failed: %s", item_id, e)
        return False


def render_item(item_id: str, mode: str = "auto") -> dict | None:
    """Produce + store the clean image synchronously. mode: auto | gemini | cleanup. Returns the render record."""
    it = db.get_item(item_id)
    if it is None:
        return None
    _inflight.add(item_id)
    try:
        prev = get_render(item_id) or {}
        cutout = _open(it.get("cutout_path")) or _open(it.get("white_path")) or _open(it.get("crop_path"))
        if cutout is None:
            raise FileNotFoundError("item has no image")
        crop = _open(it.get("crop_path"))
        cat = it.get("category") or (it.get("attributes") or {}).get("category")
        rgb, fg, pinfo = prepare_garment(cutout, crop, cat)
        ref_pixels = np.asarray(rgb.convert("RGB"))[fg]
        clean, info = cleanup_from_mask(rgb, fg, pinfo)
        method, model, checks = "cleanup", None, {"method": "cleanup", "cleanup": info}
        final = clean
        if mode in ("auto", "gemini"):
            if gemini_render_available():
                cutout_white = Image.new("RGB", cutout.size, (255, 255, 255))
                cm = cutout.convert("RGBA")
                cutout_white.paste(cm, mask=cm.split()[3])
                try:
                    g, rec = _try_gemini(it, cutout_white, crop, clean, ref_pixels)
                except Exception as e:
                    log.exception("gemini render crashed")
                    g, rec = None, {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                checks["gemini_render"] = rec
                if g is not None:
                    final, method, model = g, "gemini", rec.get("model")
                    checks["method"] = "gemini"
                    checks["verification"] = rec["verifications"][-1]
                else:
                    checks["fallback_reason"] = ("verification failed" if rec.get("verifications") else
                                                 "image generation failed")
            else:
                st = gemini_status()
                checks["gemini_render"] = {"skipped": True,
                                           "reason": st.get("reason") or ("disabled" if st["disabled"] else
                                                                          "gemini not configured" if not st["configured"]
                                                                          else "unavailable")}
        if method == "cleanup":
            try:  # informational sanity check (the cleanup never invents pixels)
                white = _open(it.get("white_path")) or cutout.convert("RGB")
                checks["clip_vs_cutout"] = round(clip_similarity(clean, white), 4)
            except Exception:
                pass
        path = clean_dir() / f"{item_id}_{int(time.time() * 1000)}.jpg"
        final.save(path, quality=92)
        for old in clean_dir().glob(f"{item_id}_*"):
            if old != path:
                old.unlink(missing_ok=True)
        use_clean = RENDER_EMBED_FROM_CLEAN and method == "gemini" and checks.get("verification", {}).get("passed")
        src, prev_src = ("clean" if use_clean else "cutout"), (prev.get("embed_source") or "cutout")
        rec = _save(item_id, status="done", method=method, clean_path=str(path), model=model, checks=checks,
                    error=None, embed_source=src)
        if src != prev_src and not _refresh_embeddings(item_id):
            rec = _save(item_id, embed_source=prev_src)
            _refresh_embeddings(item_id)
        log.info("Rendered %s via %s%s", item_id, method, f" ({model})" if model else "")
        return rec
    finally:
        _inflight.discard(item_id)


def _safe_render(item_id: str, mode: str) -> None:
    try:
        render_item(item_id, mode)
    except Exception as e:
        log.exception("render of %s failed", item_id)
        try:
            _save(item_id, status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass


def schedule(item_ids: list[str], mode: str = "auto") -> None:
    """Queue renders (background thread, one at a time). Never raises."""
    if not RENDER_ENABLED:
        return
    for iid in item_ids:
        try:
            _save(iid, status="pending", error=None)
            _inflight.add(iid)
            if SYNC:
                _safe_render(iid, mode)
            else:
                _executor.submit(_safe_render, iid, mode)
        except Exception as e:
            log.warning("could not schedule render for %s: %s", iid, e)
