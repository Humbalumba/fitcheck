"""Attribute-driven, canonical-pose prompt for the Gemini image redraw (render option 1).

The prompt is assembled from everything we know about the item: the stored Gemini attributes (category, subcategory,
colours, material/fabric, pattern, brand, description) plus the structured garment details (garment type, which side
was photographed, neckline, sleeve length, closure, hood, pockets, ribbing, fit, length, lining/hardware/stitch
colours and every logo/text/graphic with its position and orientation). Each garment type gets a canonical
brand-catalogue presentation spec so the output looks like an isolated e-commerce product shot, not a cleaned-up
snapshot. The result is not verified: one image call per item (app/render._try_gemini)."""
from __future__ import annotations

import re

# canonical presentation per garment type (what a brand's product page shows)
CANONICAL_SPEC: dict[str, str] = {
    "tshirt": "flat-lay or ghost-mannequin FRONT view of a t-shirt, perfectly symmetric, BOTH short sleeves fully "
              "extended out to the sides at the same angle, crew/V neckline and neck rib clearly shown, straight hem",
    "longsleeve_tee": "ghost-mannequin FRONT view, symmetric, BOTH long sleeves straight down alongside the body "
                      "(slightly angled out), cuffs visible, neckline shown, straight hem",
    "sweater": "ghost-mannequin FRONT view of a sweater, symmetric, BOTH sleeves straight down alongside the body, "
               "ribbed neckline, cuffs and hem band visible",
    "sweatshirt": "ghost-mannequin FRONT view of a sweatshirt, symmetric, BOTH sleeves straight down alongside the "
                  "body, ribbed crew neck, ribbed cuffs and waistband visible",
    "polo": "flat-lay or ghost-mannequin FRONT view of a polo shirt, symmetric, collar laid flat and neatly folded, "
            "button placket fully visible (all buttons shown), BOTH short sleeves extended with their ribbed bands",
    "shirt": "ghost-mannequin FRONT view of a button-up shirt, fully buttoned, collar neat, button placket straight "
             "down the centre, BOTH sleeves straight down with cuffs visible",
    "tank_top": "ghost-mannequin FRONT view, symmetric, both straps/armholes shown, neckline and hem visible",
    "hoodie": "ghost-mannequin FRONT view of a pullover hoodie, symmetric, hood shown standing up behind the neck "
              "with its opening visible, drawstrings hanging evenly, kangaroo pocket visible, BOTH sleeves straight "
              "down alongside the body, ribbed cuffs and waistband",
    "zip_hoodie": "ghost-mannequin FRONT view of a zip-up hoodie, FULLY ZIPPED (or neatly closed) with the zip "
                  "running straight down the centre and the zip pull visible, hood shown behind the neck with its "
                  "opening/lining visible, drawstrings even, BOTH sleeves straight down, pockets visible",
    "jacket": "ghost-mannequin FRONT view of a jacket, zipped/buttoned neatly closed, closure straight down the "
              "centre, collar/hood shown, BOTH sleeves straight down alongside the body, pockets and cuffs visible",
    "coat": "ghost-mannequin FRONT view of a coat, closed, collar/lapels neat, BOTH sleeves straight down, full "
            "length visible",
    "jeans": "flat-lay FRONT view of jeans, perfectly straight: waistband level at the top, button and fly facing "
             "the camera, front pockets and coin pocket visible, BOTH legs straight down side by side, hems visible",
    "trousers": "flat-lay FRONT view of trousers, straight: waistband level, fly/closure facing the camera, BOTH "
                "legs straight down side by side, creases pressed, hems visible",
    "shorts": "flat-lay FRONT view of shorts, straight: waistband level, fly/closure facing the camera, both legs "
              "symmetric, hems visible",
    "skirt": "flat-lay FRONT view of a skirt, waistband level at the top, full length shown, hem evenly spread",
    "dress": "ghost-mannequin FRONT view of a dress, FULL LENGTH from straps/shoulders to hem, straps or sleeves "
             "shown symmetrically, skirt evenly spread",
    "shoes": "studio product shot of the PAIR, 3/4 side view, side by side, laces tidy, soles visible at the edge",
    "accessory": "clean studio product shot, upright, centered, the whole item in frame",
    "other": "clean studio product shot, upright, centered, the whole item in frame",
}
# back-view presentations (the photo shows the back; we never invent the front)
CANONICAL_SPEC_BACK: dict[str, str] = {
    "jeans": "flat-lay BACK view of jeans, perfectly straight: waistband level at the top with its belt loops, back "
             "yoke, BOTH back pockets and any back patch/label visible, BOTH legs straight down side by side, hems "
             "visible",
    "trousers": "flat-lay BACK view of trousers, straight: waistband level at the top, back pockets visible, BOTH "
                "legs straight down side by side, hems visible",
    "shorts": "flat-lay BACK view of shorts, straight: waistband level, back pockets visible, both legs symmetric",
    "skirt": "flat-lay BACK view of a skirt, waistband level at the top, full length shown, hem evenly spread",
    "hoodie": "ghost-mannequin BACK view of a hoodie, symmetric, hood lying neatly against the upper back, BOTH "
              "sleeves straight down alongside the body, ribbed cuffs and waistband",
    "zip_hoodie": "ghost-mannequin BACK view of a zip-up hoodie, symmetric, hood lying neatly against the upper "
                  "back, BOTH sleeves straight down alongside the body, ribbed cuffs and waistband",
}
_BACK_NOTE = ("The photo shows the BACK of this garment. Present it as a symmetric BACK view in the same canonical pose "
              "(do not invent a front design); keep every back graphic exactly where it is.")

