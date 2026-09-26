# FitCheck API

Base URL (dev): `http://localhost:8000`. JSON everywhere except `POST /api/detect` (multipart).
CORS: `http://localhost:3000` plus any origin (dev). Media files are served statically under `/media/...`;
every `*_url` field is an absolute path like `/media/cutouts/abc.png` — prefix it with the API base.

Timestamps (`created_at`) are ISO-8601 UTC.

## Item object

```json
{
  "id": "it_2a8505fb19fc",
  "status": "closet",                 // "closet" | "candidate" | "detected"
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

---

## GET /api/health
```json
{"ok": true, "gemini": true, "scorer": "outfit_transformer", "gemini_model": "gemini-2.5-flash", "closet_size": 40}
```
`gemini` = an API key is configured. `scorer` = `"outfit_transformer"` (real model) or `"stub"` (temporary placeholder).

## POST /api/detect
Multipart: `file` (jpeg/png/webp/heic), optional `purpose` = `closet` | `candidate`.
Runs Stage 1 (Gemini boxes → segformer cutout) + Stage 2 (Gemini attributes). All items are saved with
`status: "detected"`. Takes ~3-15 s depending on item count.
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
{"item_id": "it_...", "price": 35}      // price optional; overrides/sets the item's price
```
The item becomes a `candidate` if it was `detected`. Response (trimmed; real output for the seeded
near-duplicate navy tee):
```json
{
  "item": Item,
  "template_names": ["top+bottom", "top+bottom+outerwear"],
  "redundancy": {
    "level": "near_duplicate",                // "none" | "similar" | "near_duplicate"
    "top_similarity": 0.904,
    "matches": [                               // up to 3 same-category closet items with similarity >= 0.75
      {"item": Item, "similarity": 0.904, "image_similarity": 0.8629, "text_similarity": 1.0}
    ]
  },
  "outfits": [                                 // sorted by template then score; max 60 per template
    {"template": "top+bottom", "score": 0.8982, "items": [Item /*candidate*/, Item /*jeans*/]},
    {"template": "top+bottom+outerwear", "score": 0.97, "items": [Item, Item, Item]}
  ],
  "outfits_truncated": false,
  "outfit_count_by_template": {"top+bottom": 4, "top+bottom+outerwear": 12},
  "total_new_outfits": 16,
  "value": {"price": 18.0, "cost_per_outfit": 1.12, "weighted_outfits": 15.093, "value_score": 18,
            "redundancy_factor": 0.2, "budget_remaining": 200.0, "currency": "USD"},
  "verdict": {"decision": "SKIP", "reasons": [
      "Pairs into 16 outfits, but they'd mostly repeat looks you already have",
      "Very similar to your navy t-shirt (0.90 match)",
      "$1.12 per new outfit (within your $10 limit)"]},
  "scorer": "outfit_transformer",
  "supported": true,
  "message": null,
  "evaluation_id": "ev_5576138a1e11"
}
```
A BUY example (seeded olive shirt, price 35): `total_new_outfits: 16`, `redundancy.level: "none"`,
`value: {"price": 35.0, "cost_per_outfit": 2.19, "weighted_outfits": 15.352, "value_score": 81, ...}`,
`verdict: {"decision": "BUY", "reasons": ["Creates 16 new outfits with your wardrobe (4 top+bottom, 12 top+bottom+outerwear)", "Nothing like it in your closet yet", "$2.19 per new outfit (within your $10 limit)"]}`.

**Unsupported categories (shoes, accessory):** HTTP 200 with `"supported": false`, a friendly `"message"`,
`outfits: []`, `total_new_outfits: 0`, `value.value_score: null` and
`verdict: {"decision": "UNSUPPORTED", "reasons": [message]}`. Redundancy is still computed.

Templates per category: top/bottom → `top+bottom`, `top+bottom+outerwear`; outerwear → `top+bottom+outerwear`,
`dress+outerwear`; dress → `dress` (the dress itself = 1 outfit unless near-duplicate, score 1.0), `dress+outerwear`.

## POST /api/candidate/{item_id}/add-to-closet
"I bought it" → `Item` with `status: "closet"` (records `purchased_at` / `purchase_price`, which count against the monthly budget).

## GET /api/settings  ·  PUT /api/settings
PUT takes any subset; returns the full settings object.
```json
{
  "compat_threshold": 0.5,                 // min OutfitTransformer score for an outfit to count
  "redundancy_similar_threshold": 0.82,
  "redundancy_duplicate_threshold": 0.90,
  "redundancy_text_weight": 0.3,           // extra: similarity = 0.7*image cosine + 0.3*attribute-text cosine
  "min_new_outfits": 3,
  "max_cost_per_outfit": 10.0,
  "monthly_budget": 200.0,                 // null = no budget cap
  "style_goal": "",
  "occasions": [],
  "match_gender_presentation": true,       // extra: don't pair mens-only with womens-only items
  "max_pairs_for_layering": 40             // extra: cap on top+bottom pairs used when evaluating outerwear
}
```

Errors: `404 {"detail": "item ... not found"}`, `422` validation, `500 {"detail": "detection failed: ..."}`.
