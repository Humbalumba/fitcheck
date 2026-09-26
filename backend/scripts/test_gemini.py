#!/usr/bin/env python
"""Gemini Stage 1 (boxes) + Stage 2 (attributes JSON) smoke test on data/test_images/.

  GEMINI_API_KEY=... python scripts/test_gemini.py [image ...]
Runs the real detect pipeline against a throwaway DB (the demo closet is untouched) and prints, per photo,
the chosen model, every detected box/label, segmentation strategy and the structured attributes.
Also writes an annotated preview per photo to /tmp/gemini_<name>.jpg.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.environ["FITCHECK_DB"] = str(Path(tempfile.mkdtemp()) / "gemini_test.db")
sys.path.insert(0, str(BACKEND))

from PIL import Image, ImageDraw  # noqa: E402

from app import config, gemini, pipeline  # noqa: E402

if not gemini.is_configured():
    sys.exit("GEMINI_API_KEY not set")
MODEL = gemini.model_name()
TAG = os.environ.get("GEMINI_TAG") or MODEL
print("model:", MODEL)
_fn = "detect_items" if config.GEMINI_MODE == "single" else "detect_boxes"
_orig_detect = getattr(gemini, _fn)
_t_detect = {}


def _timed_detect(img):
    t0 = time.time()
    try:
        return _orig_detect(img)
    finally:
        _t_detect["s"] = time.time() - t0


setattr(gemini, _fn, _timed_detect)
print("mode:", config.GEMINI_MODE)
summary = []
imgs = sys.argv[1:] or [str(p) for p in sorted(config.TEST_IMAGES_DIR.glob("*.jpg"))
                        if not p.name.startswith("product_")] + [str(config.TEST_IMAGES_DIR / "product_dress_womens.jpg")]
for path in imgs:
    t = time.time()
    res = pipeline.detect(Path(path).read_bytes(), "closet", source="gemini_test")
    total = time.time() - t
    print(f"\n=== {Path(path).name}: detector={res['detector']} items={len(res['items'])} "
          f"(boxes {_t_detect.get('s', 0):.1f}s, total {total:.1f}s)")
    summary.append((Path(path).name, len(res["items"]), _t_detect.get("s", 0), total))
    print("  model used:", gemini._model_name)
    im = Image.open(config.MEDIA_DIR / res["image_url"][len("/media/"):]).convert("RGB")
    d = ImageDraw.Draw(im)
    for it in res["items"]:
        a = it["attributes"]
        y0, x0, y1, x1 = it["bbox"]
        W, H = im.size
        d.rectangle([x0 * W / 1000, y0 * H / 1000, x1 * W / 1000, y1 * H / 1000], outline="red", width=3)
        d.text((x0 * W / 1000 + 4, y0 * H / 1000 + 4), it["label"], fill="red")
        print(f"  - {it['label']!r} bbox={it['bbox']} seg={a.get('segmentation')} src={a.get('source')}")
        print("    ", json.dumps({k: a.get(k) for k in ("category", "subcategory", "primary_color", "secondary_colors",
                                                        "pattern", "fabric_guess", "formality", "formality_label",
                                                        "seasons", "style_tags", "gender_presentation", "brand",
                                                        "price", "currency", "description")}))
    im.save(f"/tmp/gemini_{TAG}_{Path(path).stem}.jpg")
    # cutout contact sheet (white-background cutouts as the closet will show them)
    cuts = [Image.open(config.MEDIA_DIR / it["image_url"][len("/media/"):]).convert("RGB") for it in res["items"]]
    if cuts:
        sheet = Image.new("RGB", (200 * min(len(cuts), 6), 220 * ((len(cuts) + 5) // 6)), "white")
        sd = ImageDraw.Draw(sheet)
        for k, (c, it) in enumerate(zip(cuts, res["items"])):
            c.thumbnail((196, 196))
            sheet.paste(c, ((k % 6) * 200 + 2, (k // 6) * 220 + 2))
            sd.text(((k % 6) * 200 + 2, (k // 6) * 220 + 202),
                    f"{it['attributes'].get('segmentation')}|{it['category']}", fill="black")
        sheet.save(f"/tmp/gemini_{TAG}_{Path(path).stem}_cuts.jpg")

print("\nSUMMARY", TAG)
for name, n, tb, tt in summary:
    print(f"  {name:32s} items={n:2d} boxes={tb:5.1f}s total={tt:5.1f}s")
