#!/usr/bin/env python
"""Seed a coherent ~40-item demo closet from Polyvore product images (items alone on white).

Why Polyvore: the OutfitTransformer compat model was trained on Polyvore cut-outs; catalog photos of
people wearing clothes saturate its scores near 1.0. We take whole *real* Polyvore outfits (test split of
owj0421/polyvore-outfits, images from owj0421/polyvore) so the closet contains sets that genuinely go
together, plus 2 men's outfits (menswear is rare in Polyvore).

  python scripts/seed_demo_closet.py            # add anything missing (idempotent, ids 'seed_pv<id>')
  python scripts/seed_demo_closet.py --reset    # wipe DB, FAISS and media, then reseed
Also seeds held-out *candidate* items (status 'candidate', ids 'seedcand_pv<id>', attributes.test_key)
and writes their images to data/test_images/product_<key>.jpg.
Attributes come from Polyvore category + product-name keywords, with fashion-clip zero-shot filling gaps
(no Gemini needed). Picks are pinned in data/seed_manifest.json.
"""
from __future__ import annotations

import argparse
import collections
import glob
import io
import json
import logging
import random
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, db  # noqa: E402

log = logging.getLogger("seed")
CAT_MAP = {"tops": "top", "bottoms": "bottom", "outerwear": "outerwear", "all-body": "dress", "shoes": "shoes",
           "bags": "accessory"}
MEN_RX = re.compile(r"\bmen'?s?\b|\bmans\b|topman", re.I)
N_WOMEN_TB, N_WOMEN_DRESS, WOMEN_SHOE_SETS, WOMEN_OUTERWEAR = 8, 4, 6, 6
# hand-checked men's outfits from the test split (anchor item url_name -> slots to take). The second set's
# jacket photo shows a person wearing it, so it's skipped (worn photos saturate the compat model).
MEN_SET_ANCHORS = {"ck jeans mens omero high": ("tops", "bottoms", "outerwear", "shoes"),
                   "christian louboutin mens louis orlato": ("tops", "bottoms", "shoes")}

SUBCAT_KW = {
    "top": [(r"sweatshirt", "sweatshirt"), (r"\btee\b|t-shirt|\btshirt", "t-shirt"), (r"\btank\b|cami", "tank top"), (r"blouse", "blouse"),
            (r"polo", "polo"), (r"hoodie|hooded", "hoodie"), (r"sweatshirt", "sweatshirt"),
            (r"sweater|jumper|pullover|knit", "sweater"), (r"crop", "crop top"), (r"bodysuit", "bodysuit"),
            (r"button|oxford|dobby|shirt", "shirt"), (r"top", "top")],
    "bottom": [(r"short", "shorts"), (r"skirt", "skirt"), (r"jean|denim", "jeans"), (r"legging", "leggings"),
               (r"jogg|sweatpant|track", "joggers"), (r"chino", "chinos"), (r"culotte", "culottes"),
               (r"trouser|pant|slack", "trousers")],
    "outerwear": [(r"blazer", "blazer"), (r"trench", "trench coat"), (r"parka", "parka"), (r"puffer|down", "puffer jacket"),
                  (r"bomber", "bomber jacket"), (r"biker|moto|leather", "leather jacket"), (r"denim", "denim jacket"),
                  (r"cardigan", "cardigan"), (r"vest|gilet", "vest"), (r"coat", "coat"), (r"jacket", "jacket")],
    "dress": [(r"jumpsuit", "jumpsuit"), (r"romper|playsuit", "romper"), (r"maxi", "maxi dress"),
              (r"mini", "mini dress"), (r"midi", "midi dress"), (r"dress|gown", "dress")],
    "shoes": [(r"sneaker|trainer|vans|converse|sk8", "sneakers"), (r"boot", "boots"), (r"sandal", "sandals"),
              (r"loafer", "loafers"), (r"oxford|derby|brogue", "oxfords"), (r"mule", "mules"), (r"flat", "flats"),
              (r"heel|pump|stiletto", "heels")],
    "accessory": [(r"bag|tote|clutch|satchel|purse|backpack", "bag")],
}
COLORS = ["navy", "black", "white", "grey", "gray", "blue", "red", "burgundy", "pink", "green", "olive", "khaki",
          "beige", "brown", "camel", "tan", "cream", "ivory", "yellow", "orange", "purple", "silver", "gold", "nude",
          "indigo", "charcoal", "mint", "coral", "lilac", "blush"]
