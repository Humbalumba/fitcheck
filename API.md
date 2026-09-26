# FitCheck API

Base URL (dev): `http://localhost:8000`. JSON everywhere except `POST /api/detect` (multipart).
CORS: `http://localhost:3000` plus any origin (dev). Media files are served statically under `/media/...`;
every `*_url` field is an absolute path like `/media/cutouts/abc.png` — prefix it with the API base.

Timestamps (`created_at`) are ISO-8601 UTC.

## Item object

```json
{
  "id": "it_2a8505fb19fc",
  "status": "closet",                 // "closet" | "candidate" | "detected" | "suggestion" (shopping pick; never in the closet/FAISS)
  "category": "top",                  // top | bottom | dress | outerwear | shoes | accessory
  "image_url": "/media/white/498fb1ccdf6f_0.jpg",    // cutout on white (use this for display)
  "cutout_url": "/media/cutouts/498fb1ccdf6f_0.png", // transparent PNG
  "crop_url": "/media/crops/498fb1ccdf6f_0.jpg",     // plain crop from the original photo (extra)
  "bbox": [120, 80, 640, 510],        // [ymin,xmin,ymax,xmax] 0-1000 in the source photo (null for seeded items)
  "label": "navy crew-neck t-shirt",  // detector label (extra)
  "photo_id": "ph_e032095d0228",      // extra
  "attributes": {
    "category": "top",
    "subcategory": "t-shirt",
    "primary_color": "navy",
    "secondary_colors": [],
    "pattern": "solid",
    "fabric_guess": "cotton jersey",
    "formality": 2,
    "formality_label": "casual",      // 1 very casual | 2 casual | 3 smart casual | 4 business | 5 formal
    "seasons": ["spring", "summer"],
    "style_tags": ["minimalist", "casual"],
    "gender_presentation": "mens",    // mens | womens | unisex
    "brand": null,
    "price": null,                    // number, only if a price tag was visible (or user-entered)
    "currency": null,
    "description": "Navy cotton crew-neck t-shirt",
    "source": "gemini",               // "gemini" | "fashion-clip-zero-shot" (offline fallback) | "seed:..."
    "segmentation": "category"        // "category" | "dominant" | "fallback_crop"
  },
  "created_at": "2026-09-26T03:13:23+00:00"
}
```
Attributes are free-form JSON: the UI can PATCH any key. Extra keys may appear (`edited`, `purchased_at`,
`purchase_price`, `price_source`, ...).

**Price estimates.** Every item gets `estimated_price_usd` (typical new US retail for brand + type + material),
`price_confidence` (`"high"` = brand clearly identified, `"medium"`, `"low"`) and `price_estimate_source`
(`"gemini"` = read in the detection call itself, no extra request; `"gemini_text"` = one batched text-only backfill
call for items saved before estimates existed; `"table"` = per-type defaults in `backend/app/data/price_defaults.json`
when Gemini is unavailable). `price` is only ever a real price: `price_source` `"user"` (typed in; PATCHing `price`
sets it), `"tag"` (read off a price tag) or `"seed"`. The estimate never overwrites `price`.
Backfill: runs once in the background at startup (`FITCHECK_PRICE_BACKFILL=0` disables it) and via
`backend/scripts/backfill_price_estimates.py`.

---

## GET /api/health
```json
{"ok": true, "gemini": true, "scorer": "outfit_transformer", "gemini_model": "gemini-3-flash-preview",
 "gemini_mode": "single", "gemini_exhausted": [], "closet_size": 40}
```
`gemini_model` = model currently in use (after failover), null until first resolved. `gemini_exhausted` = models
whose free-tier quota is used up (skipped until reset). `gemini_mode` = `single` (1 call/photo) | `two_stage`.
`gemini` = an API key is configured. `scorer` = `"outfit_transformer"` (real model) or `"stub"` (temporary placeholder).

## POST /api/detect
Multipart: `file` (jpeg/png/webp/heic), optional `purpose` = `closet` | `candidate`.
Runs Stage 1+2 (one Gemini call: boxes + attributes → segformer cutout per box, plain crop when the mask is
poor). All items are saved with `status: "detected"`. Takes ~5-12 s (up to ~25 s if Gemini is busy).
`attributes.segmentation` = `category` | `dominant` | `garment_union` | `fallback_crop`.
```json
{
  "photo_id": "ph_e032095d0228",
  "image_url": "/media/originals/498fb1ccdf6f.jpg",
  "detector": "gemini",            // extra: "gemini" | "segformer" (offline fallback) | "whole_image"
  "items": [ Item, Item, ... ]     // each with bbox, label, cutout_url, crop_url, attributes
}
```

