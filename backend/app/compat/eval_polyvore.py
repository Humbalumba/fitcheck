"""Calibration check on the real Polyvore Outfits (nondisjoint) compatibility test split.

Run from backend/:  python -m app.compat.eval_polyvore [--n 200]
Needs ~2.2GB of HF downloads (owj0421/polyvore item images + owj0421/polyvore-outfits splits).
Reports: AUC + score distribution on full outfits, and on 2-/3-item sub-outfits that mimic
FitCheck's usage (top+bottom from the same real outfit vs. random/duplicate-category pairs).
"""
from __future__ import annotations

import io
import json
import random
import sys
import time
from collections import defaultdict

import numpy as np
from PIL import Image

from .scorer import CompatibilityScorer
from .test_compat import auc


def main(n: int = 200):
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    rng = random.Random(0)
    comp = json.load(open(hf_hub_download("owj0421/polyvore-outfits", "nondisjoint_compatibility/test.json", repo_type="dataset")))
    sets = json.load(open(hf_hub_download("owj0421/polyvore-outfits", "nondisjoint_default/test.json", repo_type="dataset")))
    conv = {f"{s['set_id']}_{it['index']}": it["item_id"] for s in sets for it in s["items"]}
    pos = rng.sample([c for c in comp if c["label"] == "1"], n)
    neg = rng.sample([c for c in comp if c["label"] == "0"], n)
    outfit_ids = [[conv[i] for i in c["items"] if i in conv] for c in pos + neg]
    extra_sets = rng.sample(sets, 3 * n)  # for sub-outfit experiments
    needed = set(sum(outfit_ids, [])) | {it["item_id"] for s in extra_sets for it in s["items"]}
    print(f"{len(needed)} items needed")

    meta = {}
    imgs = {}
    for i in range(6):
        p = hf_hub_download("owj0421/polyvore", f"data/data-0000{i}-of-00005.parquet", repo_type="dataset")
        t = pq.read_table(p, filters=[("item_id", "in", list(needed))]).to_pylist()
        for r in t:
            meta[r["item_id"]] = (r["category"], r["url_name"] or r["title"] or "")
            imgs[r["item_id"]] = r["image"]["bytes"] if isinstance(r["image"], dict) else r["image"]
        del t
    print(f"found {len(meta)} items; categories: {sorted(set(c for c, _ in meta.values()))}")

    scorer = CompatibilityScorer()
    ids = sorted(meta)
    E = {}
    t0 = time.time()
    for s in range(0, len(ids), 32):
        b = ids[s:s + 32]
        e = scorer.embed_items([Image.open(io.BytesIO(imgs[i])) for i in b], [meta[i][1] for i in b])
        E.update(zip(b, e))
    print(f"embedded {len(ids)} items in {time.time() - t0:.0f}s")
    print("example texts:", [meta[i][1] for i in ids[:5]])

    def report(name, P, N):
        P, N = np.array(P), np.array(N)
        print(f"\n== {name}: n+={len(P)} n-={len(N)} AUC={auc(P, N):.3f} "
              f"mean+={P.mean():.3f} mean-={N.mean():.3f}")
        for th in (0.3, 0.5, 0.7, 0.8, 0.9, 0.95):
            print(f"   th {th:.2f}: pos pass {np.mean(P >= th):.2f}  neg pass {np.mean(N >= th):.2f}")
        ths = np.linspace(0.01, 0.99, 99)
        acc = [(np.mean(P >= th) + np.mean(N < th)) / 2 for th in ths]
        print(f"   best balanced-acc threshold {ths[int(np.argmax(acc))]:.2f} -> {max(acc):.3f}")

    sc = scorer.score_outfits([[E[i] for i in o if i in E] for o in outfit_ids])
    report("Polyvore test full outfits", sc[:n], sc[n:])

    # sub-outfits: top+bottom (same real outfit) vs top+bottom (random) vs top+top, bottom+bottom
    by_cat = defaultdict(list)
    real_pairs, real_triples = [], []
    for s in extra_sets:
        cats = {}
        for it in s["items"]:
            if it["item_id"] in E:
                cats.setdefault(meta[it["item_id"]][0], it["item_id"])
                by_cat[meta[it["item_id"]][0]].append(it["item_id"])
        if "tops" in cats and "bottoms" in cats:
            real_pairs.append([cats["tops"], cats["bottoms"]])
            if "shoes" in cats:
                real_triples.append([cats["tops"], cats["bottoms"], cats["shoes"]])
    r = lambda c: E[rng.choice(by_cat[c])]
    m = len(real_pairs)
    s_real = scorer.score_outfits([[E[a], E[b]] for a, b in real_pairs])
    s_rand = scorer.score_outfits([[r("tops"), r("bottoms")] for _ in range(m)])
    s_tt = scorer.score_outfits([[r("tops"), r("tops")] for _ in range(m)])
    s_bb = scorer.score_outfits([[r("bottoms"), r("bottoms")] for _ in range(m)])
    s_ss = scorer.score_outfits([[r("shoes"), r("shoes")] for _ in range(m)])
    report("top+bottom: real pair vs random pair", s_real, s_rand)
    report("top+bottom real vs top+top random", s_real, s_tt)
    report("top+bottom real vs bottom+bottom random", s_real, s_bb)
    report("top+bottom real vs shoes+shoes random", s_real, s_ss)
    k = len(real_triples)
    s3 = scorer.score_outfits([[E[i] for i in t] for t in real_triples])
    s3r = scorer.score_outfits([[r("tops"), r("bottoms"), r("shoes")] for _ in range(k)])
    s3d = scorer.score_outfits([[r("tops"), r("tops"), r("bottoms")] for _ in range(k)])
    report("top+bottom+shoes real vs random", s3, s3r)
    report("top+bottom+shoes real vs top+top+bottom random", s3, s3d)


