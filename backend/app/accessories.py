"""FitCheck handles clothes and shoes only: accessories are dropped everywhere, from scanning to saving.

Accessories = bags, jewellery, hats / caps, belts, scarves, sunglasses / eyewear, watches, gloves, ties, socks...
They are never detected (Gemini schema has no 'accessory' category and the prompt says to ignore them; parsed
results are filtered again here), can't be saved to the closet, get a friendly "clothes and shoes only" answer
from "Should I buy?" and are never suggested as products.

`is_accessory` looks at the category and at the HEAD word (last word) of the subcategory / label, so
"tote bag", "baseball cap" and "silver bangle" are accessories, while "cap-sleeve top", "tie-dye t-shirt",
"belted dress" and "scarf-print blouse" are not.
"""
from __future__ import annotations

import re

from . import config

ACCESSORY_WORDS = frozenset("""
accessory accessories
bag bags handbag handbags purse purses tote totes clutch clutches satchel satchels backpack backpacks rucksack
crossbody wallet wallets pouch duffel duffle
jewelry jewellery jewel jewels necklace necklaces bracelet bracelets bangle bangles earring earrings ring rings
pendant pendants brooch anklet anklets cufflinks
hat hats cap caps beanie beanies beret berets fedora fedoras visor visors headband headbands bandana bandanas
bandanna headwear snapback
belt belts suspenders
scarf scarves shawl shawls
sunglasses sunglass glasses eyeglasses eyewear shades goggles
watch watches smartwatch
glove gloves mittens mitten
tie ties necktie neckties bowtie bowties
sock socks
keychain umbrella scrunchie hairclip
""".split())

# category values that mean "accessory" (the old schema value + things a model might put there)
_ACCESSORY_CATEGORIES = ACCESSORY_WORDS | {"bags", "jewelry", "jewellery", "eyewear", "headwear"}

MESSAGE = ("FitCheck only checks clothes and shoes right now, so accessories like bags, hats and jewellery "
           "aren't supported.")
SAVE_MESSAGE = ("Accessories can't be added to your closet: FitCheck only handles clothes and shoes right now "
                "(tops, bottoms, outerwear, dresses and shoes).")
NO_CLOTHES_MESSAGE = ("No clothes found in this photo. FitCheck only picks up clothes and shoes, so accessories "
                      "like bags, hats and jewellery are skipped.")

_SPLIT = re.compile(r"[^a-z]+")


def head_word(text: str | None) -> str:
    """Last word of a short type phrase ('black tote bag' -> 'bag', 't-shirt' -> 'shirt')."""
    words = [w for w in _SPLIT.split((text or "").lower()) if w]
    return words[-1] if words else ""


def is_accessory_category(category: str | None) -> bool:
    c = (category or "").strip().lower()
    return bool(c) and c in _ACCESSORY_CATEGORIES


def is_accessory(category: str | None = None, subcategory: str | None = None, label: str | None = None) -> bool:
    """True if the category is an accessory, or the head word of the subcategory (else of the label) is one.
    The label is only a fallback: a label like 'jeans with belt' must not hide the 'jeans' subcategory."""
    if is_accessory_category(category):
        return True
    text = subcategory if (subcategory or "").strip() else label
    return head_word(text) in ACCESSORY_WORDS


def is_accessory_item(it: dict | None) -> bool:
    """For items / attribute dicts / suggestion products (category, subcategory, and the label or product name)."""
    if not it:
        return False
    a = it.get("attributes") or {}
    cat = it.get("category") or a.get("category")
    sub = a.get("subcategory") or it.get("subcategory")
    return is_accessory(cat, sub, it.get("label")) or (bool(it.get("name")) and is_accessory(None, None, it["name"]))
