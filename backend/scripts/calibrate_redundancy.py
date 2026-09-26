#!/usr/bin/env python
"""Similarity stats on the demo closet (image cosine, text cosine, and the 0.7/0.3 blend used for redundancy)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402

from app import db  # noqa: E402
from app.evaluate import redundancy_text, text_embeddings  # noqa: E402
from app.vectors import ensure_fclip  # noqa: E402

W = float(sys.argv[1]) if len(sys.argv) > 1 else 0.3
items = db.list_items(status="closet") + db.list_items(status="candidate")
V = np.stack([ensure_fclip(i) for i in items])
T = text_embeddings([redundancy_text(i) for i in items])
SI, ST = V @ V.T, T @ T.T
S = (1 - W) * SI + W * ST
names = [f"{i['status'][:4]}:{redundancy_text(i)[13:]} ({i['attributes'].get('description','')[:22]})" for i in items]
cats = [i["category"] for i in items]
for lbl, M in (("image", SI), ("blend", S)):
    same = np.array([M[a, b] for a in range(len(items)) for b in range(a + 1, len(items)) if cats[a] == cats[b]])
    print(f"{lbl:6s} same-category n={len(same)} p50={np.median(same):.3f} p90={np.percentile(same, 90):.3f} "
          f"p97={np.percentile(same, 97):.3f} max={same.max():.3f}")
print(f"\nTop same-category pairs (blend w_text={W}):")
pairs = sorted(((S[a, b], SI[a, b], a, b) for a in range(len(items)) for b in range(a + 1, len(items))
                if cats[a] == cats[b]), reverse=True)
for s, si, a, b in pairs[:15]:
    print(f"  {s:.3f} (img {si:.3f})  {names[a]:50s} <-> {names[b]}")
