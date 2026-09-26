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
python scripts/seed_demo_closet.py --reset   # 40 closet items (men's + women's) + 7 held-out candidates
python scripts/seed_demo_closet.py           # idempotent top-up
python scripts/calibrate_redundancy.py       # similarity stats used to pick redundancy thresholds
```
Source: HF dataset `ashraq/fashion-product-images-small`; attributes come from its labels (no Gemini).
`data/seed_manifest.json` pins which products were picked. Held-out candidate images +
real multi-item photos for Stage 1-2 testing are in `data/test_images/`.

## Run
```bash
./run.sh          # foreground on 0.0.0.0:8000 (refuses if port busy; PORT=8001 ./run.sh)
./run.sh --bg     # background, logs to server.log, pid in server.pid
./stop.sh
```

## Test
```bash
python -m pytest tests -q     # runs on a temp copy of the DB; ~30 s
```

## Pipeline
| Stage | Where | What |
|---|---|---|
| 1 Capture & segment | `app/pipeline.py`, `app/gemini.py`, `app/segment.py` | Gemini `box_2d` boxes (structured output) → padded crop → segformer_b2_clothes mask (category classes, fallback to dominant clothing class, largest components, fill holes) → transparent PNG + white JPG. Poor mask → plain crop. Offline fallback: segformer box proposals. |
| 2 Identify | `app/gemini.py` (`Attributes` schema) | cutout + context crop → category, subcategory, colors, pattern, fabric, formality 1-5, seasons, style tags, gender, brand, price (only if tag visible). Offline fallback: fashion-clip zero-shot (`app/fallback.py`). |
| Storage | `app/db.py` | SQLite `data/fitcheck.db`: photos, items, item_embeddings, compat_edges (outfit score cache), evaluations, outfits, outfit_items, settings. |
| Vectors | `app/vectors.py` | fashion-clip image embeddings (white cutout, L2-norm) in FAISS `IndexIDMap(IndexFlatIP)` → `data/closet.faiss` (+ `closet.ids.npy`). Rebuilt from DB if stale. |
| 3-4 Redundancy | `app/evaluate.py::redundancy` | same-category closet items, similarity = 0.7·image cosine + 0.3·text cosine (fashion-clip text embedding of "color pattern subcategory"). ≥0.90 near duplicate, ≥0.82 similar. |
| 5 Outfits | `app/evaluate.py::generate_outfits`, `app/scoring.py` | Greedy layered search with `compat_threshold` (default 0.5) using `CompatibilityScorer.score_outfits`; cached in `compat_edges`. |
| 6 Verdict | `app/evaluate.py::compute_value_and_verdict` | Pure math (below). |

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