## POST /api/closet/items
Moves detected (or candidate) items into the closet; computes embeddings and updates the FAISS index.
```json
// request
{"item_ids": ["it_2a8505fb19fc", "it_77c0e1d2aa01"],
 "attributes_overrides": {"it_2a8505fb19fc": {"primary_color": "olive", "subcategory": "chinos"}}}
// response
{"items": [Item, Item]}
```

## GET /api/closet/items?category=top
`category` optional. → `{"items": [Item, ...]}`

## PATCH /api/closet/items/{id}
`{"attributes": {"formality": 4, "primary_color": "charcoal"}}` → `Item` (merged; `formality_label` auto-updated).
Works for any item id (detected / candidate / closet).

## DELETE /api/closet/items/{id}
→ `{"ok": true}`

## GET /api/items/{id}  (extra)
→ `Item`

## POST /api/evaluate
```json
{"item_id": "it_...", "price": 45}      // price optional; overrides/sets the item's price
```
The item becomes a `candidate` if it was `detected`. Response (trimmed real output for the seeded white
trousers candidate against the Polyvore demo closet):
```json
{
  "item": Item,
  "template_names": ["top+bottom+shoes", "top+bottom+outerwear+shoes"],
  "redundancy": {
    "level": "none",                          // "none" | "similar" | "near_duplicate"
    "top_similarity": 0.7981,
    "matches": [                              // up to 3 same-category closet items with similarity >= 0.75
      {"item": Item, "similarity": 0.7981, "image_similarity": 0.8423, "text_similarity": 0.6948}
    ]
  },
  "outfits": [                                // sorted by template (template_names order) then score; max 60/template
    {"template": "top+bottom+shoes", "score": 0.9851, "raw_score": 0.9807, "n_items": 3,
     "items": [Item /*top*/, Item /*candidate trousers*/, Item /*boots*/]},
    {"template": "top+bottom+outerwear+shoes", "score": 0.93, "raw_score": 0.83, "n_items": 4, "items": [...]}
  ],
  "outfits_truncated": false,
  "outfit_count_by_template": {"top+bottom+shoes": 7, "top+bottom+outerwear+shoes": 35},
  "total_new_outfits": 42,
  "value": {"price": 45.0, "currency": "USD",                // the price used: user > tag > estimate
            "price_source": "user",                  // "user" | "tag" | "estimated" (no price given -> the estimate)
            "price_confidence": null,                // estimates only: "high" | "medium" | "low"
            "estimated_price": 60.0,                 // the item's estimate (present even when a user price won)
            "cost_per_wear": 0.35, "expected_wears": 130.0,       // same wears model as sustainability
            "price_bar": 1.5, "price_bar_source": "closet_estimates",
            // "closet_median": >= 3 closet items with real (user/tag) prices; "closet_estimates": fewer, so real
            // prices (counted twice) + estimates of the unpriced items; "default" ($0.75) only with neither
            "priced_closet_items": 0, "estimated_closet_items": 40,
            "versatility": {"new_outfits": 42, "max_possible": 56, "share": 0.75},
            "outfit_quality": 0.999, "weighted_outfits": 38.993, "value_score": 96},   // value_score == verdict.score
  "verdict": {
    "decision": "BUY",                        // "BUY" | "CONSIDER" | "SKIP" | "UNSUPPORTED"
    "score": 96,                              // 0-100: BUY >= 65, CONSIDER 45-64, SKIP < 45
    "uncapped_score": 96, "capped_by": null,  // automatic SKIPs are capped at 44: "near_duplicate" | "no_outfits"
    "reasons": ["Makes 42 of the 56 outfits a bottom can make with your closet",   // exactly the top 2
                "About $0.35 per wear, below a typical $0.75"],
    "all_reasons": ["...", "...", "Its best outfits are strong matches (1.00 average score)"],
    "components": {                           // sub-scores 0-100 (cost may dip below 0 past 4x your bar)
      "versatility":    {"score": 97, "weight": 0.4, "points": 38.8, "new_outfits": 42, "max_possible": 56},
      "outfit_quality": {"score": 100, "weight": 0.2, "points": 20.0, "top_k": 5},
      "cost":           {"score": 93, "weight": 0.4, "points": 37.2, "cost_per_wear": 0.35, "price_bar": 0.75,
                         "price_bar_source": "default", "price_source": "user", "price_confidence": null},
                         // estimated price: weight x1 / x0.75 / x0.5 for high / medium / low confidence, and the
                         // reason reads "About $0.46 per wear (est. price ~$60), below your usual ~$1.50"
      "gap_fill":       {"points": 0, "category_count": 8, "garment_type": "trousers", "type_count": 1},
      "similarity":     {"points": 0, "level": "none", "top_similarity": 0.7981}},
    "bands": {"BUY": 65, "CONSIDER": 45}, "formula_version": 2},
  "calibration": {"strictness": 0.5, "raw_cutoffs_by_size": {"2": 0.15, "3": 0.35, "4+": 0.6}},
  "scorer": "outfit_transformer",
  "supported": true,
  "message": null,
  "sustainability": {                         // informational estimate; NEVER affects the verdict (see below)
    "supported": true, "is_estimate": true, "score": 77, "grade": "B", "label": "Good",
    "reasons": ["Unlocks 42 outfits, so its ~9.7 kg CO2e is spread over ~130 expected wears (~0.07 kg per wear).",
                "Material unknown, so we assumed an average textile (~22 kg CO2e per kg)."],
    "footprint_kg_co2e": 9.72, "water_l": 1611, "water_complete": true, "expected_wears": 130.0,
    "per_wear_kg_co2e": 0.0748, "per_wear_water_l": 12.4, "cost_per_wear": 0.35,
    "garment_type": "trousers", "footprint_basis": "per_kg", "weight_kg": 0.45,
    "materials": [...], "material_parse": {...}, "components": {...}, "methodology": "...", "sources": [...], "version": "..."
  },
  "evaluation_id": "ev_1e046c19e631"
}
```
**Verdict.** Pure math in `app/verdict.py` (no LLM): versatility vs the most outfits that category could make with
this closet (40%), quality of the best 5 outfits (20%), cost per wear vs your usual cost per wear (40%; median
price ÷ typical wears of priced closet items, topped up with estimates while < 3 have real prices, $0.75 default only
with no prices or estimates at all; with no price the item's estimate is used, labelled "est."), +12/+6 gap-fill bonus (first of its category / garment
type), −6 if you already own ≥ 5 of that type, −5 if similar; near-duplicates are always SKIP. Full formula:
`backend/README.md`.