PATTERNS = [(r"stripe", "striped"), (r"plaid|tartan", "plaid"), (r"check|gingham", "checked"), (r"floral|flower", "floral"),
            (r"leopard|animal|snake|zebra", "animal print"), (r"polka|dot", "polka dot"), (r"camo", "camo"),
            (r"print|graphic", "printed")]
FABRICS = ["denim", "leather", "suede", "silk", "satin", "cotton", "wool", "cashmere", "linen", "lace", "chiffon",
           "velvet", "knit", "jersey", "tweed", "corduroy", "faux fur", "sequin"]
FORMALITY_KW = [(r"blazer|trouser|oxford|pump|heel|silk|satin|tailored|pencil", 4), (r"gown|tuxedo", 5),
                (r"jogg|sweat|hoodie|sneaker|legging|tank|tee|t-shirt|short", 1)]


# ------------------------------------------------------------------ data access
def snapshot(repo: str) -> str:
    from huggingface_hub import snapshot_download
    for base in (Path.home() / ".cache" / "huggingface" / "hub", Path(config.os.environ["HF_HOME"]) / "hub"):
        hits = glob.glob(str(base / f"datasets--{repo.replace('/', '--')}" / "snapshots" / "*"))
        if hits:
            return hits[0]
    return snapshot_download(repo, repo_type="dataset")


def load_meta():
    import pandas as pd
    for p in (config.MODELS_DIR / "polyvore_meta.pkl", Path("/tmp/polyvore_meta.pkl")):
        if p.exists():
            return pd.read_pickle(p).set_index("item_id")
    import pyarrow.parquet as pq
    frames = []
    for shard, f in enumerate(sorted(glob.glob(f"{snapshot('owj0421/polyvore')}/data/*.parquet"))):
        df = pq.read_table(f, columns=["item_id", "url_name", "title", "category"]).to_pandas()
        df["shard"] = shard
        frames.append(df)
    meta = pd.concat(frames, ignore_index=True)
    meta.to_pickle(config.MODELS_DIR / "polyvore_meta.pkl")
    return meta.set_index("item_id")


def load_images(meta, ids: set[str]) -> dict[str, bytes]:
    import pyarrow.parquet as pq
    files = sorted(glob.glob(f"{snapshot('owj0421/polyvore')}/data/*.parquet"))
    out = {}
    shards = collections.defaultdict(list)
    for i in ids:
        shards[int(meta.at[i, "shard"])].append(i)
    for shard, lst in sorted(shards.items()):
        t = pq.read_table(files[shard], columns=["item_id", "image"], filters=[("item_id", "in", lst)]).to_pylist()
        for r in t:
            img = r["image"]
            out[r["item_id"]] = img["bytes"] if isinstance(img, dict) else img
        del t
    return out


def load_sets():
    return json.load(open(f"{snapshot('owj0421/polyvore-outfits')}/nondisjoint_default/test.json"))


# ------------------------------------------------------------------ selection
def set_items(s, meta):
    out = collections.defaultdict(list)
    for it in s["items"]:
        iid = it["item_id"]
        if iid in meta.index:
            out[meta.at[iid, "category"]].append(iid)
    return out


def name_of(meta, iid) -> str:
    t, u = meta.at[iid, "title"], meta.at[iid, "url_name"]
    return str(t or u or "").strip()


