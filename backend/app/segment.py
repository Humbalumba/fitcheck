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


def segment_crop(crop: Image.Image, category_hint: str | None, max_side: int = 512):
    """Return (cutout_rgba, white_rgb, info). Falls back to the plain crop if the mask is poor."""
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
    wanted = CATEGORY_CLASSES.get(category_hint or "", set())
    mask = np.isin(seg, list(wanted)) if wanted else np.zeros_like(seg, bool)
    strategy = "category"
    if mask.sum() < 0.05 * total:
        # label didn't match what segformer sees (common for flat lays): use dominant clothing classes
        clothing = {k: v for k, v in counts.items() if k in CLOTHING_CLASSES}
        if clothing:
            top_cls = max(clothing, key=clothing.get)
            keep = {k for k, v in clothing.items() if v >= 0.25 * clothing[top_cls]}
            mask = np.isin(seg, list(keep))
            strategy = "dominant"
    info = {"classes": {SEG_LABELS[k]: round(v / total, 3) for k, v in counts.items() if v / total > 0.01}}

    frac = mask.sum() / total
    if frac < 0.08:
        info.update(strategy="fallback_crop", mask_frac=round(float(frac), 3))
        return _plain(crop, info)

    # clean: close small gaps, keep largest component (+ big secondary parts e.g. two shoes), fill holes
    mask = ndimage.binary_closing(mask, structure=np.ones((5, 5)), iterations=2)
    lab, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        biggest = sizes.max()
        keep_ids = [i + 1 for i, s in enumerate(sizes) if s >= 0.3 * biggest]
        mask = np.isin(lab, keep_ids)
    mask = ndimage.binary_fill_holes(mask)
    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    frac = mask.sum() / total
    if frac < 0.08:
        info.update(strategy="fallback_crop", mask_frac=round(float(frac), 3))
        return _plain(crop, info)

    alpha = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    rgba = crop.copy()
    rgba.putalpha(alpha)
    bbox = alpha.point(lambda a: 255 if a > 20 else 0).getbbox()
    if bbox:
        pad = int(0.03 * max(W, H))
        bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(W, bbox[2] + pad), min(H, bbox[3] + pad))
        rgba = rgba.crop(bbox)
    white = Image.new("RGB", rgba.size, (255, 255, 255))
    white.paste(rgba, mask=rgba.split()[3])
    info.update(strategy=strategy, mask_frac=round(float(frac), 3))
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