_CAT_FALLBACK = {"top": "tshirt", "outerwear": "jacket", "dress": "dress", "bottom": "trousers", "shoes": "shoes",
                 "accessory": "accessory"}


def garment_type_for(item: dict, details: dict | None) -> str:
    d = details or {}
    if d.get("garment_type"):
        return str(d["garment_type"])
    from .render_template import _guess_type, _item_text
    g, _ = _guess_type(_item_text(item))
    if g:
        return g
    cat = (item.get("category") or (item.get("attributes") or {}).get("category") or "").lower()
    return _CAT_FALLBACK.get(cat, "other")


def _fmt_list(v) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x)
    return str(v)


def attribute_lines(item: dict, details: dict | None) -> list[str]:
    a = item.get("attributes") or {}
    d = details or {}
    lines = []
    lines.append(f"Item: {a.get('description') or item.get('label') or 'clothing item'}")
    for key, label in (("subcategory", "type"), ("primary_color", "main colour"), ("colors", "colours"),
                       ("secondary_colors", "secondary colours"), ("material", "material"),
                       ("fabric_guess", "fabric"), ("pattern", "pattern"), ("brand", "brand")):
        v = a.get(key)
        if v:
            lines.append(f"{label}: {_fmt_list(v)}")
    for key, label in (("neckline", "neckline"), ("sleeve_length", "sleeve length"), ("closure", "closure"),
                       ("fit", "fit"), ("length", "length"), ("lining_color", "hood lining colour"),
                       ("hardware_color", "zip/buttons/rivets colour"), ("stitch_color", "topstitching colour")):
        v = d.get(key)
        if v and str(v).lower() not in ("none", "null", ""):
            lines.append(f"{label}: {str(v).replace('_', ' ')}")
    if d.get("has_hood"):
        lines.append("hood: yes")
    if d.get("has_front_pockets"):
        lines.append("front pockets: yes")
    if d.get("ribbed_hem_cuffs"):
        lines.append("ribbed cuffs and hem: yes")
    return lines


def graphic_lines(details: dict | None) -> list[str]:
    out = []
    for g in (details or {}).get("graphics") or []:
        if not isinstance(g, dict):
            continue
        what = g.get("description") or g.get("kind") or "graphic"
        txt = f' reading "{g["text"]}"' if g.get("text") else ""
        pos = str(g.get("position") or "where it is in the photo").replace("_", " ")
        exact = (" -- spell it exactly as printed, same font style and colours" if g.get("text") else
                 " -- copy its exact shapes and strokes; do NOT redraw it as a letter, word or a different symbol")
        out.append(f"- {what}{txt} ({g.get('kind') or 'graphic'}) on the {pos}: reproduce it exactly, same size "
                   f"and position relative to the garment{exact}")
    if out:  # descriptions come from a text model and can misname colours/materials; the pixels are the truth
        out.append("- Take every logo's, patch's and label's exact colours and material look from the PHOTO (images 1-2),"
                   " not from the wording above.")
    return out