def choose(meta, sets, seed: int) -> dict:
    rng = random.Random(seed)
    good = lambda iid: bool(str(meta.at[iid, "url_name"] or "").strip())  # noqa: E731
    shuffled = sets[:]
    rng.shuffle(shuffled)
    closet, cands, used_sets = [], {}, set()

    n_outer = [0]

    def take(s, cats):
        si = set_items(s, meta)
        for c in cats:
            if c == "outerwear":
                if n_outer[0] >= WOMEN_OUTERWEAR or not si.get(c):
                    continue
                n_outer[0] += 1
            for iid in si.get(c, [])[:1]:
                closet.append((iid, "womens"))
        used_sets.add(s["set_id"])

    tb = dr = shoe_sets = 0
    for s in shuffled:
        si = set_items(s, meta)
        if any(MEN_RX.search(str(meta.at[i, "url_name"])) for v in si.values() for i in v):
            continue
        apparel = [i for c in ("tops", "bottoms", "outerwear", "all-body", "shoes") for i in si.get(c, [])]
        if not apparel or not all(good(i) for i in apparel):
            continue
        n = {c: len(si.get(c, [])) for c in ("tops", "bottoms", "outerwear", "all-body", "shoes")}
        if tb < N_WOMEN_TB and n["tops"] == 1 and n["bottoms"] == 1 and n["all-body"] == 0 and n["shoes"] == 1 \
                and n["outerwear"] <= 1:
            cats = ["tops", "bottoms", "outerwear"] + (["shoes"] if shoe_sets < WOMEN_SHOE_SETS else [])
            shoe_sets += "shoes" in cats
            take(s, cats); tb += 1
        elif dr < N_WOMEN_DRESS and n["all-body"] == 1 and n["tops"] == 0 and n["bottoms"] == 0 and n["shoes"] == 1 \
                and n["outerwear"] <= 1:
            cats = ["all-body", "outerwear"] + (["shoes"] if shoe_sets < WOMEN_SHOE_SETS else [])
            shoe_sets += "shoes" in cats
            take(s, cats); dr += 1
        if tb >= N_WOMEN_TB and dr >= N_WOMEN_DRESS:
            break
    # men's outfits
    for anchor, slots in MEN_SET_ANCHORS.items():
        for s in sets:
            si = set_items(s, meta)
            if any(str(meta.at[i, "url_name"]) == anchor for v in si.values() for i in v):
                for c in slots:
                    for iid in si.get(c, [])[: (2 if c == "tops" else 1)]:
                        closet.append((iid, "mens"))
                used_sets.add(s["set_id"])
                break
    # held-out candidates from unused women's sets (one per slot) + one men's top
    wanted = {"top_womens": "tops", "bottom_womens": "bottoms", "outerwear_womens": "outerwear",
              "dress_womens": "all-body", "shoes_womens": "shoes", "bag_womens": "bags"}
    for s in shuffled:
        if s["set_id"] in used_sets:
            continue
        si = set_items(s, meta)
        if any(MEN_RX.search(str(meta.at[i, "url_name"])) for v in si.values() for i in v):
            continue
        for key, c in list(wanted.items()):
            if si.get(c) and good(si[c][0]) and len(str(meta.at[si[c][0], "url_name"]).split()) >= 3:
                cands[key] = (si[c][0], "womens")
                del wanted[key]
                used_sets.add(s["set_id"])
                break
        if not wanted:
            break
    closet_ids = {i for i, _ in closet}
    mens_opts = []
    for s in sets:  # men's top options not in the closet (first one without a person in the photo wins)
        if s["set_id"] in used_sets:
            continue
        si = set_items(s, meta)
        mens_opts += [i for i in si.get("tops", []) if MEN_RX.search(str(meta.at[i, "url_name"])) and i not in closet_ids
                      and re.search(r"shirt|tee|polo|sweater|henley", str(meta.at[i, "url_name"]))]
        if len(mens_opts) >= 8:
            break
    return {"closet": closet, "candidates": cands, "mens_top_options": mens_opts,
            "used_sets": sorted(used_sets)}