**Sustainability.** `app/sustainability.py::score_item(candidate attributes, category, total_new_outfits,
redundancy.top_similarity, price)` with the redundancy thresholds from settings, computed after outfits and
redundancy are known and saved with the evaluation. Footprint = typical garment weight × per-kg CO2e/water of the
material mix (WRAP 2012 etc.; shoes use per-pair LCA values); expected wears = PEFCR default wears for the type ×
utility(new outfits) × redundancy factor (0.75 similar / 0.5 near-duplicate); score 0-100 compares the per-wear
footprint with a typical item of that type (50 = typical, +30 per halving); grade A ≥ 80 … E < 35. Unknown material
→ average textile. Accessories: `{"supported": false, "score": null, ...}` (UI hides the card); if the estimator ever
fails the field is `null`. Method + sources: `docs/SUSTAINABILITY.md`. The seeded black cami (near-duplicate, 30
outfits, $28) gets 44 D ("~3.2 kg CO2e over only ~39 expected wears").

## GET /api/evaluations/{evaluation_id}
A saved evaluation, same shape as the `POST /api/evaluate` response (+ `created_at`). Evaluations saved before the
sustainability score existed get `sustainability` computed on read (pure Python, using the stored settings snapshot).
Evaluations saved by the old BUY/SKIP formula get `value` + `verdict` recomputed on read from the stored outfit
count/scores, redundancy and price with the current closet (`verdict.recomputed_on_read: true`,
`verdict.original_decision`).
`404` if unknown.

## GET /api/items/{item_id}/sustainability
The `sustainability` object for an item that went through "Should I buy?" (e.g. bought → now in the closet): the
item's **current** attributes (so a corrected fabric counts) + new outfits / redundancy / price stored with its latest
evaluation, plus `evaluation_id`, `evaluated_at`, `n_new_outfits_at_evaluation`. `404` if never evaluated (the
closet sheet then shows nothing).
**Scores.** `score` = *calibrated* compatibility in [0,1] (use this in the UI, e.g. as a %): 0.5 means exactly
the balanced cutoff for an outfit of that size, 1.0 = a perfect raw score. `raw_score` = the OutfitTransformer
probability; raw scores are NOT comparable across outfit sizes (2 items ≈ 0.15 cutoff, 3 ≈ 0.35, 4+ ≈ 0.6),
which is why the calibrated one exists. `raw_score` is `null` for the no-shoes "dress alone" outfit (fixed score 0.5).
An outfit is kept iff `score >= settings.compat_threshold` (the "match strictness" slider, default 0.5).
`weighted_outfits` = sum of calibrated scores.

