"""Sanity test for the OutfitTransformer compatibility scorer.

Run from backend/:   python -m app.compat.test_compat  [--quick]

Pulls product images from the HF dataset `ashraq/fashion-product-images-small` (one ~136MB
parquet shard, cached by huggingface_hub), embeds a handful of men's and women's items, and
compares scores of sensible vs nonsensical outfits. Also prints speed numbers.
"""
from __future__ import annotations

import io
import itertools
import os
import random
import sys
import time
from pathlib import Path

import numpy as np

from .scorer import CompatibilityScorer

IMG_DIR = Path(os.environ.get("FITCHECK_COMPAT_TEST_DIR", "/tmp/fitcheck_compat_test"))
SHARD = "data/train-00000-of-00002-6cff4c59f91661c3.parquet"


def load_catalog():
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download
    p = hf_hub_download("ashraq/fashion-product-images-small", SHARD, repo_type="dataset")
    df = pq.read_table(p).to_pandas()
    df["text"] = (df.baseColour.fillna("").str.lower() + " " + df.articleType.str.lower() + ", "
                  + df.gender.str.lower() + ", " + df.usage.fillna("").str.lower())
    return df


def item_path(row) -> str:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    f = IMG_DIR / f"{row.id}.jpg"
    if not f.exists():
        f.write_bytes(row.image["bytes"])
    return str(f)


_PROMPTS = ["a photo of a model", "a flat lay photo of a garment"]
_APPAREL = {"Shirts", "Tshirts", "Trousers", "Jeans", "Shorts", "Track Pants", "Tops", "Skirts", "Dresses",
            "Kurtas", "Jackets", "Sweaters", "Sweatshirts", "Blazers", "Leggings", "Capris"}
_PRODUCT_ONLY = {"scorer": None}


def product_only_mask(scorer, rows) -> np.ndarray:
    """Test-data hygiene only: many catalog photos show the garment worn by a model (so the
    photo already contains a whole outfit). Keep flat/product-only shots via fashion-clip zero-shot."""
    import torch
    with torch.inference_mode():
        pil = [scorer._load_rgb(item_path(r)) for r in rows]
        pix = scorer.processor(images=pil, return_tensors="pt")["pixel_values"]
        im = torch.nn.functional.normalize(scorer.vision(pixel_values=pix).image_embeds, dim=-1)
        tok = scorer.tokenizer(text=_PROMPTS, padding=True, return_tensors="pt")
        tx = torch.nn.functional.normalize(scorer.text(**tok).text_embeds, dim=-1)
        return (im @ tx.T).argmax(-1).numpy() == 1


def pick(df, n=1, seed=0, **kw):
    q = df
    for k, v in kw.items():
        q = q[q[k].isin(v if isinstance(v, (list, tuple)) else [v])]
    assert len(q), f"no item for {kw}"
    q = q.sample(n=min(6 * n + 12, len(q)), random_state=seed)
    sc = _PRODUCT_ONLY["scorer"]
    if sc is not None and set(q.articleType) <= _APPAREL:
        keep = q[product_only_mask(sc, list(q.itertuples()))]
        q = keep if len(keep) else q
    return q.head(n)


def auc(pos, neg):
    pos, neg = np.asarray(pos), np.asarray(neg)
    return float(((pos[:, None] > neg[None, :]).mean() + 0.5 * (pos[:, None] == neg[None, :]).mean()))


