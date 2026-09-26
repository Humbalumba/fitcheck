# FitCheck — frontend

Mobile-first Next.js 16 (App Router, TypeScript) + Tailwind v4 app for FitCheck:
"should I buy this?" based on how many **new outfits** an item unlocks with your closet and whether it's redundant.

## Screens
| Route | What |
|---|---|
| `/closet` | Grid of closet cut-outs, category chips with counts, tap an item → edit attributes / delete |
| `/add` | Onboarding: snap/upload photos of clothes laid flat. Photos are queued and run through `POST /api/detect` (purpose=closet) one at a time; boxes overlaid on the photo, editable item cards with include checkboxes, "Add N items to closet" |
| `/buy` | Snap one item → detect (purpose=candidate) → pick which box if several → price (pre-filled from tag, required otherwise) → `POST /api/evaluate` → BUY/SKIP verdict, new-outfit count, cost/outfit, value score, redundancy card, outfits grouped by template, "I bought it — add to closet" |
| `/settings` | Budget, style goal, occasions + threshold sliders (`GET/PUT /api/settings`) |

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
Fixture closet of 15 items (SVG garments), fake detection (2–4 items per photo), and a local evaluator that respects the settings thresholds.

## Contract notes (reconciled with ../API.md)
- Items are displayed with `image_url` (cut-out on white), falling back to `cutout_url` (transparent PNG).
- `value.value_score` is 0–100; `value.budget_remaining` shown under the stats when present.
- `verdict.decision` can be `BUY`, `SKIP` or `UNSUPPORTED` (shoes/accessories: `supported:false` + `message`) — shown as a neutral "Can't score outfits" card.
- Outfit templates are rendered generically (`top+bottom+shoes` → "Top + Bottom + Shoes"); shoes/outerwear/accessories go in the side column of the outfit card.
- Settings: `compat_threshold` is labelled **Match strictness** (0–1, 0.5 = balanced; backend calibrates per outfit size). `match_gender_presentation` gets a toggle when present.
- Attribute edits on a candidate are PATCHed before `POST /api/evaluate` (PATCH works for any item status).

## Code map
- `src/lib/api.ts` — typed API client (real/mock switch, `mediaUrl()` for `/media/...` paths)
- `src/lib/mock.ts` — mock backend
- `src/lib/types.ts` — API contract types
- `src/components/` — `PhotoWithBoxes` (bbox overlay, `[ymin,xmin,ymax,xmax]` 0–1000), `AttributeEditor`, `OutfitCard`, `FilePicker`, UI primitives
- `src/app/{closet,add,buy,settings}/page.tsx` — screens

## Screenshots
Generated with Playwright (390×844) into `/workspace/fitcheck/screenshots/`.
