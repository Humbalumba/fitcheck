# app/compat: OutfitTransformer outfit compatibility (Stage 5)

**Model:** OutfitTransformer (Sarkar et al., WACV 2023), community re-implementation
https://github.com/owj0421/outfit-transformer (MIT). This uses the "CLIP" variant's **compatibility-prediction**
checkpoint (`compatibillity_clip_best.pth`, Google Drive id `1mzNqGBmd8UjVJjKwVa5GdGYHKutZKSSi`),
trained on Polyvore Outfits (nondisjoint). Repo's reported CP AUC is 0.95; on a 400-outfit test sample here I got AUC 0.922.
The module only borrows the architecture and weights. It has no hand-written matching rules.

**Item embedding (1024-d float32):** `[L2norm(fashion-clip image_embeds) ; L2norm(fashion-clip text_embeds)]`,
using `patrickjohncyh/fashion-clip` (frozen, weights taken from the checkpoint). The model uses both the image and the text.
The text was trained on Polyvore `url_name` strings such as "christian pellizzari floral jacquard trousers", so short
lowercase descriptions work. Leaving the text out lowers quality a little.

**Outfit scoring:** a learned task token plus the item embeddings go through a 6-layer transformer (d=1024, 16 heads).
A sigmoid on the task-token output gives P(compatible). An outfit holds 1 to 16 items, and the item order doesn't matter.

## Install
```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r app/compat/requirements.txt   # the backend .venv already has everything except gdown
cd backend && python -m app.compat.prepare_weights   # ~1.15GB download, then splits into safetensors (~806MB) and deletes the zip/pth
```
The weights go in `backend/models/outfit_transformer/`, which the repo-root `.gitignore` already covers with `backend/models/`.
You can override the location with `FITCHECK_COMPAT_MODEL_DIR`. Once prepared, it runs fully offline.

## Usage
```python
from app.compat import get_scorer, threshold_for   # or CompatibilityScorer(device="cpu")
s = get_scorer()                                     # ~3-7s load, ~1.3GB RSS peak
e = s.embed_item("top.jpg", "navy slim chino pants, cotton, casual")   # cache per item
E = s.embed_items([p1, p2], [t1, t2])                # batched version
scores = s.score_outfits([[e_top, e_bottom], [e_top, e_bottom, e_shoes]])
ok = [sc >= threshold_for(len(o)) for sc, o in zip(scores, outfits)]
```

## Speed (8-core CPU, no GPU)
embed: ~20-40 ms/item batched on small images, ~60 ms/item on 300px images including decode.
score_outfits: 30x2 items in 20 ms, 100x2 in 45 ms, 100x3 in 50 ms, 100x4 in 70 ms, 1000x3 in 480 ms.

## Calibration and caveats (read this)
* **Scores depend on outfit length.** On the real Polyvore test split, real top+bottom pairs average 0.35 and random pairs 0.15.
  Real top+bottom+shoes sets average 0.60 and random ones 0.17. Full real outfits average 0.80, versus 0.15 for Polyvore negatives.
  So a flat 0.5 cutoff only makes sense for 4+ item outfits. Use `threshold_for(n)`: 2 → 0.15, 3 → 0.35, 4+ → 0.6.
  These are the balanced-accuracy optima on Polyvore. Scoring top+bottom+shoes separates good from bad better than a 2-item pair
  (AUC 0.83 vs 0.70), so include shoes when the wardrobe has them. Ranking or per-user relative thresholds are more robust than absolute ones.
* **It does not penalize redundancy.** The model learned whether a set looks like a curated set versus a random one. It did not learn
  outfit structure: the same shirt twice scores 0.85, and two tops score about the same as a random top+bottom. Handle redundancy
  separately, for example with embedding similarity to existing wardrobe items. Build candidate outfits by slot (top × bottom × shoes).
* **Photos must show the item alone.** Polyvore items are product cut-outs on white. Catalog or wardrobe photos where a person
  wears the garment already show a whole outfit, and scores saturate near 1.0 for everything. Resolution isn't the cause: images
  downscaled to 60px still work. Use flat-lay, hanger, or cut-out photos, ideally with background removal and a crop to the garment.
* **Gender bias.** Polyvore is overwhelmingly women's fashion; only about 0.2% of item names say "mens". Men's outfits made from
  Polyvore men's items mostly score low (0.05-0.25), and good and bad were not separable on random men's combos (AUC ~0.5).
  Expect weaker, noisier signals for menswear.
