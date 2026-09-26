"""Offline fallback for Stage 2 when Gemini is unavailable (no key / quota / network).

Zero-shot fashion-clip classification of the white-background cutout. Lower quality than Gemini,
but keeps the demo working. Attributes carry source='fashion-clip-zero-shot' so the UI can flag them.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .gemini import FORMALITY_LABELS

# subcategory -> (category, default formality, seasons)
SUBCATS = {
    "t-shirt": ("top", 1, ["spring", "summer", "fall"]), "polo shirt": ("top", 2, ["spring", "summer"]),
    "button-down shirt": ("top", 3, ["spring", "summer", "fall", "winter"]), "blouse": ("top", 3, ["spring", "summer", "fall"]),
    "sweater": ("top", 2, ["fall", "winter"]), "hoodie": ("top", 1, ["fall", "winter"]),
    "sweatshirt": ("top", 1, ["fall", "winter"]), "tank top": ("top", 1, ["summer"]),
    "jeans": ("bottom", 2, ["spring", "fall", "winter"]), "chinos": ("bottom", 3, ["spring", "summer", "fall"]),
    "dress trousers": ("bottom", 4, ["spring", "fall", "winter"]), "shorts": ("bottom", 1, ["summer"]),
    "skirt": ("bottom", 2, ["spring", "summer"]), "leggings": ("bottom", 1, ["fall", "winter"]),
    "track pants": ("bottom", 1, ["spring", "fall"]),
    "dress": ("dress", 3, ["spring", "summer"]), "jumpsuit": ("dress", 2, ["spring", "summer"]),
    "blazer": ("outerwear", 4, ["spring", "fall", "winter"]), "jacket": ("outerwear", 2, ["spring", "fall"]),
    "denim jacket": ("outerwear", 2, ["spring", "fall"]), "coat": ("outerwear", 3, ["fall", "winter"]),
    "puffer jacket": ("outerwear", 1, ["winter"]), "cardigan": ("outerwear", 2, ["spring", "fall"]),
    "sneakers": ("shoes", 1, ["spring", "summer", "fall"]), "boots": ("shoes", 2, ["fall", "winter"]),
    "heels": ("shoes", 4, ["spring", "summer", "fall"]), "sandals": ("shoes", 1, ["summer"]),
    "loafers": ("shoes", 3, ["spring", "fall"]),
}
# Accessories are NOT part of FitCheck (clothes and shoes only). They are still zero-shot prompts so an accessory
# photo is recognised as one (and dropped by the pipeline) instead of being forced into the nearest garment type.
ACCESSORY_SUBCATS = ["handbag", "backpack", "belt", "hat", "baseball cap", "scarf", "sunglasses", "necklace",
                     "bracelet", "watch", "necktie", "gloves", "socks"]
ACCESSORY_MIN_PROB = 0.5  # only call it an accessory when fashion-clip is fairly sure (never drop a real garment lightly)
COLORS = ["black", "white", "grey", "navy", "blue", "light blue", "red", "burgundy", "pink", "purple", "green",
          "olive", "yellow", "orange", "brown", "beige", "cream", "khaki"]
PATTERNS = ["solid", "striped", "plaid", "floral", "graphic print", "polka dot", "camouflage", "denim wash"]

_text_cache: dict[str, np.ndarray] = {}


def _texts(key: str, prompts: list[str]) -> np.ndarray:
    if key not in _text_cache:
        from .vectors import embed_texts
        _text_cache[key] = embed_texts(prompts)
    return _text_cache[key]


def _best(img_vec: np.ndarray, key: str, labels: list[str], template: str):
    T = _texts(key, [template.format(l) for l in labels])
    sims = T @ img_vec
    p = np.exp(sims * 100); p /= p.sum()
    i = int(np.argmax(p))
    return labels[i], float(p[i])


def zero_shot_attributes(white: Image.Image, label: str = "", img_vec: np.ndarray | None = None,
                         restrict_category: str | None = None) -> dict:
    from .vectors import embed_images
    v = img_vec if img_vec is not None else embed_images([white])[0]
    subs = [k for k, x in SUBCATS.items() if restrict_category in (None, x[0])] or list(SUBCATS)
    if restrict_category is None:  # open classification: accessories compete, and win only when clearly one
        sub, p = _best(v, "sub:any+acc", subs + ACCESSORY_SUBCATS, "a photo of a {}")
        if sub in ACCESSORY_SUBCATS and p >= ACCESSORY_MIN_PROB:
            return {"category": None, "subcategory": sub, "accessory": True, "description": sub,
                    "source": "fashion-clip-zero-shot"}
    sub, _ = _best(v, f"sub:{restrict_category}", subs, "a photo of a {}")
    color, _ = _best(v, "color", COLORS, "a photo of a {} garment")
    pattern, _ = _best(v, "pattern", PATTERNS, "a photo of a {} garment")
    T = _texts("gender", ["men's clothing", "women's clothing"])
    g = T @ v
    gender = "unisex" if abs(g[0] - g[1]) < 0.01 else ("mens" if g[0] > g[1] else "womens")
    cat, formality, seasons = SUBCATS[sub]
    if cat == "dress":
        gender = "womens"
    from .pricing import table_estimate
    out = {
        "category": cat, "subcategory": sub, "primary_color": color, "secondary_colors": [],
        "pattern": pattern, "fabric_guess": "unknown", "formality": formality,
        "formality_label": FORMALITY_LABELS[formality], "seasons": seasons, "style_tags": [],
        "gender_presentation": gender, "brand": None, "price": None, "currency": None,
        "description": f"{color} {pattern + ' ' if pattern != 'solid' else ''}{sub}",
        "source": "fashion-clip-zero-shot",
    }
    out.update(table_estimate(out, cat))  # deterministic per-type median price (app/data/price_defaults.json)
    return out