def has_person(img: Image.Image) -> bool:
    """segformer face/arm/leg pixels -> the product photo shows a person (bad for the compat model)."""
    from app.models_runtime import get_segformer
    import torch
    proc, model, lock = get_segformer()
    with lock, torch.inference_mode():
        seg = model(**proc(images=img.convert("RGB"), return_tensors="pt")).logits.argmax(1)[0].numpy()
    return float(np.isin(seg, [11, 12, 13, 14, 15]).mean()) > 0.01


def near_dup_pool(meta, sets, exclude: set[str], seed: int, category="tops", n=1200) -> list[str]:
    rng = random.Random(seed + 1)
    pool = [i for s in sets for i in set_items(s, meta).get(category, []) if i not in exclude
            and not MEN_RX.search(str(meta.at[i, "url_name"]))]
    return rng.sample(pool, min(n, len(pool)))


# ------------------------------------------------------------------ attributes / assets
def _kw(rx_list, text, default=None):
    for rx, val in rx_list:
        if re.search(rx, text):
            return val
    return default


def attributes_for(iid: str, meta, gender: str, white: Image.Image) -> dict:
    from app.fallback import SUBCATS, zero_shot_attributes
    from app.gemini import FORMALITY_LABELS
    cat = CAT_MAP[meta.at[iid, "category"]]
    name = name_of(meta, iid)
    text = f"{name} {meta.at[iid, 'url_name'] or ''}".lower()
    zs = zero_shot_attributes(white, restrict_category=cat if cat != "accessory" else None)
    sub = _kw(SUBCAT_KW.get(cat, []), text) or zs["subcategory"]
    color = next((c for c in COLORS if re.search(rf"\b{c}\b", text)), None) or zs["primary_color"]
    color = {"gray": "grey"}.get(color, color)
    pattern = _kw(PATTERNS, text) or ("solid" if zs["pattern"] in ("solid", "denim wash") else zs["pattern"])
    fabric = next((f for f in FABRICS if f in text), None)
    if fabric is None and sub in ("jeans", "denim jacket"):
        fabric = "denim"
    formality = _kw(FORMALITY_KW, text) or SUBCATS.get(sub, (None, 2, None))[1]
    seasons = SUBCATS.get(sub, (None, None, ["spring", "summer", "fall", "winter"]))[2]
    return {
        "category": cat, "subcategory": sub, "primary_color": color, "secondary_colors": [], "pattern": pattern,
        "fabric_guess": fabric, "formality": formality, "formality_label": FORMALITY_LABELS[formality],
        "seasons": seasons, "style_tags": [], "gender_presentation": gender, "brand": None, "price": None,
        "currency": None, "description": name[:1].upper() + name[1:], "source": "seed:polyvore",
        "polyvore_id": iid, "polyvore_category": meta.at[iid, "category"],
    }


