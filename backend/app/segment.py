"""Stage 1b: segformer_b2_clothes cutouts for a Gemini bounding box crop."""
from __future__ import annotations

import logging

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

from .models_runtime import get_segformer

log = logging.getLogger("fitcheck.segment")

# mattmdjaga/segformer_b2_clothes labels
SEG_LABELS = {0: "Background", 1: "Hat", 2: "Hair", 3: "Sunglasses", 4: "Upper-clothes", 5: "Skirt",
              6: "Pants", 7: "Dress", 8: "Belt", 9: "Left-shoe", 10: "Right-shoe", 11: "Face",
              12: "Left-leg", 13: "Right-leg", 14: "Left-arm", 15: "Right-arm", 16: "Bag", 17: "Scarf"}
CLOTHING_CLASSES = {1, 3, 4, 5, 6, 7, 8, 9, 10, 16, 17}
CATEGORY_CLASSES = {
    "top": {4},
    "outerwear": {4, 7},
    "bottom": {5, 6},
    "dress": {7, 4, 5},
    "shoes": {9, 10},
    "accessory": {1, 3, 8, 16, 17},
}
_LABEL_HINTS = [  # label keyword -> category (used when Gemini's category isn't known yet)
    (("dress", "gown", "jumpsuit", "romper"), "dress"),
    (("jacket", "coat", "blazer", "parka", "cardigan", "vest", "windbreaker", "trench", "puffer"), "outerwear"),
    (("jean", "pant", "trouser", "chino", "short", "skirt", "legging", "jogger", "slack"), "bottom"),
    (("shoe", "sneaker", "boot", "heel", "sandal", "loafer", "flat", "slipper", "trainer"), "shoes"),
    (("bag", "hat", "cap", "belt", "scarf", "sunglass", "watch", "tie", "purse", "backpack", "jewel"), "accessory"),
    (("shirt", "tee", "top", "blouse", "sweater", "hoodie", "polo", "tank", "sweatshirt", "camisole", "jersey"), "top"),
]


def guess_category_from_label(label: str) -> str | None:
    l = (label or "").lower()
    for keys, cat in _LABEL_HINTS:
        if any(k in l for k in keys):
            return cat
    return None


def mask_quality(mask: np.ndarray, inner: tuple[int, int, int, int] | None, category: str | None) -> tuple[bool, dict]:
    """A good cutout of a *tightly boxed* garment spans most of the box and fills a decent part of it.
    Masks that only cover a fragment (occluded garment, segformer picked the neighbouring item on a flat lay)
    are rejected so the caller can fall back to the plain crop."""
    H, W = mask.shape
    x0, y0, x1, y1 = inner or (0, 0, W, H)
    sub = mask[y0:y1, x0:x1]
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    if not sub.any():
        return False, {"span_w": 0.0, "span_h": 0.0, "fill": 0.0}
    ys, xs = np.where(sub)
    span_w = (xs.max() - xs.min() + 1) / bw
    span_h = (ys.max() - ys.min() + 1) / bh
    fill = sub.sum() / float(bw * bh)
    q = {"span_w": round(float(span_w), 2), "span_h": round(float(span_h), 2), "fill": round(float(fill), 2)}
    min_span = 0.65 if category == "accessory" else 0.8
    min_fill = 0.2 if category == "accessory" else 0.25
    return (span_w >= min_span and span_h >= min_span and fill >= min_fill), q


GARMENT_CLASSES = {4, 5, 6, 7}  # segformer often mixes these up on flat lays (trousers -> "Dress", shoes -> "Upper")


def _clean(mask: np.ndarray) -> np.ndarray:
    mask = ndimage.binary_closing(mask, structure=np.ones((5, 5)), iterations=2)
    lab, n = ndimage.label(mask)
    if n > 1:  # keep largest component (+ big secondary parts e.g. two shoes)
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        keep_ids = [i + 1 for i, s in enumerate(sizes) if s >= 0.3 * sizes.max()]
        mask = np.isin(lab, keep_ids)
    mask = ndimage.binary_fill_holes(mask)
    return ndimage.binary_opening(mask, structure=np.ones((3, 3)))


