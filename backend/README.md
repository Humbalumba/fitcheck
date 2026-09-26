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
python -m pytest tests -q     # 81 tests on a snapshot copy of the DB, Gemini off (FITCHECK_GEMINI_OFF=1); ~25 s
# test data: $FITCHECK_TEST_DATA_DIR, else ../../fitcheck-testdata if present, else data/. The live closet can be
# small/empty, so seed a scratch dir once:  FITCHECK_DATA_DIR=../../fitcheck-testdata python scripts/seed_demo_closet.py --reset
GEMINI_API_KEY=... python scripts/test_gemini.py   # Stage 1+2 on data/test_images (throwaway DB)
# previews: /tmp/gemini_<model>_<photo>.jpg (boxes) and ..._cuts.jpg (cutouts + segmentation strategy)
# compare a model: GEMINI_MODEL=gemini-3.7-flash GEMINI_FALLBACK_MODEL=gemini-3.7-flash GEMINI_CHAIN_MAX=1 python scripts/test_gemini.py
```

## Pipeline
| Stage | Where | What |
|---|---|---|
| 1 Capture & segment | `app/pipeline.py`, `app/gemini.py`, `app/segment.py` | Default `GEMINI_MODE=single`: ONE Gemini call per photo returns `box_2d` boxes + full attributes for every garment (structured output). Padded crop → segformer_b2_clothes mask candidates (category classes / dominant class / garment-class union, since segformer confuses trousers↔"dress" on flat lays) → quality gate (mask must span ≥80% of the tight Gemini box and fill ≥25%) → transparent PNG + white JPG. Poor mask, accessories segformer can't see, or an item >30% covered by other detected items (dense flat lay) → plain crop. Offline fallback: segformer box proposals. |
| 2 Identify | `app/gemini.py` (`Attributes` schema) | category (jackets/blazers/cardigans = outerwear), subcategory, colors, pattern, fabric, formality 1-5, seasons, style tags, gender, brand, price (only if a tag is readable). `GEMINI_MODE=two_stage` = boxes first, then one call per item with cutout + context crop (N+1 requests). Offline fallback: fashion-clip zero-shot (`app/fallback.py`). |
| Storage | `app/db.py` | SQLite `data/fitcheck.db`: photos, items, item_embeddings, compat_edges (outfit score cache), evaluations, outfits, outfit_items, settings. |
| Vectors | `app/vectors.py` | fashion-clip image embeddings (white cutout, L2-norm) in FAISS `IndexIDMap(IndexFlatIP)` → `data/closet.faiss` (+ `closet.ids.npy`). Rebuilt from DB if stale. |
| 3-4 Redundancy | `app/evaluate.py::redundancy` | same-category closet items, similarity = 0.7·image cosine (FAISS) + 0.3·text cosine (fashion-clip text embedding of "color pattern subcategory"). ≥0.88 near duplicate, ≥0.80 similar. |
| 5 Outfits | `app/evaluate.py::generate_outfits`, `app/scoring.py` | Slot-by-slot greedy search scored by OutfitTransformer (`app/compat`), calibrated per outfit size, kept iff calibrated ≥ `compat_threshold`; shoes as optional last layer; raw scores cached in `compat_edges`. |
| 6 Verdict | `app/verdict.py::compute_verdict` (inputs gathered by `app/evaluate.py::verdict_for`) | Pure math (below): one 0-100 score → BUY / CONSIDER / SKIP. |

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

### Verdict formula (`app/verdict.py::compute_verdict`, pure math — no LLM)
One 0-100 score → **BUY ≥ 65 · CONSIDER 45-64 · SKIP < 45**. Unit tests: `tests/test_verdict.py`.
```
n  = #new outfits (each passed "match strictness");  M = max outfits this item's slot COULD make with this closet
     (every base look the builder tries: top = #bottoms x (1 + #outerwear), bottom = #tops x (1 + #outerwear),
      outerwear = min(#top-bottom pairs, 40) + #dresses, dress = 1 + #outerwear, shoes = #top-bottom pairs + #dresses)
versatility    (40%) = 1/2 log(1+n)/log(1+M') + 1/2 min(1, log(1+n)/log(1+min(8, M')))       M' = max(M, 2)
outfit quality (20%) = mean calibrated score of the best 5 outfits
cost per wear  (40%) = clamp(0.6 + 0.3 log2(price_bar / cost_per_wear), -0.5, 1)
    cost_per_wear = price / expected_wears   (expected_wears = app.sustainability.expected_wears: PEFCR base wears
                    for the garment type x utility(n) [0.5x..2x, log] x 0.75 similar / 0.5 near-duplicate)
    price         = user price > tag price > ESTIMATE (app/pricing.py; estimated cost weight x1 / x0.75 / x0.5 for
                    high / medium / low confidence; the reason says "(est. price ~$60)")
    price_bar     = "your usual cost per wear" = median of price / base_wears(type) over closet items with a real
                    (user/tag) price when >= 3 have one ("closet_median"); else over real prices (counted twice) +
                    the estimates of unpriced items ("closet_estimates"); $0.75/wear default only when neither exists
