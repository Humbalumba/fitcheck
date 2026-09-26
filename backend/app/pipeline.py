"""Stage 1 (detect + segment) and Stage 2 (identify) orchestration, plus closet mutations."""
from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from . import config, db, gemini
from . import render as _render
from .fallback import zero_shot_attributes
from .scoring import COMPAT_KIND, ensure_compat_embeddings
from .segment import guess_category_from_label, propose_boxes, segment_crop
from .vectors import closet_index, ensure_fclip

log = logging.getLogger("fitcheck.pipeline")

try:  # iPhone HEIC uploads
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # pragma: no cover
    pass

_executor = ThreadPoolExecutor(max_workers=config.GEMINI_MAX_PARALLEL)


def item_to_api(it: dict) -> dict:
    a = dict(it.get("attributes") or {})
    original = config.media_url(it.get("white_path")) or config.media_url(it.get("crop_path"))
    r = _render.api_fields(it)  # clean product image (app/render.py); never raises
    return {
        "id": it["id"],
        "status": it["status"],
        "category": it.get("category") or a.get("category"),
        # display image: the clean render when ready, else the cutout on white
        "image_url": r["clean_image_url"] or original,
        "original_image_url": original,
        "clean_image_url": r["clean_image_url"],
        "clean_method": r["clean_method"],
        "render_status": r["render_status"],
        "render_checks": r["render_checks"],
        "cutout_url": config.media_url(it.get("cutout_path")) or config.media_url(it.get("white_path")),
        "crop_url": config.media_url(it.get("crop_path")),
        "bbox": it.get("bbox"),
        "label": it.get("label"),
        "photo_id": it.get("photo_id"),
        "attributes": a,
        "created_at": it.get("created_at"),
    }


def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    s = config.MAX_IMAGE_SIDE / max(img.size)
    if s < 1:
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    return img


def _box_px(box, W, H, pad_frac):
    y0, x0, y1, x1 = box
    x0, x1 = x0 / 1000 * W, x1 / 1000 * W
    y0, y1 = y0 / 1000 * H, y1 / 1000 * H
    pw, ph = (x1 - x0) * pad_frac + 4, (y1 - y0) * pad_frac + 4
    return (int(max(0, x0 - pw)), int(max(0, y0 - ph)), int(min(W, x1 + pw)), int(min(H, y1 + ph)))


def _identify(white: Image.Image, context: Image.Image, label: str) -> dict:
    if gemini.is_configured():
        try:
            return gemini.identify(white, context, label)
        except Exception as e:
            log.warning("Gemini identify failed (%s: %s); using zero-shot fallback", type(e).__name__, str(e)[:200])
    return zero_shot_attributes(white, label)