A near-duplicate example (seeded black cami vs the closet's black tank top):
`redundancy.level: "near_duplicate"`, `top_similarity: 0.898`, `verdict: {"decision": "SKIP", "score": 44,
"uncapped_score": 76, "capped_by": "near_duplicate", "reasons": ["Very similar to your black tank top (0.90 match)",
"You already own 5 t-shirts and tops"], ...}`.

**Templates** (`template_names` is always returned in this order; the frontend should group by it):

| candidate category | closet has shoes (and `use_shoes_layer`) | no shoes in closet |
|---|---|---|
| top / bottom | `top+bottom+shoes`, `top+bottom+outerwear+shoes` | `top+bottom`, `top+bottom+outerwear` |
| outerwear | `top+bottom+outerwear+shoes`, `dress+outerwear+shoes` | `top+bottom+outerwear`, `dress+outerwear` |
| dress | `dress+shoes`, `dress+outerwear+shoes` | `dress` (dress alone, 1 outfit), `dress+outerwear` |
| shoes | `top+bottom+shoes`, `dress+shoes` | same |
| accessory | unsupported | unsupported |

Every outfit has exactly one item per slot (never two tops). **Counting rule:** shoes are a completing
last layer — each base look (top+bottom, top+bottom+outerwear, dress, dress+outerwear) is scored with every
closet shoe and judged by its best shoe, so each look counts once no matter how many shoes you own.
Outerwear variants of a passing top+bottom look count as additional outfits. For a **shoes** candidate a look
counts only if the new shoes pass AND score higher than every shoe you already own for that look.

**Unsupported categories (accessory):** HTTP 200 with `"supported": false`, a friendly `"message"`,
`outfits: []`, `total_new_outfits: 0`, `value.value_score: null` and
`verdict: {"decision": "UNSUPPORTED", "score": null, "reasons": [message]}`. Redundancy is still computed.

## POST /api/evaluations/{evaluation_id}/suggestions[?refresh=true]
Live-shopping picks for an evaluation, called by the UI **after** the verdict renders. Mode follows the verdict:
* `SKIP` or `CONSIDER` → `"mode": "alternatives"`, title "Better picks instead": real products of the **same type**
  in colors / textures unlike the candidate and the closet. Must not be a near-duplicate of the closet, must not share
  the candidate's color, and must beat the candidate on new outfits or score. Ranked by their own verdict
  (BUY > CONSIDER > SKIP), then score, then outfits.
* `BUY` → `"mode": "pairings"`, title "Pairs well with this": real products in the **other slots**
  (top / bottom / outerwear / shoes / dress as relevant). Each one is scored against the *future* closet (closet + the
  candidate, which also counts toward the price bar and gap fill); it must form outfits that include the candidate
  and get its own `BUY` (score ≥ 65) from the verdict formula. Ranked by outfits with the candidate, then outfits,
  then score; at most 2 per category.
* `UNSUPPORTED` → empty `suggestions` with a `message`.

At most **3** suggestions; fewer if fewer qualify (never padded). Results are cached per evaluation in the
`suggestions` table (`cached: true` on re-open, no Gemini call); `?refresh=true` recomputes.

How products are found: **one** Gemini call. With a key that has Google Search grounding (paid tier), it's
`gemini-3-flash-preview` + `google_search`, asking for strict-JSON products (grounding redirect URLs are
resolved). Grounding is **not available on the free tier** (tier 429); the backend then remembers that for 6 h
and instead makes one normal JSON-mode Gemini call that plans ~8 targeted shopping queries, which run live
against public Shopify storefront search (`/search/suggest.json`) of ~16 retailers (Everlane, Princess Polly,
Good American, Marine Layer, tentree, Outerknown, Allbirds, Fashion Nova, Thursday Boots, Bonobos...). Then for
each product: check the link (HTTP 200), fetch a clean product photo (image_url, else og:image / twitter:image /
JSON-LD; on-model and lifestyle shots are rejected when a garment-only shot isn't available), cut it out with
segformer, embed with fashion-clip, and run the normal redundancy + OutfitTransformer + verdict pipeline on it as
a hypothetical item (`status: "suggestion"`, media under `/media/suggest/`). No other Gemini calls. Takes ~25-30 s
on CPU (Gemini ~4 s, search ~3 s, fetch ~9-14 s, pipeline ~10 s).

```json
{
  "evaluation_id": "ev_1cddffd5bd44", "mode": "pairings", "title": "Pairs well with this",
  "candidate_verdict": "BUY", "cached": false, "message": null,       // message set when nothing qualifies
  "source": "store_search",            // or "google_search" (grounded) / fixture replays
  "model": "gemini-3-flash-preview", "search_queries": ["navy quilted vest", "..."], "stores": ["everlane.com", "..."],
  "considered": 8, "with_photo": 6,
  "timing_s": {"gemini": 4.2, "search": 2.9, "fetch": 8.5, "pipeline": 10.1, "total": 25.7},
  "suggestions": [{
    "id": "it_b60c4dbe68f5", "item": { /* Item, status "suggestion" */ },
    "name": "Marina Quilted Vest", "brand": "Marine Layer", "retailer": "Marine Layer",
    "price": 68.0, "currency": "USD",
    "product_url": "https://www.marinelayer.com/products/marina-quilted-vest", "link_status": 200, "link_ok": true,
    "image_url": "/media/suggest/sg_....jpg",        // cutout on white
    "photo_url": "/media/suggest/sg_..._orig.jpg",   // the retailer's photo (local copy)
    "source_image_url": "https://cdn.shopify.com/...",
    "category": "outerwear", "subcategory": "jacket", "color": "navy", "color_source": "listing", "material": null,
    "reason": "Unlocks 43 outfits, 7 with the white trousers, $0.52 per wear",
    "verdict": {"decision": "BUY", "score": 88, "reasons": ["..."], ...}, "value": { /* as in /api/evaluate */ },
    "total_new_outfits": 43, "outfits_with_candidate": 7,
    "outfit_count_by_template": {"top+bottom+outerwear+shoes": 40, "dress+outerwear+shoes": 3}, "template_names": ["..."],
    "redundancy": {"level": "none", "top_similarity": 0.71, "closest": "black jacket"},
    "similarity_to_candidate": null,
    "sustainability": { /* same object as in /api/evaluate: 77, "B", ... for this vest */ },
    "outfits": [ /* up to 12 Outfit objects, ones containing the candidate first */ ]
  }],
  "rejected": [{"name": "...", "retailer": "Fashion Nova", "reason": "no clean photo of the garment (lifestyle / worn shot)"}]
}
```
Each suggestion's `sustainability` uses its own attributes/material (listing title + tags; the product title decides
the garment type), its own new-outfit count and redundancy. It only breaks exact ties in the ranking. Cached results
from before the score existed get it computed on read.

Errors: `404` unknown evaluation; `503 {"detail": "...quota..."}` when Gemini is exhausted/unavailable (nothing
cached, the UI shows a friendly note + Retry); `500` otherwise.

## GET /api/evaluations/{evaluation_id}/suggestions
The cached result (same shape, `cached: true`) or `404` if none yet. Never calls Gemini.

## POST /api/candidate/{item_id}/add-to-closet
"I bought it" → `Item` with `status: "closet"` (records `purchased_at` / `purchase_price`; closet prices feed the
verdict's personal price bar).

## GET /api/settings  ·  PUT /api/settings
PUT takes any subset; returns the full settings object. Unknown / retired keys (e.g. the old outfit-count and
cost-per-outfit limits) are ignored on write and dropped from stored settings on read.
```json
{
  "compat_threshold": 0.5,                 // "match strictness" 0..1 on the CALIBRATED score (0.5 = balanced per-size cutoff)
  "redundancy_similar_threshold": 0.80,
  "redundancy_duplicate_threshold": 0.88,
  "redundancy_text_weight": 0.3,           // extra: similarity = 0.7*image cosine + 0.3*attribute-text cosine
  "style_goal": "",
  "occasions": [],
  "match_gender_presentation": true,       // extra: don't pair mens-only with womens-only items
  "max_pairs_for_layering": 40,            // extra: cap on top+bottom pairs used when evaluating outerwear
  "use_shoes_layer": true                  // extra: complete looks with the best closet shoe (see counting rule)
}
```

Errors: `404 {"detail": "item ... not found"}`, `422` validation, `500 {"detail": "detection failed: ..."}`.