def segment_crop(crop: Image.Image, category_hint: str | None, max_side: int = 512,
                 inner: tuple[int, int, int, int] | None = None,
                 exclude: list[tuple[int, int, int, int]] | None = None):
    """Return (cutout_rgba, white_rgb, info). Falls back to the plain crop if the mask is poor.

    inner   = the detector's (unpadded) box in crop pixel coords (used by the quality gate).
    exclude = boxes (crop coords) of other, smaller detected items overlapping this crop; if they cover
              > 30% of this item's box (dense/overlapping flat lay) the plain crop is returned.
    Candidates: A = segformer classes of the category; B = any garment class minus excluded boxes
    (for garments/shoes; segformer confuses garment classes on flat lays). The first candidate passing
    mask_quality wins (A preferred when it covers most of B); otherwise the plain crop is used."""
    crop = crop.convert("RGB")
    W, H = crop.size
    scale = min(1.0, max_side / max(W, H))
    small = crop.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.BILINEAR) if scale < 1 else crop

    proc, model, mlock = get_segformer()
    import torch
    with mlock, torch.inference_mode():
        inputs = proc(images=small, return_tensors="pt")
        logits = model(**inputs).logits  # (1, C, h/4, w/4)
        up = torch.nn.functional.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False)
        seg = up.argmax(dim=1)[0].numpy().astype(np.uint8)

    counts = {int(k): int(v) for k, v in zip(*np.unique(seg, return_counts=True))}
    total = float(W * H)
    info = {"classes": {SEG_LABELS[k]: round(v / total, 3) for k, v in counts.items() if v / total > 0.01}}
    cat = category_hint or ""

    cands: list[tuple[str, np.ndarray]] = []
    wanted = CATEGORY_CLASSES.get(cat, set())
    a = np.isin(seg, list(wanted)) if wanted else np.zeros_like(seg, bool)
    cat_frac = a.sum() / total
    if cat_frac < 0.05:
        clothing = {k: v for k, v in counts.items() if k in CLOTHING_CLASSES}
        if clothing:
            top_cls = max(clothing, key=clothing.get)
            # accessories: only trust segformer if it actually sees an accessory class (else it's the
            # garment underneath, e.g. a clutch lying on a sweater)
            if cat != "accessory" or top_cls in CATEGORY_CLASSES["accessory"]:
                a = np.isin(seg, [k for k, v in clothing.items() if v >= 0.25 * clothing[top_cls]])
                cands.append(("dominant", a))
    else:
        cands.append(("category", a))
    # union of garment classes: segformer confuses garment classes on flat lays (trousers -> "Dress",
    # flats -> "Upper-clothes"). Not for accessories; for shoes only when segformer sees no shoes at all
    # (otherwise it would swallow the trouser legs of a worn outfit); bottoms never pull in upper-clothes.
    if cat and cat != "accessory" and not (cat == "shoes" and cat_frac >= 0.05):
        # bottoms: include "Upper-clothes" only as a small confusion region (not a jacket hanging over them)
        drop_upper = cat == "bottom" and counts.get(4, 0) / total > 0.2
        union_cls = (GARMENT_CLASSES - {4} if drop_upper else GARMENT_CLASSES) | wanted
        cands.append(("garment_union", np.isin(seg, list(union_cls))))
    levels = [cands]
    # Dense flat lays: if other (smaller) items cover a big part of this box, any mask will either be a
    # fragment or swallow the neighbours -> use the plain crop (honest, looks like a photo).
    if exclude and inner:
        occ = np.zeros(seg.shape, bool)
        for (ex0, ey0, ex1, ey1) in exclude:
            occ[max(0, ey0):max(0, ey1), max(0, ex0):max(0, ex1)] = True
        x0, y0, x1, y1 = inner
        overlap = float(occ[y0:y1, x0:x1].mean()) if (y1 > y0 and x1 > x0) else 0.0
        info["overlap"] = round(overlap, 2)
        if overlap > 0.3:
            info.update(strategy="fallback_crop", reason="overlapping items")
            return _plain(crop, info)
    chosen = None
    info["candidates"] = {}
    for level in levels:
        scored = []
        for name, m in level:
            if m.sum() < 0.05 * total:
                continue
            m = _clean(m)
            ok, q = mask_quality(m, inner, category_hint)
            scored.append((name, m, ok, q))
            info["candidates"][name] = q | {"ok": bool(ok)}
        good = [x for x in scored if x[2]]
        if good:
            chosen = good[0]
            if len(good) > 1 and not good[0][0].startswith("garment_union"):
                # prefer the union mask when the class mask misses a big part of the garment
                if good[0][1].sum() < 0.92 * good[1][1].sum():
                    chosen = good[1]
            break
    if chosen is None:
        info.update(strategy="fallback_crop")
        return _plain(crop, info)
    strategy, mask, _, q = chosen

    alpha = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    rgba = crop.copy()
    rgba.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v > 20 else 0).getbbox()
    if bbox:
        pad = int(0.03 * max(W, H))
        bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(W, bbox[2] + pad), min(H, bbox[3] + pad))
        rgba = rgba.crop(bbox)
    white = Image.new("RGB", rgba.size, (255, 255, 255))
    white.paste(rgba, mask=rgba.split()[3])
    info.update(strategy=strategy, mask_frac=round(float(mask.sum() / total), 3), mask_quality=q)
    return rgba, white, info