def make_assets(img: Image.Image, stem: str) -> dict:
    from app.segment import white_bg_cutout
    img = img.convert("RGB")
    rgba, white = white_bg_cutout(img)
    d = config.MEDIA_DIR / "seed"
    paths = {"crop_path": d / "crops" / f"{stem}.jpg", "cutout_path": d / "cutouts" / f"{stem}.png",
             "white_path": d / "white" / f"{stem}.jpg"}
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    img.save(paths["crop_path"], quality=92)
    rgba.save(paths["cutout_path"])
    white.save(paths["white_path"], quality=92)
    return {k: str(v) for k, v in paths.items()}


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe DB, FAISS index and media first")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--reselect", action="store_true", help="ignore data/seed_manifest.json and pick again")
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
    sets = load_sets()
    manifest_path = config.DATA_DIR / "seed_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if manifest.get("source") != "polyvore" or args.reselect:
        manifest = {"source": "polyvore", **choose(meta, sets, args.seed)}
        manifest["candidates"] = {k: list(v) for k, v in manifest["candidates"].items()}
    closet = [tuple(x) for x in manifest["closet"]]
    cands = {k: tuple(v) for k, v in manifest["candidates"].items()}

    if "top_mens" not in cands and manifest.get("mens_top_options"):
        opts = manifest["mens_top_options"]
        oi = load_images(meta, set(opts))
        for o in opts:
            if o in oi and not has_person(Image.open(io.BytesIO(oi[o]))):
                cands["top_mens"] = (o, "mens")
                manifest["candidates"]["top_mens"] = [o, "mens"]
                break
    need_pool = "near_dup_top" not in cands
    pool = near_dup_pool(meta, sets, {i for i, _ in closet} | {v[0] for v in cands.values()}, args.seed) \
        if need_pool else []
    ids = {i for i, _ in closet} | {v[0] for v in cands.values()} | set(pool)
    log.info("Loading %d Polyvore images", len(ids))
    imgs = load_images(meta, ids)

    if need_pool:  # the pool top most similar (fashion-clip) to some closet top -> near-duplicate demo
        from app.vectors import embed_images
        ctops = [i for i, g in closet if g == "womens" and meta.at[i, "category"] == "tops"]
        pids = [i for i in pool if i in imgs]
        P = np.concatenate([embed_images([Image.open(io.BytesIO(imgs[i])) for i in pids[k:k + 64]])
                            for k in range(0, len(pids), 64)])
        C = embed_images([Image.open(io.BytesIO(imgs[i])) for i in ctops])
        S = P @ C.T
        best = int(np.argmax(S.max(1)))
        cands["near_dup_top"] = (pids[best], "womens")
        log.info("near-duplicate candidate %s (%s) ~ closet %s (%s) cos=%.3f", pids[best], name_of(meta, pids[best]),
                 ctops[int(np.argmax(S[best]))], name_of(meta, ctops[int(np.argmax(S[best]))]), S.max())
        manifest["candidates"]["near_dup_top"] = list(cands["near_dup_top"])
    manifest_path.write_text(json.dumps(manifest, indent=1))

    for old in config.TEST_IMAGES_DIR.glob("product_*.jpg"):
        old.unlink()
    created = 0
    plan = [(iid, g, "closet", None) for iid, g in closet] + [(v[0], v[1], "candidate", k) for k, v in cands.items()]
    for iid, gender, status, key in plan:
        item_id = ("seed_pv" if status == "closet" else "seedcand_pv") + iid
        if iid not in imgs:
            log.warning("no image for %s", iid)
            continue
        img = Image.open(io.BytesIO(imgs[iid])).convert("RGB")
        if key:
            img.save(config.TEST_IMAGES_DIR / f"product_{key}.jpg", quality=92)
        if db.get_item(item_id):
            continue
        assets = make_assets(img, item_id)
        attrs = attributes_for(iid, meta, gender, Image.open(assets["white_path"]))
        if key:
            attrs["test_key"] = key
            attrs["price"], attrs["currency"], attrs["price_source"] = \
                {"top": 28.0, "bottom": 45.0, "outerwear": 80.0, "dress": 55.0, "shoes": 65.0}.get(attrs["category"], 30.0), \
                "USD", "seed"
        db.insert_item(item_id=item_id, status=status, category=attrs["category"], attributes=attrs,
                       label=attrs["description"], source="seed", **assets)
        created += 1
        log.info("seeded %-9s %-9s %-6s %s", status, attrs["category"], gender, attrs["description"][:60])

    from app.scoring import ensure_compat_embeddings, scorer_kind
    from app.vectors import closet_index, ensure_fclip
    items = db.list_items(status="closet") + db.list_items(status="candidate")
    for it in items:
        ensure_fclip(it)
    closet_index.rebuild()
    ensure_compat_embeddings(items)
    by_cat = collections.Counter(i["category"] for i in db.list_items(status="closet"))
    log.info("Done: %d new items; closet=%s; scorer=%s", created, dict(by_cat), scorer_kind())


if __name__ == "__main__":
    main()