RENDER_PROMPT_V2 = """You are a professional e-commerce product photographer and retoucher for a fashion brand.
Image 1 is ONE garment segmented from a customer's photo (background removed). It may be wrinkled, crumpled, folded,
tilted, rotated, partly occluded or cut off. Image 2 is the original photo region for context only (other items may be
visible there: ignore them; use it to read colours, logos and printed text more accurately).{pose_ref}

WHAT WE KNOW ABOUT THIS GARMENT:
{attributes}

PRODUCE: an isolated brand-catalogue product image of THIS EXACT garment, the way a brand shows it on its product page:
- Presentation: {spec}.{back}
- ORIENTATION: first rotate the WHOLE garment so it is upright (collar/neck or waistband at the top, perfectly
  vertical centre line) -- the photo may show it tilted, sideways or upside down. Logos, prints and text rotate
  TOGETHER with the garment: keep each one's orientation relative to the garment (collar up). Prints are normally
  square to the garment's vertical axis; a small tilt in the photo comes from the fabric being crumpled, so
  straighten it with the fabric. Text must read left-to-right, upright, never mirrored.
- Centered, the whole garment in frame with an even margin, on a PURE WHITE (#FFFFFF) background, soft even studio
  lighting, only a faint soft shadow.
- Pressed and smoothed: NO wrinkles, creases or folds; symmetric; sleeves/legs straightened into the pose above.
- COMPLETE every part that is hidden, folded under, tucked, cropped or occluded in the photo (e.g. a folded sleeve,
  the hood, the far leg, the hem) consistently with the attributes above and the visible parts.
{graphics}MUST PRESERVE EXACTLY (a real item the customer owns): the exact colour and shade, fabric texture, pattern,
every logo / printed or embroidered text (identical spelling, font style, colours), graphics and their size and
position, buttons, zippers, pockets, drawstrings, collar/hood, neckline, sleeve length, hem length, cut and silhouette.
Keep plain areas plain: no invented seams, labels, pockets, drawstrings, zips, buttons or prints.
DO NOT add anything that is not in the photo: no new logos, text, labels or tags, no model or person, no hanger, no
mannequin stand, no props, no watermark. Do not change the colour. Output only the image."""


POSE_REF_NOTE = """
Image 3 is a SCHEMATIC of the target catalogue layout generated from the attributes: use it ONLY for pose, symmetry,
upright orientation, proportions, framing and where/how upright the logos sit. Take everything about the garment's
appearance (exact colour, fabric texture, seams, construction details, graphics and text) from images 1 and 2. The
result must look like a real studio PHOTOGRAPH of the customer's garment, not a drawing or a flat illustration."""


def _adjust_spec(spec: str, gtype: str, d: dict, is_back: bool = False) -> str:
    """Make the canonical spec agree with the garment's known construction."""
    if gtype in ("hoodie", "zip_hoodie"):
        if d.get("has_drawstrings") is False:
            spec = (spec.replace("drawstrings hanging evenly, ", "").replace("drawstrings even, ", "")
                    + ", NO drawstrings (this hood has none)")
        if d.get("has_hood") is False:
            spec = spec.replace("hood shown behind the neck with its opening/lining visible, ", "")
    if d.get("has_front_pockets") is False and not is_back:
        spec = (spec.replace(", kangaroo pocket visible", "").replace(", pockets visible", "")
                .replace(", pockets and cuffs visible", ", cuffs visible") + ", no front pockets")
    closure = str(d.get("closure") or "")
    if gtype in ("jacket", "coat", "shirt") and closure == "buttons":
        spec = spec.replace("zipped/buttoned", "buttoned") + ", every button shown"
    if gtype == "jacket" and closure == "full_zip":
        spec = spec.replace("zipped/buttoned", "fully zipped")
    if closure == "half_zip":
        spec += ", half-zip closed at the neck with the zip pull visible"
    sl = str(d.get("sleeve_length") or "")
    if gtype == "dress" and sl in ("short", "long", "three_quarter"):
        spec += f", {sl.replace('_', '-')} sleeves shown symmetrically"
    if d.get("lining_color") and gtype in ("hoodie", "zip_hoodie") and not is_back:
        spec += f", {d['lining_color']} hood lining visible inside the hood opening"
    return spec


def build_render_prompt(item: dict, details: dict | None, pose_ref: bool = False) -> str:
    gtype = garment_type_for(item, details)
    spec = CANONICAL_SPEC.get(gtype, CANONICAL_SPEC["other"])
    d = details or {}
    is_back = str(d.get("view") or "").lower() == "back"
    if is_back:
        spec = CANONICAL_SPEC_BACK.get(gtype) or spec.replace("FRONT view", "BACK view")
    spec = _adjust_spec(spec, gtype, d, is_back)
    neck = str(d.get("neckline") or "")
    if neck == "v_neck":
        spec = spec.replace("crew/V neckline", "V neckline").replace("ribbed neckline", "ribbed V neckline")
    elif neck in ("crew", "scoop", "mock_neck", "turtleneck"):
        spec = spec.replace("crew/V neckline", f"{neck.replace('_', ' ')} neckline")
    if gtype in ("tshirt",) and str(d.get("sleeve_length") or "") == "long":
        spec = CANONICAL_SPEC["longsleeve_tee"]
    back = (" " + _BACK_NOTE) if is_back else ""
    gl = graphic_lines(details)
    graphics = ("LOGOS / TEXT / GRAPHICS ON IT (real, keep them):\n" + "\n".join(gl) + "\n") if gl else \
        ("It has NO logos or text unless clearly visible in the photo; do not add any.\n"
         if details is not None else "")
    attrs = "\n".join("- " + ln for ln in attribute_lines(item, details))
    return RENDER_PROMPT_V2.format(attributes=attrs, spec=spec, back=back, graphics=graphics,
                                   pose_ref=POSE_REF_NOTE if pose_ref else "")