def _plain(crop: Image.Image, info: dict):
    rgba = crop.convert("RGBA")
    return rgba, crop.convert("RGB"), info


_GROUPS = {"top": {4}, "bottom": {5, 6}, "dress": {7}, "shoes": {9, 10}, "bag": {16}, "hat": {1}, "scarf": {17}}


def propose_boxes(img: Image.Image, min_frac: float = 0.015, max_side: int = 640) -> list[dict]:
    """Offline Stage-1 fallback (no Gemini): garment boxes from segformer on the whole image.
    Works well for worn / on-mannequin photos, weaker for flat lays. Returns Gemini-style boxes."""
    img = img.convert("RGB")
    W, H = img.size
    s = min(1.0, max_side / max(W, H))
    small = img.resize((max(1, int(W * s)), max(1, int(H * s)))) if s < 1 else img
    proc, model, mlock = get_segformer()
    import torch
    with mlock, torch.inference_mode():
        logits = model(**proc(images=small, return_tensors="pt")).logits
        seg = torch.nn.functional.interpolate(logits, size=small.size[::-1], mode="bilinear",
                                              align_corners=False).argmax(1)[0].numpy()
    h, w = seg.shape
    boxes = []
    for name, cls in _GROUPS.items():
        m = ndimage.binary_closing(np.isin(seg, list(cls)), structure=np.ones((5, 5)))
        lab, n = ndimage.label(m)
        if n == 0:
            continue
        sizes = ndimage.sum(m, lab, range(1, n + 1))
        # shoes: merge both shoes into one item; others: each big component is an item
        comps = [i + 1 for i, sz in enumerate(sizes) if sz >= min_frac * h * w]
        if name == "shoes" and comps:
            comps = [comps]
        else:
            comps = [[c] for c in comps]
        for group in comps:
            ys, xs = np.where(np.isin(lab, group))
            boxes.append({"box_2d": [int(ys.min() / h * 1000), int(xs.min() / w * 1000),
                                     int(ys.max() / h * 1000), int(xs.max() / w * 1000)],
                          "label": name})
    return boxes


def white_bg_cutout(img: Image.Image, tol: int = 18):
    """Product shot already on white: make the white region connected to the border transparent.
    Returns (rgba, white_rgb)."""
    img = img.convert("RGB")
    a = np.asarray(img).astype(np.int16)
    near_white = (a.min(axis=2) >= 255 - tol)
    lab, n = ndimage.label(near_white)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    bg = np.isin(lab, list(border))
    fg = ndimage.binary_fill_holes(~bg)
    if fg.mean() < 0.03:  # nothing found: keep whole image
        fg = np.ones_like(fg)
    alpha = Image.fromarray((fg * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8))
    rgba = img.copy()
    rgba.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v > 20 else 0).getbbox()
    if bbox:
        rgba = rgba.crop(bbox)
    white = Image.new("RGB", rgba.size, (255, 255, 255))
    white.paste(rgba, mask=rgba.split()[3])
    return rgba, white