score = weighted mean (weights renormalised if there's no price) + gap fill + similarity, clamped 0..100
    gap fill:   +12 first of its category (first dress / outerwear), +6 first of its garment type, -6 if you own >= 5
                of that type (counts only items it could be worn with, i.e. gender-compatible)
    similarity: -5 if "similar" to something you own
automatic SKIP (score capped at 44): near-duplicate, or no outfits although the closet has partners for it;
nothing to pair it with at all yet: capped at 64 (never an outright BUY)
reasons = the 2 factors that moved the score most (BUY: most positive, SKIP: most negative, CONSIDER: one of each)
```
The evaluation returns `verdict.{decision, score, reasons (2), all_reasons, components, bands}` and
`value.{cost_per_wear, expected_wears, price_bar, price_bar_source, versatility.{new_outfits, max_possible}}`.
Evaluations saved by the old BUY/SKIP formula get the new verdict computed on read (`evaluate.with_current_verdict`).
There is no monthly spending limit anywhere; old settings rows with retired keys are ignored.

### Price estimates (`app/pricing.py`)
- New items: the Gemini detection schema has `estimated_price_usd` + `price_confidence` (same call, no extra request);
  `pricing.normalize_estimate` validates them (clamps $3..$5000, "high" only with a brand, table fallback if omitted).
- Fallback detector (FashionCLIP zero-shot): per-garment-type median prices from `app/data/price_defaults.json`
  (ROUGH US mid-market defaults, confidence "low").
- Existing items: `pricing.backfill()` = ONE batched text-only Gemini call from the stored attributes for every item
  with neither a price nor an estimate (closet first, <= 80 items), table fallback on any error (quota). Runs once in
  a background thread at startup (`FITCHECK_PRICE_BACKFILL=0` to disable; tests disable it) and via
  `scripts/backfill_price_estimates.py [--no-gemini] [--dry-run]`. Only the estimate keys are written.
- An evaluation never calls Gemini for prices: an item without a stored estimate gets the table estimate on the fly.

## Shopping suggestions (`app/suggest.py`)
After a verdict the UI calls `POST /api/evaluations/{id}/suggestions` (see API.md). SKIP or CONSIDER → up to 3 better
same-type alternatives; BUY → up to 3 pairings from other slots that are themselves BUYs (score ≥ 65) with the future
closet (never padded).
* **One Gemini call per evaluation**, cached in the `suggestions` table. Primary: Google Search grounding
  (`types.Tool(google_search=...)`) on the failover chain. **Grounding needs a paid-tier key**; on this free key
  every model answers with a tier 429 — detected (no quota metric), remembered for 6 h, and it does *not* mark
  models exhausted. Fallback: one JSON-mode call plans ~8 shopping queries, run live on public Shopify storefront
  search of ~16 stores (`SUGGEST_STORES="domain|Name|wm,..."` overrides the list).
* Each product goes through the same pieces as a closet photo, minus Gemini: photo → segformer cutout (white-bg
  product shots used as-is) → fashion-clip embedding → redundancy vs closet → OutfitTransformer outfits → verdict.
  Stored as items with `status='suggestion'` (the items CHECK constraint is migrated on startup by a table rebuild
  that preserves rows/FKs), media in `data/media/suggest/`; never in the closet list or FAISS.
* Env: `SUGGEST_GROUNDING=off` (skip straight to store search), `SUGGEST_FETCH_TIMEOUT_S` (default 8),
  `FITCHECK_SUGGEST_FIXTURE_DIR=<dir>` (replay `<mode>.products.json` / `<mode>.json` (grounded) /
  `<mode>.plan.json` instead of calling Gemini — `tests/fixtures/suggest` has real recorded responses),
  `FITCHECK_GEMINI_OFF=1` (never call Gemini). Raw plan responses are recorded to `data/suggest_raw/`.
* Limitations: store search only covers Shopify retailers (no Uniqlo/Zara/H&M/Gap); products whose only photos are
  lifestyle / worn shots are dropped (shoes need a product-only shot); color comes from the listing title/tags,
  else fashion-clip zero-shot; menswear is weaker (few men's stores + the compat model); ~25-30 s per first call.

## Sustainability score (`app/sustainability.py`)
Pure Python (no network / Gemini / DB); factors with sources in `app/data/sustainability_factors.json`, method in
`docs/SUSTAINABILITY.md`. `evaluate()` calls `score_item(attributes, category, n_new_outfits, top_similarity, price,
dup_threshold=…, similar_threshold=…)` after the verdict is final and returns/saves it as `sustainability`; it
never changes the verdict (an estimator error just yields `null`). Suggestions get the same score per product
(final ranking tiebreaker only). Old evaluations / cached suggestions get it computed on read
(`GET /api/evaluations/{id}`, suggestions endpoints); closet items that were evaluated expose it at
`GET /api/items/{id}/sustainability`. Seeded demo items have no fabric → average textile (e.g. white trousers 77 B,
near-duplicate black cami 44 D). It's an estimate: generic fibre factors, no durability model, modelled wear curve.

## Clean product images (`app/render.py`, `app/render_template.py`, `app/render_prompt.py`)
Every closet item gets a display-only catalogue image (embeddings keep using the original cutout, except a
*verified* Gemini redraw). Priority: **Gemini redraw** (if available & verified) > **canonical template** > **cleanup**.
- *Gemini redraw*: attribute-driven, canonical-pose prompt (`render_prompt.py`: garment type/view/neckline/sleeves/
  closure/hood/pockets/lining/hardware + every logo with its position) plus the template render as a pose schematic
  (image 3); verified by fashion-clip + LAB colour + a Gemini vision QA call; one retry with the QA issues.
- *Template* (no image generation): procedural brand-style garment templates (`garment_templates.py`: tee, V-neck,
  long-sleeve, sweater, polo, hoodie, zip hoodie, jacket, jeans, trousers, shorts, skirt, dresses) recoloured with the
  robust LAB fabric colour, real logo pixels transplanted upright at their anchor; patterned / multi-colour /
  unsupported items fall back to the cleanup.
- *Cleanup*: segmentation + gentle tone/WB + tilt fix on a white 1024² canvas.
- `GEMINI_IMAGE_API_KEY` in `backend/.env` (or env) = key for image generation + QA (e.g. billing-enabled); falls back
  to `GEMINI_API_KEY`. The quota auto-disable (`data/render_state.json`, stores only a sha256 prefix of the key)
  resets automatically when this key changes. Other knobs: `RENDER_GEMINI=auto|on|off`, `RENDER_TEMPLATE=0`,
  `RENDER_POSE_REF=0`, `RENDER_MODEL`, `RENDER_CLIP_MIN` (0.75, vs the cleanup cutout) and
  `RENDER_CLIP_MIN_TEMPLATE` (0.65, vs the canonical template render; only counts when the Gemini vision QA ran and
  passed, because a flat-lay re-layout legitimately scores lower against the crumpled cutout).
- Re-render the closet: `python scripts/render_closet.py [--mode auto|gemini|template|cleanup] [--dry-run] [--ids ..]`
  (drives the running backend; `--local` renders in-process).

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

## Gemini model & quota (tested 2026-09-25/26)
- Default `GEMINI_MODEL=gemini-3-flash-preview`: same tight boxes as `gemini-3.8-flash` (newest, what "auto" picks)
  on the shared test photos, but 2-4 s per photo vs 3-14 s (3.8 rejects `thinking_level=minimal`, needs "low")
  and fewer 503 "high demand" errors. `gemini-2.5-flash` returns **404 "no longer available to new users"** for
  this key; `gemini-3.5-flash` was overloaded (503s, ~20 s).
- Free-tier key = **20 requests/day per model** (resets midnight PT = 3 AM ET) plus a per-minute limit. That's why
  single-call mode is the default (1 request per photo instead of 1 + items).
- Failover chain: the configured model, then every listed Flash model newest-first, then `gemini-flash-latest`.
  A daily-quota 429 marks the model exhausted until reset and moves on; per-minute 429 / 503 are retried briefly.
  `/api/health` shows `gemini_model` (current) and `gemini_exhausted`.
