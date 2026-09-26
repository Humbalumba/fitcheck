# FitCheck backend (FastAPI)

Tells a single demo user whether to buy a clothing item: how many **new outfits** it creates with their
closet (OutfitTransformer compatibility scores) and whether it's **redundant** (fashion-clip + FAISS).
API contract: [`../API.md`](../API.md).

## Setup
```bash
cd backend
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
uv pip install -r requirements.txt
export GEMINI_API_KEY=...        # or put GEMINI_API_KEY=... in backend/.env (picked up without restart)
```
HF weights (segformer_b2_clothes, fashion-clip) download to `backend/models/hf` on first use.
The OutfitTransformer weights (module `app/compat/`, built separately) live in `backend/models/outfit_transformer`
(`python -m app.compat.prepare_weights`). If that module/weights are missing, a clearly-logged TEMPORARY
stub scorer is used instead (`/api/health` → `"scorer": "stub"`).

## Seed the demo closet
```bash
python scripts/seed_demo_closet.py --reset   # ~40 closet items + 8 held-out candidates (~1 min)
python scripts/seed_demo_closet.py           # idempotent top-up
python scripts/demo_report.py                # evaluate every seeded candidate + closet score stats
python scripts/calibrate_redundancy.py       # similarity stats used to pick redundancy thresholds
python scripts/contact_sheet.py /tmp/s.jpg   # visual check of closet + candidates
```
Source: **Polyvore** product cut-outs on white (HF `owj0421/polyvore` images + `owj0421/polyvore-outfits`
test-split outfits; ~2 GB HF cache, already present on this box). We take 8 real women's top+bottom(+outerwear)(+shoes)
outfits, 4 real dress outfits and 2 real men's outfits, so the closet contains sets that genuinely go together:
11 tops, 10 bottoms, 7 outerwear, 4 dresses, 8 shoes (32 womens / 8 mens items). Attributes come from the Polyvore category +
product-name keywords with fashion-clip zero-shot filling gaps (no Gemini needed). The earlier
`ashraq/fashion-product-images-small` closet was dropped: most of its photos show a person wearing the item, which
pushes OutfitTransformer scores to ~1 for everything. Picks are pinned in `data/seed_manifest.json`.
Held-out candidates (status `candidate`, `attributes.test_key`): `top_womens`, `bottom_womens`, `outerwear_womens`,
`dress_womens`, `shoes_womens`, `bag_womens` (unsupported demo), `top_mens`, `near_dup_top` (auto-picked: closest
Polyvore top to a closet top, a black cami vs the closet's black tank). Their images are also in `data/test_images/product_*.jpg`
next to real multi-item photos (flat lays, clothes on a bed, mannequin) for Stage 1-2 testing.

## Run
```bash
./run.sh          # foreground on 0.0.0.0:8000 (refuses if port busy; PORT=8001 ./run.sh)
./run.sh --bg     # background, logs to server.log, pid in server.pid
./stop.sh
```

## Test
```bash
python -m pytest tests -q     # 18 tests on a snapshot copy of the DB; ~15 s
GEMINI_API_KEY=... python scripts/test_gemini.py   # Stage 1+2 on data/test_images (throwaway DB)
```

## Pipeline
| Stage | Where | What |
|---|---|---|
| 1 Capture & segment | `app/pipeline.py`, `app/gemini.py`, `app/segment.py` | Gemini `box_2d` boxes (structured output) → padded crop → segformer_b2_clothes mask (category classes, fallback to dominant clothing class, largest components, fill holes) → transparent PNG + white JPG. Poor mask → plain crop. Offline fallback: segformer box proposals. |
| 2 Identify | `app/gemini.py` (`Attributes` schema) | cutout + context crop → category, subcategory, colors, pattern, fabric, formality 1-5, seasons, style tags, gender, brand, price (only if tag visible). Offline fallback: fashion-clip zero-shot (`app/fallback.py`). |
| Storage | `app/db.py` | SQLite `data/fitcheck.db`: photos, items, item_embeddings, compat_edges (outfit score cache), evaluations, outfits, outfit_items, settings. |
| Vectors | `app/vectors.py` | fashion-clip image embeddings (white cutout, L2-norm) in FAISS `IndexIDMap(IndexFlatIP)` → `data/closet.faiss` (+ `closet.ids.npy`). Rebuilt from DB if stale. |
| 3-4 Redundancy | `app/evaluate.py::redundancy` | same-category closet items, similarity = 0.7·image cosine (FAISS) + 0.3·text cosine (fashion-clip text embedding of "color pattern subcategory"). ≥0.88 near duplicate, ≥0.80 similar. |
| 5 Outfits | `app/evaluate.py::generate_outfits`, `app/scoring.py` | Slot-by-slot greedy search scored by OutfitTransformer (`app/compat`), calibrated per outfit size, kept iff calibrated ≥ `compat_threshold`; shoes as optional last layer; raw scores cached in `compat_edges`. |
| 6 Verdict | `app/evaluate.py::compute_value_and_verdict` | Pure math (below). |

### Compatibility calibration (`app/scoring.py`)
Raw OutfitTransformer scores depend on outfit size (Polyvore balanced cutoffs: 2 items 0.15, 3 items 0.35, 4+ 0.6 —
`app.compat.threshold_for`). Each raw score is mapped to a **calibrated** score with a per-size piecewise-linear curve
through (0→0), (threshold_for(n)→0.5), (1→1). The user-facing `compat_threshold` ("match strictness", default 0.5)
keeps an outfit iff calibrated ≥ strictness, i.e. raw ≥ cutoff_n(s): s=0.5 → exactly threshold_for(n); s=0 → everything;
s=0.75 → 2 items 0.575 / 3 items 0.675 / 4+ 0.8. API returns both `raw_score` and `score` (calibrated); UI and value
math use the calibrated one. On the demo closet (70 gender-compatible top×bottom pairs) at s=0.5: 50% of 2-item pairs
pass; judged with the best closet shoe (3 items) 56% pass; at s=0.7 27% / 47%.

### Outfit templates & counting
Built strictly by slot (the model doesn't penalize two tops): top/bottom → top+bottom, top+bottom+outerwear;
outerwear → top+bottom+outerwear (closet pairs that pass, top 40), dress+outerwear; dress → dress, dress+outerwear;
shoes → top+bottom+shoes, dress+shoes. **Shoes are an optional last layer:** if the closet has shoes, every base look is
scored with each shoe and judged by its best one (3-item scoring separates much better than 2-item: AUC 0.83 vs 0.70),
and the template name gets `+shoes`. N counts base looks + their outerwear variants, one per look — shoes never multiply
the count. A shoes candidate counts a look only if it passes and beats every shoe you already own for that look.
Gender: mens-only and womens-only items are never combined (`match_gender_presentation`).

### Verdict formula (one function: `compute_value_and_verdict`)
```
N = #outfits (each passed compat_threshold);  W = sum of their scores
cost_per_outfit = price / max(N,1)
r = W * max_cost_per_outfit / price            (no price: r = W / min_new_outfits)
value_score = round(100 * redundancy_factor * budget_factor * r/(1+r))   # 50 == exactly at your $/outfit limit
redundancy_factor: none 1.0 | similar 0.7 | near_duplicate 0.2;  budget_factor 0.5 if price > remaining monthly budget
BUY iff N >= min_new_outfits AND not near_duplicate AND cost_per_outfit <= max_cost_per_outfit AND price <= remaining budget
```
Remaining budget = `monthly_budget` − prices of items bought (add-to-closet) this calendar month.

## Known limitations
* **Menswear is weak in the compat model**: Polyvore is ~0.2% men's items; men's outfits score noisier/lower and
  good vs bad men's combos were barely separable in the compat worker's tests (AUC ~0.5). The demo closet has only
  8 men's items (2 real Polyvore men's outfits).
* The compat model expects the garment alone on white. Photos of the item being worn saturate scores near 1.
  Stage 1 cutouts on white handle flat-lay / hanger photos; worn photos still go through segformer, but the other
  garments get masked out only as well as segformer manages.
* Absolute thresholds are Polyvore-calibrated; best-shoe selection makes the shoe layer somewhat optimistic
  (the more shoes you own, the more looks pass).
* Redundancy thresholds (0.88 / 0.80) were tuned on Polyvore product shots; real phone photos of the same kind of
  garment may score higher — adjust via `/api/settings` if needed.
* Seeded attributes come from product names + zero-shot, so a few colors/subcategories are off (editable via PATCH).
* Accessories (bags, jewellery...) are not evaluated (`UNSUPPORTED`).
