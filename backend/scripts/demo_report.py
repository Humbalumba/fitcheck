#!/usr/bin/env python
"""Evaluate every seeded candidate and print scores (raw + calibrated) — used to sanity-check calibration."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
from app import db  # noqa: E402
from app.evaluate import evaluate  # noqa: E402

for c in sorted(db.list_items(status="candidate"), key=lambda c: c["attributes"].get("test_key", "")):
    t = time.time()
    r = evaluate(c["id"])
    a = c["attributes"]
    print(f"\n== {a.get('test_key')}: {a['description'][:50]} (${a.get('price')})  [{time.time()-t:.1f}s]")
    print(f"   redundancy={r['redundancy']['level']} top={r['redundancy']['top_similarity']} "
          f"-> {[m['item']['attributes']['description'][:30] for m in r['redundancy']['matches'][:1]]}")
    print(f"   N={r['total_new_outfits']} {r['outfit_count_by_template']}  value={r['value'].get('value_score')} "
          f"cpo={r['value'].get('cost_per_outfit')}  verdict={r['verdict']['decision']}")
    for reason in r["verdict"]["reasons"]:
        print("     -", reason)
    for o in r["outfits"][:3]:
        print(f"     {o['template']:28s} cal={o['score']:.2f} raw={o['raw_score']} :: "
              + " | ".join(i['attributes']['description'][:22] for i in o['items']))

# ---- closet-level calibration stats: all gender-compatible top x bottom pairs, with/without best shoe
from app.evaluate import gender_ok  # noqa: E402
from app.scoring import calibrate, ensure_compat_embeddings, raw_cutoff, score_outfits_cached  # noqa: E402

closet = db.list_items(status="closet")
emb = ensure_compat_embeddings(closet)
by = {}
for it in closet:
    by.setdefault(it["category"], []).append(it)
pairs = [[t, b] for t in by["top"] for b in by["bottom"] if gender_ok(t, b)]
raw2 = np.array(score_outfits_cached([[t["id"], b["id"]] for t, b in pairs], emb))
shoes = by.get("shoes", [])
raw3_all = [[score_outfits_cached([[t["id"], b["id"], s["id"]]], emb)[0] for s in shoes if gender_ok(s, t)]
            for t, b in pairs]
raw3_best = np.array([max(x) for x in raw3_all])
raw3_mean = np.array([np.mean(x) for x in raw3_all])
print(f"\nCloset top x bottom pairs: {len(pairs)}")
for lbl, arr, n in (("2-item raw", raw2, 2), ("3-item raw (mean over shoes)", raw3_mean, 3),
                    ("3-item raw (best shoe)", raw3_best, 3)):
    print(f"  {lbl:30s} p10={np.percentile(arr,10):.2f} p50={np.median(arr):.2f} p90={np.percentile(arr,90):.2f} "
          f"pass@0.5={np.mean(arr >= raw_cutoff(0.5, n)):.0%} pass@0.7={np.mean(arr >= raw_cutoff(0.7, n)):.0%}")