def main(quick: bool = False):
    t0 = time.time()
    scorer = CompatibilityScorer()
    print(f"model load: {time.time() - t0:.1f}s")
    df = load_catalog()
    if "--allow-worn" not in sys.argv:
        _PRODUCT_ONLY["scorer"] = scorer

    # ---------------------------------------------------------------- hand-picked items
    spec = {
        "M white formal shirt": dict(gender="Men", articleType="Shirts", usage="Formal", baseColour="White"),
        "M blue formal shirt": dict(gender="Men", articleType="Shirts", usage="Formal", baseColour="Blue"),
        "M black formal trousers": dict(gender="Men", articleType="Trousers", usage="Formal", baseColour=["Black", "Grey"]),
        "M black formal shoes": dict(gender="Men", articleType="Formal Shoes", baseColour=["Black", "Brown"]),
        "M tie": dict(gender="Men", articleType="Ties"),
        "M casual tshirt": dict(gender="Men", articleType="Tshirts", usage="Casual", baseColour=["Grey", "Black", "White"]),
        "M blue jeans": dict(gender="Men", articleType="Jeans", baseColour="Blue"),
        "M casual shoes": dict(gender="Men", articleType="Casual Shoes", baseColour=["White", "Black", "Brown"]),
        "M track pants": dict(gender="Men", articleType="Track Pants"),
        "M flip flops": dict(gender="Men", articleType="Flip Flops"),
        "M sports shoes": dict(gender="Men", articleType="Sports Shoes"),
        "M sports tshirt": dict(gender="Men", articleType="Tshirts", usage="Sports"),
        "M shorts": dict(gender="Men", articleType="Shorts"),
        "W top": dict(gender="Women", articleType="Tops", usage="Casual", baseColour=["White", "Black", "Pink"]),
        "W jeans": dict(gender="Women", articleType="Jeans"),
        "W skirt": dict(gender="Women", articleType="Skirts"),
        "W heels": dict(gender="Women", articleType="Heels", baseColour=["Black", "Beige", "Red"]),
        "W handbag": dict(gender="Women", articleType="Handbags"),
        "W dress": dict(gender="Women", articleType="Dresses"),
        "W kurta": dict(gender="Women", articleType="Kurtas"),
    }
    rows = {name: next(pick(df, **kw).itertuples()) for name, kw in spec.items()}
    names = list(rows)
    paths = [item_path(rows[n]) for n in names]
    texts = [rows[n].text for n in names]
    t = time.time()
    E = scorer.embed_items(paths, texts)
    dt = time.time() - t
    print(f"embedded {len(names)} items in {dt:.2f}s ({dt / len(names) * 1000:.0f} ms/item, batched)")
    t = time.time()
    _ = scorer.embed_item(paths[0], texts[0])
    print(f"single embed_item: {(time.time() - t) * 1000:.0f} ms")
    emb = dict(zip(names, E))
    E_notext = scorer.embed_items(paths, [""] * len(paths))
    emb_nt = dict(zip(names, E_notext))
    print("\nitems:")
    for n in names:
        print(f"  {n:26s} -> id {rows[n].id}: {rows[n].productDisplayName!r}  text={rows[n].text!r}")

    outfits = [
        ("GOOD", ["M white formal shirt", "M black formal trousers"]),
        ("GOOD", ["M blue formal shirt", "M black formal trousers", "M black formal shoes"]),
        ("GOOD", ["M white formal shirt", "M black formal trousers", "M black formal shoes", "M tie"]),
        ("GOOD", ["M casual tshirt", "M blue jeans"]),
        ("GOOD", ["M casual tshirt", "M blue jeans", "M casual shoes"]),
        ("GOOD", ["M sports tshirt", "M track pants", "M sports shoes"]),
        ("GOOD", ["M casual tshirt", "M shorts", "M flip flops"]),
        ("GOOD", ["W top", "W jeans"]),
        ("GOOD", ["W top", "W jeans", "W heels", "W handbag"]),
        ("GOOD", ["W top", "W skirt", "W heels"]),
        ("GOOD", ["W dress", "W heels", "W handbag"]),
        ("BAD", ["M white formal shirt", "M blue formal shirt"]),
        ("BAD", ["M casual tshirt", "M white formal shirt"]),
        ("BAD", ["M black formal trousers", "M blue jeans"]),
        ("BAD", ["M black formal trousers", "M track pants", "M shorts"]),
        ("BAD", ["M black formal shoes", "M casual shoes", "M sports shoes"]),
        ("BAD", ["M white formal shirt", "M black formal trousers", "M flip flops"]),
        ("BAD", ["M white formal shirt", "M track pants", "M flip flops"]),
        ("BAD", ["M tie", "M sports tshirt", "M shorts"]),
        ("BAD", ["W dress", "W jeans"]),
        ("BAD", ["W top", "W top"]),
        ("BAD", ["W jeans", "W skirt"]),
        ("BAD", ["W kurta", "M track pants", "M sports shoes"]),
    ]
    s = scorer.score_outfits([[emb[i] for i in o] for _, o in outfits])
    s_nt = scorer.score_outfits([[emb_nt[i] for i in o] for _, o in outfits])
    print(f"\n{'label':5s} {'score':>6s} {'no-text':>7s}  outfit")
    for (lab, o), a, b in zip(outfits, s, s_nt):
        print(f"{lab:5s} {a:6.3f} {b:7.3f}  {' + '.join(o)}")
    good = [a for (l, _), a in zip(outfits, s) if l == "GOOD"]
    bad = [a for (l, _), a in zip(outfits, s) if l == "BAD"]
    print(f"hand-picked: mean GOOD={np.mean(good):.3f} mean BAD={np.mean(bad):.3f} AUC={auc(good, bad):.3f}")

    # ---------------------------------------------------------------- random-sample stats
    n = 10 if quick else 40
    pools = {
        "M_ftop": dict(gender="Men", articleType="Shirts", usage="Formal"),
        "M_fbot": dict(gender="Men", articleType="Trousers", usage="Formal"),
        "M_fshoe": dict(gender="Men", articleType="Formal Shoes"),
        "M_ctop": dict(gender="Men", articleType="Tshirts", usage="Casual"),
        "M_cbot": dict(gender="Men", articleType=["Jeans", "Shorts"]),
        "M_cshoe": dict(gender="Men", articleType="Casual Shoes"),
        "M_track": dict(gender="Men", articleType="Track Pants"),
        "M_flip": dict(gender="Men", articleType="Flip Flops"),
        "W_top": dict(gender="Women", articleType="Tops"),
        "W_bot": dict(gender="Women", articleType=["Jeans", "Skirts", "Shorts"]),
        "W_shoe": dict(gender="Women", articleType=["Heels", "Flats"]),
        "W_dress": dict(gender="Women", articleType="Dresses"),
    }
    P = {}
    for k, kw in pools.items():
        sub = pick(df, n=n, seed=1, **kw)
        P[k] = scorer.embed_items([item_path(r) for r in sub.itertuples()], list(sub.text))
    rng = random.Random(0)
    r = lambda k: P[k][rng.randrange(len(P[k]))]
    groups = {
        "M formal top+bottom": lambda: [r("M_ftop"), r("M_fbot")],
        "M formal top+bottom+shoes": lambda: [r("M_ftop"), r("M_fbot"), r("M_fshoe")],
        "M casual top+bottom": lambda: [r("M_ctop"), r("M_cbot")],
        "M casual top+bottom+shoes": lambda: [r("M_ctop"), r("M_cbot"), r("M_cshoe")],
        "W top+bottom": lambda: [r("W_top"), r("W_bot")],
        "W top+bottom+shoes": lambda: [r("W_top"), r("W_bot"), r("W_shoe")],
        "BAD M two formal shirts": lambda: [r("M_ftop"), r("M_ftop")],
        "BAD M two tops (tee+shirt)": lambda: [r("M_ctop"), r("M_ftop")],
        "BAD M two bottoms": lambda: [r("M_fbot"), r("M_cbot")],
        "BAD M formal shirt+track+flipflop": lambda: [r("M_ftop"), r("M_track"), r("M_flip")],
        "BAD M two shoes": lambda: [r("M_fshoe"), r("M_cshoe")],
        "BAD W two tops": lambda: [r("W_top"), r("W_top")],
        "BAD W dress+bottom": lambda: [r("W_dress"), r("W_bot")],
    }
    print(f"\nrandom outfits ({n} items per pool, 60 outfits per group):")
    print(f"{'group':36s} {'mean':>6s} {'p10':>6s} {'p50':>6s} {'p90':>6s} {'>=0.5':>6s}")
    allgood, allbad = [], []
    for g, fn in groups.items():
        sc = np.array(scorer.score_outfits([fn() for _ in range(60)]))
        (allbad if g.startswith("BAD") else allgood).extend(sc.tolist())
        print(f"{g:36s} {sc.mean():6.3f} {np.percentile(sc, 10):6.3f} {np.median(sc):6.3f} "
              f"{np.percentile(sc, 90):6.3f} {(sc >= 0.5).mean():6.2f}")
    print(f"random: AUC(good vs bad)={auc(allgood, allbad):.3f}")
    for th in (0.3, 0.4, 0.5, 0.6, 0.7):
        tpr = np.mean(np.array(allgood) >= th)
        fpr = np.mean(np.array(allbad) >= th)
        print(f"  threshold {th:.1f}: good pass {tpr:.2f}, bad pass {fpr:.2f}")

    # ---------------------------------------------------------------- speed
    items = np.concatenate(list(P.values()))
    for L, N in [(2, 30), (2, 100), (3, 100), (4, 100), (3, 1000)]:
        batch = [[items[rng.randrange(len(items))] for _ in range(L)] for _ in range(N)]
        scorer.score_outfits(batch[:4])  # warm
        t = time.time()
        scorer.score_outfits(batch)
        print(f"speed: {N} outfits x {L} items: {(time.time() - t) * 1000:.0f} ms")


if __name__ == "__main__":
    main(quick="--quick" in sys.argv)