def mens_domain_test(k: int = 8, m: int = 40):
    """Polyvore-domain images, men's items found via url_name ("mens ..."), random combos per slot."""
    import pandas as pd
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    rng = random.Random(0)
    shards = [hf_hub_download("owj0421/polyvore", f"data/data-0000{i}-of-00005.parquet", repo_type="dataset")
              for i in range(6)]
    df = pd.concat([pq.read_table(p, columns=["item_id", "url_name", "category"]).to_pandas() for p in shards])
    u, c = df.url_name.fillna(""), df.category
    men = u.str.contains(r"\bmens?\b", regex=True) & ~u.str.contains("women")
    slots = {
        "M_shirt": men & (c == "tops") & u.str.contains("shirt") & ~u.str.contains("tee|t-shirt|sweat"),
        "M_tee": men & (c == "tops") & u.str.contains(r"tee|t-shirt", regex=True),
        "M_pants": men & (c == "bottoms") & u.str.contains(r"jean|trouser|pant|chino", regex=True),
        "M_shoes": men & (c == "shoes"),
        "W_top": (c == "tops") & u.str.contains("blouse"),
        "W_skirt": (c == "bottoms") & u.str.contains("mini skirt"),
        "W_pumps": (c == "shoes") & u.str.contains("pumps"),
    }
    pick = {s: df[msk].sample(n=min(k, int(msk.sum())), random_state=0) for s, msk in slots.items()}
    need = set(pd.concat(pick.values()).item_id)
    imgs = {}
    for p in shards:
        for r in pq.read_table(p, columns=["item_id", "image"], filters=[("item_id", "in", list(need))]).to_pylist():
            imgs[r["item_id"]] = r["image"]["bytes"] if isinstance(r["image"], dict) else r["image"]
    scorer = CompatibilityScorer()
    E = {}
    for s, d in pick.items():
        print(f"{s:8s} ({len(d)}): {list(d.url_name)[:4]}")
        E[s] = scorer.embed_items([Image.open(io.BytesIO(imgs[i])) for i in d.item_id], list(d.url_name))
    r = lambda s: E[s][rng.randrange(len(E[s]))]
    groups = {
        "GOOD M shirt+pants": ["M_shirt", "M_pants"],
        "GOOD M tee+pants": ["M_tee", "M_pants"],
        "GOOD M shirt+pants+shoes": ["M_shirt", "M_pants", "M_shoes"],
        "GOOD M tee+pants+shoes": ["M_tee", "M_pants", "M_shoes"],
        "GOOD W blouse+skirt": ["W_top", "W_skirt"],
        "GOOD W blouse+skirt+pumps": ["W_top", "W_skirt", "W_pumps"],
        "BAD  M shirt+shirt": ["M_shirt", "M_shirt"],
        "BAD  M shirt+tee": ["M_shirt", "M_tee"],
        "BAD  M pants+pants": ["M_pants", "M_pants"],
        "BAD  M shoes+shoes": ["M_shoes", "M_shoes"],
        "BAD  M shirt+shirt+pants": ["M_shirt", "M_shirt", "M_pants"],
        "BAD  W skirt+M pants": ["W_skirt", "M_pants"],
        "BAD  W blouse+blouse": ["W_top", "W_top"],
    }
    print(f"\n{'group':28s} {'mean':>6s} {'p10':>6s} {'p50':>6s} {'p90':>6s}")
    G, B = {2: [], 3: []}, {2: [], 3: []}
    for g, sl in groups.items():
        sc = np.array(scorer.score_outfits([[r(s) for s in sl] for _ in range(m)]))
        (G if g.startswith("GOOD") else B)[len(sl)].extend(sc)
        print(f"{g:28s} {sc.mean():6.3f} {np.percentile(sc, 10):6.3f} {np.median(sc):6.3f} {np.percentile(sc, 90):6.3f}")
    for L in (2, 3):
        print(f"{L}-item AUC good vs bad: {auc(G[L], B[L]):.3f}")


if __name__ == "__main__":
    if "--mens" in sys.argv:
        mens_domain_test()
    else:
        n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 200
        main(n)
