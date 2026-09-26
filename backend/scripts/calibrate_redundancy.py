#!/usr/bin/env python
"""Print fashion-clip cosine similarity stats on the demo closet to tune redundancy thresholds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402

from app import db  # noqa: E402
from app.vectors import ensure_fclip  # noqa: E402

items = db.list_items(status="closet") + db.list_items(status="candidate")
V = np.stack([ensure_fclip(i) for i in items])
S = V @ V.T
names = [f"{i['status'][:4]}:{i['attributes'].get('primary_color')} {i['attributes'].get('subcategory')}" for i in items]
cats = [i["category"] for i in items]
same, diff = [], []
for a in range(len(items)):
    for b in range(a + 1, len(items)):
        (same if cats[a] == cats[b] else diff).append(S[a, b])
for lbl, arr in (("same-category", same), ("cross-category", diff)):
    arr = np.array(arr)
    print(f"{lbl:15s} n={len(arr):4d} mean={arr.mean():.3f} p50={np.median(arr):.3f} p90={np.percentile(arr,90):.3f} "
          f"p99={np.percentile(arr,99):.3f} max={arr.max():.3f}")
print("\nTop same-category pairs:")
pairs = sorted(((S[a, b], a, b) for a in range(len(items)) for b in range(a + 1, len(items)) if cats[a] == cats[b]),
               reverse=True)
for s, a, b in pairs[:15]:
    print(f"  {s:.3f}  {names[a]:40s} <-> {names[b]}")
print("\nCandidates -> best same-category closet match:")
for a, it in enumerate(items):
    if it["status"] != "candidate":
        continue
    best = max(((S[a, b], b) for b in range(len(items)) if items[b]["status"] == "closet" and cats[b] == cats[a]),
               default=(0, None))
    print(f"  {names[a]:40s} -> {best[0]:.3f} {names[best[1]] if best[1] is not None else '-'}")
