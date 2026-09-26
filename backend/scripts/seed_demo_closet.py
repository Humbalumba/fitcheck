#!/usr/bin/env python
"""Seed a generic demo closet (~40 items, men's + women's) from ashraq/fashion-product-images-small.

Idempotent: items get deterministic ids ("seed_<productId>") and are skipped if present.
  python scripts/seed_demo_closet.py            # add anything missing
  python scripts/seed_demo_closet.py --reset    # wipe DB, FAISS and media, then reseed
Attributes come from the dataset labels (no Gemini). Also seeds a handful of held-out *candidate*
items (status='candidate', ids 'seedcand_<id>') used by tests, and writes their images to
data/test_images/.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import logging
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app import config, db  # noqa: E402

log = logging.getLogger("seed")
DATASET = "ashraq/fashion-product-images-small"

# (gender, articleType, baseColour|None, usage|None, name-regex|None)
CLOSET_SPEC = [
    # --- men's
    ("Men", "Tshirts", "Navy Blue", "Casual", r"solid"),
    ("Men", "Tshirts", "White", "Casual", None),
    ("Men", "Tshirts", "Black", "Casual", None),
    ("Men", "Tshirts", "Grey", "Casual", None),
    ("Men", "Shirts", "Blue", "Formal", None),
    ("Men", "Shirts", "White", "Formal", None),
    ("Men", "Shirts", "Red", "Casual", r"check"),
    ("Men", "Sweatshirts", "Grey", None, None),
    ("Men", "Sweaters", "Navy Blue", None, None),
    ("Men", "Jeans", "Blue", "Casual", None),
    ("Men", "Jeans", "Black", "Casual", None),
    ("Men", "Trousers", "Beige", "Casual", None),
    ("Men", "Trousers", "Black", "Formal", None),
    ("Men", "Shorts", "Navy Blue", None, None),
    ("Men", "Track Pants", "Black", None, None),
    ("Men", "Jackets", "Black", None, None),
    ("Men", "Blazers", None, None, None),
    ("Men", "Jackets", "Olive", None, None),
    ("Men", "Casual Shoes", "White", None, None),
    ("Men", "Formal Shoes", "Brown", None, None),
    # --- women's
    ("Women", "Tops", "White", None, None),
    ("Women", "Tops", "Pink", None, None),
    ("Women", "Tops", "Black", None, None),
    ("Women", "Tshirts", "Blue", None, None),
    ("Women", "Shirts", "White", None, None),
    ("Women", "Sweaters", None, None, None),
    ("Women", "Jeans", "Blue", None, None),
    ("Women", "Skirts", "Black", None, None),
    ("Women", "Skirts", None, None, r"print|floral|pleat"),
    ("Women", "Trousers", "Black", None, None),
    ("Women", "Leggings", None, None, None),
    ("Women", "Shorts", None, None, None),
    ("Women", "Dresses", "Black", None, None),
    ("Women", "Dresses", "Red", None, None),
    ("Women", "Dresses", "Blue", None, None),
    ("Women", "Jackets", None, None, r"denim"),
    ("Women", "Jackets", "Black", None, None),
    ("Women", "Shrug", None, None, None),
    ("Women", "Heels", "Black", None, None),
    ("Women", "Flats", None, None, None),
]
# held-out candidates (NOT in closet): used by tests + as sample buy-flow images
CANDIDATE_SPEC = [
    ("navy_tshirt_mens", ("Men", "Tshirts", "Navy Blue", "Casual", r"solid"), 18.0),  # ~dup of closet
    ("olive_shirt_mens", ("Men", "Shirts", "Olive", None, None), 35.0),
    ("khaki_chinos_mens", ("Men", "Trousers", "Khaki", None, None), 45.0),
    ("denim_jacket_womens", ("Women", "Jackets", "Blue", None, None), 60.0),
    ("floral_dress_womens", ("Women", "Dresses", None, None, r"print|floral"), 40.0),
    ("white_sneakers_mens", ("Men", "Sports Shoes", "White", None, None), 70.0),
    ("pink_top_womens", ("Women", "Tops", "Pink", None, None), 22.0),
]

ARTICLE_MAP = {  # articleType -> (category, subcategory)
    "Tshirts": ("top", "t-shirt"), "Shirts": ("top", "button-down shirt"), "Tops": ("top", "top"),
    "Sweatshirts": ("top", "sweatshirt"), "Sweaters": ("top", "sweater"), "Tunics": ("top", "tunic"),
    "Jeans": ("bottom", "jeans"), "Trousers": ("bottom", "trousers"), "Shorts": ("bottom", "shorts"),
    "Skirts": ("bottom", "skirt"), "Track Pants": ("bottom", "track pants"), "Leggings": ("bottom", "leggings"),
    "Capris": ("bottom", "capris"), "Dresses": ("dress", "dress"), "Jumpsuit": ("dress", "jumpsuit"),
    "Jackets": ("outerwear", "jacket"), "Blazers": ("outerwear", "blazer"), "Shrug": ("outerwear", "shrug"),
    "Waistcoat": ("outerwear", "waistcoat"), "Rain Jacket": ("outerwear", "rain jacket"),
    "Casual Shoes": ("shoes", "sneakers"), "Sports Shoes": ("shoes", "sports shoes"),
    "Formal Shoes": ("shoes", "formal shoes"), "Heels": ("shoes", "heels"), "Flats": ("shoes", "flats"),
}
USAGE_FORMALITY = {"Sports": 1, "Casual": 2, "Smart Casual": 3, "Travel": 2, "Party": 4, "Formal": 4}
COLOR_MAP = {"navy blue": "navy", "off white": "off-white"}


def _parquets():
    from huggingface_hub import snapshot_download
    root = snapshot_download(DATASET, repo_type="dataset")
    return sorted(glob.glob(f"{root}/**/*.parquet", recursive=True))


def load_meta():
    import pandas as pd
    import pyarrow.parquet as pq
    cols = ["id", "gender", "masterCategory", "subCategory", "articleType", "baseColour", "season", "usage",
            "productDisplayName"]
    frames = []
    for f in _parquets():
        df = pq.read_table(f, columns=cols).to_pandas()
        df["_file"] = f
        df["_row"] = range(len(df))
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def pick(meta, spec, used: set, seed: int):
    gender, art, color, usage, rx = spec
    m = meta[(meta.gender == gender) & (meta.articleType == art)]
    if color:
        m = m[m.baseColour == color]
    if usage:
        m = m[m.usage == usage]
    if rx:
        m2 = m[m.productDisplayName.fillna("").str.contains(rx, case=False, regex=True)]
        m = m2 if len(m2) else m
    m = m[~m.id.isin(used)]
    m = m[~m.productDisplayName.fillna("").str.contains(r"\bkids?\b|\bboys?\b|\bgirls?\b", case=False, regex=True)]
    if not len(m):
        return None
    return m.sample(1, random_state=seed).iloc[0]


def load_image(row) -> Image.Image:
    import pyarrow.parquet as pq
    t = pq.read_table(row["_file"], columns=["image"])
    rec = t.slice(int(row["_row"]), 1).to_pylist()[0]["image"]
    return Image.open(io.BytesIO(rec["bytes"])).convert("RGB")


def attributes_from_row(row) -> dict:
    cat, sub = ARTICLE_MAP[row["articleType"]]
    name = str(row["productDisplayName"] or "")
    low = name.lower()
    if row["articleType"] == "Trousers" and row["usage"] == "Casual":
        sub = "chinos" if row["gender"] == "Men" else "trousers"
    if row["articleType"] == "Tshirts" and "polo" in low:
        sub = "polo"
    pattern = "solid"
    for kw, p in (("stripe", "striped"), ("check", "checked"), ("plaid", "plaid"), ("floral", "floral"),
                  ("print", "printed"), ("graphic", "graphic")):
        if kw in low:
            pattern = p
            break
    formality = USAGE_FORMALITY.get(row["usage"], 2)
    if sub == "blazer":
        formality = max(formality, 4)
    color = str(row["baseColour"] or "").lower()
    color = COLOR_MAP.get(color, color) or "multi"
    season = str(row["season"] or "").lower()
    seasons = {"summer": ["spring", "summer"], "winter": ["fall", "winter"], "fall": ["fall", "winter"],
               "spring": ["spring", "summer"]}.get(season, [])
    gender = {"Men": "mens", "Women": "womens"}.get(row["gender"], "unisex")
    from app.gemini import FORMALITY_LABELS
    return {
        "category": cat, "subcategory": sub, "primary_color": color, "secondary_colors": [],
        "pattern": pattern, "fabric_guess": "denim" if "jean" in sub or "denim" in low else None,
        "formality": formality, "formality_label": FORMALITY_LABELS[formality], "seasons": seasons,
        "style_tags": [t for t in [str(row["usage"] or "").lower()] if t], "gender_presentation": gender,
        "brand": name.split(" ")[0] if name else None, "price": None, "currency": None,
        "description": name, "source": "seed:" + DATASET, "dataset_id": int(row["id"]),
    }


def make_assets(img: Image.Image, stem: str, category: str) -> dict:
    """Upscale the tiny (60x80) product image, run the same segformer cutout as Stage 1b."""
    from app.segment import segment_crop
    big = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
    d = config.MEDIA_DIR / "seed"
    for sub in ("crops", "cutouts", "white"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    rgba, white, info = segment_crop(big, category)
    paths = {"crop_path": d / "crops" / f"{stem}.jpg", "cutout_path": d / "cutouts" / f"{stem}.png",
             "white_path": d / "white" / f"{stem}.jpg"}
    big.save(paths["crop_path"], quality=92)
    rgba.save(paths["cutout_path"])
    white.save(paths["white_path"], quality=92)
    return {k: str(v) for k, v in paths.items()} | {"_seg": info.get("strategy")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe DB, FAISS index and media first")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.reset:
        log.info("Resetting DB, FAISS and media")
        db.reset_all()
        for p in (config.FAISS_PATH, config.FAISS_PATH.with_suffix(".ids.npy")):
            p.unlink(missing_ok=True)
        for sub in ("seed", "originals", "crops", "cutouts", "white", "context"):
            shutil.rmtree(config.MEDIA_DIR / sub, ignore_errors=True)
            (config.MEDIA_DIR / sub).mkdir(parents=True, exist_ok=True)
    db.init_db()

    meta = load_meta()
    used: set = set()
    for it in db.list_items():
        did = (it["attributes"] or {}).get("dataset_id")
        if did:
            used.add(did)

    created = 0
    plan = [(spec, "closet", None, None) for spec in CLOSET_SPEC] + \
           [(spec, "candidate", key, price) for key, spec, price in CANDIDATE_SPEC]
    # deterministic: pick rows in order; existing picks are recorded in a manifest
    manifest_path = config.DATA_DIR / "seed_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() and not args.reset else {}
    for n, (spec, status, key, price) in enumerate(plan):
        mkey = key or f"closet_{n}"
        row = None
        if mkey in manifest:
            r = meta[meta.id == manifest[mkey]]
            row = r.iloc[0] if len(r) else None
        if row is None:
            row = pick(meta, spec, used, args.seed + n)
        if row is None:
            log.warning("No dataset row for %s", spec)
            continue
        used.add(int(row["id"]))
        manifest[mkey] = int(row["id"])
        iid = f"seed_{row['id']}" if status == "closet" else f"seedcand_{row['id']}"
        if db.get_item(iid):
            continue
        attrs = attributes_from_row(row)
        img = load_image(row)
        assets = make_assets(img, iid, attrs["category"])
        attrs["segmentation"] = assets.pop("_seg")
        if price is not None:
            attrs["price"], attrs["currency"], attrs["price_source"] = price, "USD", "seed"
        if key:
            attrs["test_key"] = key
            img.resize((img.width * 4, img.height * 4), Image.LANCZOS).save(
                config.TEST_IMAGES_DIR / f"product_{key}.jpg", quality=92)
        db.insert_item(item_id=iid, status=status, category=attrs["category"], attributes=attrs,
                       label=attrs["description"], source="seed", **assets)
        created += 1
        log.info("seeded %-10s %-9s %s", status, attrs["category"], attrs["description"])
    manifest_path.write_text(json.dumps(manifest, indent=1))

    # embeddings: fashion-clip for everything, FAISS for closet, compat embeddings via scorer
    from app.scoring import ensure_compat_embeddings, scorer_kind
    from app.vectors import closet_index, ensure_fclip
    items = db.list_items(status="closet") + db.list_items(status="candidate")
    for it in items:
        ensure_fclip(it)
    closet_index.rebuild()
    ensure_compat_embeddings(items)
    n_closet = len(db.list_items(status="closet"))
    log.info("Done: %d new items; closet=%d; scorer=%s; FAISS=%s", created, n_closet, scorer_kind(), config.FAISS_PATH)


if __name__ == "__main__":
    main()