def detect(data: bytes, purpose: str | None = None, source: str = "upload") -> dict:
    img = load_image(data)
    W, H = img.size
    stem = db.new_id("")
    orig_path = config.MEDIA_DIR / "originals" / f"{stem}.jpg"
    img.save(orig_path, quality=90)
    photo_id = db.insert_photo(str(orig_path), purpose, W, H, source)

    boxes, detector = [], "none"
    if gemini.is_configured():
        try:
            boxes = gemini.detect_items(img) if config.GEMINI_MODE == "single" else gemini.detect_boxes(img)
            detector = "gemini"
        except Exception as e:
            log.warning("Gemini detection failed (%s: %s); using whole image", type(e).__name__, str(e)[:200])
    if not boxes and detector != "gemini":
        try:
            boxes = propose_boxes(img)
            detector = "segformer"
        except Exception as e:
            log.warning("segformer box proposal failed: %s", e)
    if not boxes:
        boxes = [{"box_2d": [0, 0, 1000, 1000], "label": "clothing item"}]
        detector = detector if detector == "gemini" else "whole_image"

    def _area(bx):
        return (bx[2] - bx[0]) * (bx[3] - bx[1])

    futures = []
    for n, b in enumerate(boxes):
        crop_box = _box_px(b["box_2d"], W, H, config.CROP_PAD_FRAC)
        crop = img.crop(crop_box)
        ctx = img.crop(_box_px(b["box_2d"], W, H, 0.35))
        ctx.thumbnail((1024, 1024))
        hint = (b.get("attributes") or {}).get("category") or guess_category_from_label(b["label"])
        bx0, by0, bx1, by1 = _box_px(b["box_2d"], W, H, 0.0)
        inner = (bx0 - crop_box[0], by0 - crop_box[1], bx1 - crop_box[0], by1 - crop_box[1])
        exclude = []  # smaller items lying on/inside this one (flat lays): keep them out of this cutout
        for m, o in enumerate(boxes):
            if m == n or _area(o["box_2d"]) >= 0.8 * _area(b["box_2d"]):
                continue
            ox0, oy0, ox1, oy1 = _box_px(o["box_2d"], W, H, 0.0)
            if ox1 <= crop_box[0] or ox0 >= crop_box[2] or oy1 <= crop_box[1] or oy0 >= crop_box[3]:
                continue
            exclude.append((ox0 - crop_box[0], oy0 - crop_box[1], ox1 - crop_box[0], oy1 - crop_box[1]))
        rgba, white, seg_info = segment_crop(crop, hint, inner=inner, exclude=exclude)
        base = f"{stem}_{n}"
        paths = {
            "crop": config.MEDIA_DIR / "crops" / f"{base}.jpg",
            "cutout": config.MEDIA_DIR / "cutouts" / f"{base}.png",
            "white": config.MEDIA_DIR / "white" / f"{base}.jpg",
            "context": config.MEDIA_DIR / "context" / f"{base}.jpg",
        }
        crop.save(paths["crop"], quality=90)
        rgba.save(paths["cutout"])
        white.save(paths["white"], quality=92)
        ctx.save(paths["context"], quality=85)
        if b.get("attributes"):  # single-call mode: attributes came with the box
            futures.append((b, paths, seg_info, None))
        else:
            futures.append((b, paths, seg_info, _executor.submit(_identify, white, ctx, b["label"])))

    items = []
    for b, paths, seg_info, fut in futures:
        attrs = dict(b["attributes"]) if fut is None else fut.result()
        attrs["segmentation"] = seg_info.get("strategy")
        iid = db.insert_item(status="detected", category=attrs.get("category"), attributes=attrs, photo_id=photo_id,
                             bbox=b["box_2d"], label=b["label"], crop_path=paths["crop"],
                             cutout_path=paths["cutout"], white_path=paths["white"], context_path=paths["context"],
                             source=source)
        items.append(item_to_api(db.get_item(iid)))
    return {"photo_id": photo_id, "image_url": config.media_url(orig_path), "detector": detector, "items": items}


def _merge_attrs(item: dict, partial: dict) -> dict:
    a = dict(item.get("attributes") or {})
    for k, v in (partial or {}).items():
        a[k] = v
    if "formality" in partial:
        try:
            a["formality"] = int(max(1, min(5, int(a["formality"]))))
            a["formality_label"] = gemini.FORMALITY_LABELS[a["formality"]]
        except Exception:
            pass
    if "price" in (partial or {}):
        # a price typed by the user always wins over tags and estimates (the estimate stays as a fallback)
        if a.get("price") not in (None, ""):
            a["price_source"] = "user"
        else:
            a.pop("price_source", None)
    if partial:
        a["edited"] = True
    return a


def update_attributes(item_id: str, partial: dict) -> dict:
    it = db.get_item(item_id)
    if it is None:
        raise KeyError(item_id)
    a = _merge_attrs(it, partial)
    db.update_item(item_id, attributes=a, category=a.get("category"))
    # text-dependent compat embedding + cached scores are stale now
    with db.get_conn() as c:
        c.execute("DELETE FROM item_embeddings WHERE item_id=? AND kind=?", (item_id, COMPAT_KIND))
    db.invalidate_compat(item_id)
    it = db.get_item(item_id)
    if it["status"] == "closet":
        ensure_compat_embeddings([it])
    return it


def add_to_closet(item_ids: list[str], overrides: dict | None = None, purchased: bool = False) -> list[dict]:
    out = []
    for iid in item_ids:
        it = db.get_item(iid)
        if it is None:
            raise KeyError(iid)
        if overrides and iid in overrides:
            it = update_attributes(iid, overrides[iid])
        if purchased:
            a = dict(it["attributes"])
            a["purchased_at"] = db.now_iso()
            if a.get("price") is not None:
                a["purchase_price"] = a["price"]
            db.update_item(iid, attributes=a)
        db.update_item(iid, status="closet")
        it = db.get_item(iid)
        ensure_fclip(it)
        closet_index.add(it)
        ensure_compat_embeddings([it])
        out.append(it)
    _render.schedule([i["id"] for i in out])  # clean product image in the background (never raises)
    return out


def delete_item(item_id: str) -> None:
    closet_index.remove(item_id)
    it = db.get_item(item_id)
    _render.forget(item_id)
    db.delete_item(item_id)
    if it and it.get("source") != "seed":
        for k in ("cutout_path", "white_path", "context_path"):
            if it.get(k):
                Path(it[k]).unlink(missing_ok=True)
