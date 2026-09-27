# FitCheck — frontend

Mobile-first Next.js 16 (App Router, TypeScript) + Tailwind v4 app for FitCheck:
"should I buy this?" based on how many **new outfits** an item unlocks with your closet and whether it's redundant.

## Screens
| Route | What |
|---|---|
| `/closet` | Grid of closet cut-outs, category chips with counts, tap an item → edit attributes / delete; items bought via "Should I buy?" also show their sustainability grade (`GET /api/items/{id}/sustainability`) |
| `/add` | Onboarding: snap/upload photos of clothes laid flat. Photos are queued and run through `POST /api/detect` (purpose=closet) one at a time; boxes overlaid on the photo, editable item cards with include checkboxes, "Add N items to closet" |
| `/buy` | Snap one item (or **paste a product link** → `POST /api/detect-url`, which scrapes the page photo + title/brand/price and pre-fills the price) → detect (purpose=candidate) → pick which box if several → price (pre-filled from tag, required otherwise) → `POST /api/evaluate` → BUY / CONSIDER / SKIP verdict card (green / amber / red) with the 0-100 score, a SKIP-CONSIDER-BUY band bar, the 2 main reasons and a sub-score line; headline stats (new outfits "of M a <category> could make", cost per wear vs your usual, versatility n/M, price), redundancy card, outfits grouped by template, "I bought it — add to closet". Under the verdict, `SuggestionsSection` calls `POST /api/evaluations/{id}/suggestions` after render: "Better picks instead" (SKIP / CONSIDER) / "Pairs well with this" (BUY) — up to 3 live products with photo, price, reason, verdict chip, "View at <retailer>" link; tapping a card opens its outfits in `OutfitModal`. A compact `SustainabilityCard` sits right under the verdict (score + A-E grade, "~X kg CO₂e over ~N expected wears = Y kg per wear", water, 2 reasons, "Estimate" info toggle; hidden when unsupported) and suggestion cards show a leaf chip with score · grade |
| `/settings` | Style goal, occasions, match strictness + redundancy sliders, toggles, and a "How the verdict works" explainer (`GET/PUT /api/settings`) |

Header pill polls `GET /api/health` every 15 s (Live / Live (limited) = stub scorer or no Gemini / Offline / Mock).

## Run
```bash
npm install
npm run dev            # http://localhost:3000  (use -H 0.0.0.0 to expose on LAN)
npm run build && npm start   # production
```
Backend (FastAPI) expected on `http://localhost:8000`.

### Environment
| Var | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `""` (same origin) | Where the browser sends API calls. Empty → relative `/api/*` and `/media/*`, proxied by Next. Set to `http://localhost:8000` to call the backend directly (backend has permissive CORS). |
| `BACKEND_URL` | `http://localhost:8000` | Target of the Next rewrite proxy for `/api/*` and `/media/*` (server side). |
| `NEXT_PUBLIC_MOCK` | unset | `1` → in-browser fixture backend (no server needed; resets on reload). |

`NEXT_PUBLIC_*` vars are inlined at build/dev start, so restart after changing them.

### Demo from a phone (single public URL)
Because the client uses same-origin relative URLs and Next proxies `/api` + `/media` to the backend,
only port 3000 needs to be public:
```bash
cloudflared tunnel --url http://localhost:3000     # or: ngrok http 3000
```
Common tunnel domains are already in `allowedDevOrigins` (next.config.ts). Camera capture uses
`<input type=file accept=image/* capture=environment>`; photos are downscaled client-side to ≤1800px JPEG before upload.
Proxy timeout is raised to 180 s for slow detection.

### Mock mode
```bash
NEXT_PUBLIC_MOCK=1 npm run dev
```
Fixture closet of 15 items (SVG garments), fake detection (2–4 items per photo), and a local evaluator that mirrors the backend's 0-100 verdict (simplified).

## Contract notes (reconciled with ../API.md)
- Items are displayed with `image_url` (cut-out on white), falling back to `cutout_url` (transparent PNG).
- Price is optional on /buy. With no price the backend uses an estimate (`value.price_source == "estimated"`): the result page shows "est. ~$60" + "Estimated from brand and type — enter the price for a more accurate verdict" and an inline price box that re-runs `POST /api/evaluate` with the typed price (a user price always wins). The closet item sheet shows `attributes.estimated_price_usd` as "est. ~$X" and the price field stays editable.
- `verdict.score` (= `value.value_score`) is 0–100; `verdict.components` holds the sub-scores; `value.cost_per_wear` / `value.price_bar` / `value.versatility` feed the headline stats.
- `verdict.decision` can be `BUY`, `CONSIDER`, `SKIP` or `UNSUPPORTED` (shoes/accessories: `supported:false` + `message`) — shown as a neutral "Can't score outfits" card.
- Outfit templates are rendered generically (`top+bottom+shoes` → "Top + Bottom + Shoes"); shoes/outerwear/accessories go in the side column of the outfit card.
- Settings: `compat_threshold` is labelled **Match strictness** (0–1, 0.5 = balanced; backend calibrates per outfit size). `match_gender_presentation` gets a toggle when present.
- Attribute edits on a candidate are PATCHed before `POST /api/evaluate` (PATCH works for any item status).

## Code map
- `src/lib/api.ts` — typed API client (real/mock switch, `mediaUrl()` for `/media/...` paths)
- `src/lib/mock.ts` — mock backend
- `src/lib/types.ts` — API contract types
- `src/components/` — `PhotoWithBoxes` (bbox overlay, `[ymin,xmin,ymax,xmax]` 0–1000), `AttributeEditor`, `OutfitCard`, `OutfitModal`, `Suggestions` (shopping picks), `Sustainability` (card, leaf chip, closet-sheet row), `FilePicker`, UI primitives
- `src/app/{closet,add,buy,settings}/page.tsx` — screens

## Screenshots
Generated with Playwright (390×844) into `/workspace/fitcheck/screenshots/`.
