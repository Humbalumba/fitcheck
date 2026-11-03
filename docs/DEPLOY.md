# Deploying FitCheck

FitCheck is four containers, described in `docker-compose.yml`:

| service    | image                                 | state                          | port (internal) |
|------------|---------------------------------------|--------------------------------|-----------------|
| `postgres` | postgres:17-alpine                    | volume `pgdata`                | 5432            |
| `minio`    | cgr.dev/chainguard/minio (S3 API)     | volume `miniodata`, private bucket | 9000 (console 9001) |
| `backend`  | built from `backend/Dockerfile` (FastAPI, torch CPU) | volume `backend-data` (cache only), `./backend/models` mounted read-only | 8000 |
| `frontend` | built from `frontend/Dockerfile` (Next.js standalone, Auth.js) | none        | 3000 (the only public port) |

How a request flows: browser → frontend :3000. Next.js answers `/api/auth/*` itself (Auth.js) and proxies every other `/api/*` and `/media/*` call to the backend. The backend checks a short-lived JWT on every `/api` call, scopes every query by `user_id`, and serves photos only through HMAC-signed URLs. Photos are cached on the backend's disk and kept for good in the bucket under `fitcheck-media/<hash(user)>/…`.

The official `minio/minio` and `minio/mc` images are no longer on Docker Hub or Quay, so the compose file uses Chainguard's free MinIO image. The backend creates the bucket on startup (`S3_CREATE_BUCKET=1`), so no `mc` init container is needed. MinIO is never exposed publicly.

## Configuration

Every setting comes from the environment:

- `.env` at the repo root, read by compose. It is never committed; see `.env.example` for the list.
- `backend/.env`, optional, for the Gemini keys (`GEMINI_API_KEY`, …).

```bash
cp .env.example .env               # or let the generator create it:
python3 deploy/init_env.py --public-url https://fitcheck.example.com   # creates any missing secrets (prints none), chmod 600
```

| var | what it is |
|-----|------|
| `PUBLIC_URL` / `AUTH_URL` / `APP_URL` / `ALLOWED_ORIGINS` | the public https origin; `init_env.py --public-url` sets all four |
| `AUTH_SECRET`, `BACKEND_JWT_SECRET`, `INTERNAL_API_SECRET`, `MEDIA_SIGNING_SECRET` | random secrets. Changing them signs everyone out or invalidates photo links. Keep them when you migrate |
| `POSTGRES_USER/PASSWORD/DB` | database credentials; the backend's `DATABASE_URL` is derived from them in compose |
| `MINIO_ROOT_USER/PASSWORD`, `S3_BUCKET` | object storage (the backend uses the same pair as its S3 keys) |
| `MEDIA_STORE` | `s3` (compose default), `local` (disk only) or `blob` (legacy Vercel) |
| `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_FORCE_PATH_STYLE` | any S3-compatible store (MinIO, AWS S3, R2, GCS interop, B2) |
| `MEDIA_S3_PRESIGN=1` | optional: `/media` redirects to a 5-minute S3 presigned URL instead of streaming through the backend. Use it only when the bucket endpoint is reachable from browsers, i.e. not with the internal MinIO |
| `AUTH_GOOGLE_ID/SECRET` | Google sign-in (the button appears only when set). Redirect URI: `<PUBLIC_URL>/api/auth/callback/google` |
| `RESEND_API_KEY`, `EMAIL_FROM` | verification and password-reset emails. Without them, emails are only logged |
| `AUTH_REQUIRE_VERIFIED_EMAIL` | `0` until Resend is configured, then `1` |
| `FITCHECK_WARMUP`, `FITCHECK_WARDROBE_WARM` | `0` skips model warmup or the background "worth a look" store search |

`BACKEND_URL` (where Next.js proxies `/api`) is fixed when the frontend image is built (build arg; default `http://backend:8000`). Rebuild the frontend if it changes. `AUTH_URL` is read at runtime.

## Running it (any Docker host)

```bash
# models: copy backend/models/ next to the compose file (≈1.8 GB: fashion-clip, segformer, outfit_transformer)
#         and make them world-readable (chmod -R a+rX backend/models); the backend runs as a non-root user.
python3 deploy/init_env.py --public-url https://your.domain
docker compose up -d --build
docker compose logs -f backend      # "alembic upgrade head" runs on every start (advisory-locked)
```

Put TLS in front of port 3000: Caddy, nginx, a Cloudflare tunnel or the platform's load balancer. Auth cookies are `Secure`, so the public URL must be https. To bake the models into the image instead of mounting them, see the comment in `backend/Dockerfile`.

Local development without Docker still works: `backend/run.sh` falls back to SQLite plus local disk if `DATABASE_URL` is unset, and `npm run dev` in `frontend/`.

## On the Grok Bot box (current setup)

The box has no iptables/NAT, so it uses `docker-compose.box.yml` (host networking, everything on 127.0.0.1) and a Cloudflare quick tunnel:

```bash
deploy/box-up.sh     # starts dockerd if needed, the tunnel, init_env with the tunnel URL, compose up
deploy/box-down.sh   # stop containers (data kept); --tunnel also stops the tunnel
```

Quick-tunnel URLs change every time the tunnel restarts. `box-up.sh` rewrites `AUTH_URL` and the related vars and recreates the web and API containers. For a stable address, use a named Cloudflare tunnel on your own domain.

## Migrating to a hosting service

You need to carry over three things: **the `.env` secrets, the Postgres database and the bucket.** `backend-data` is only a cache: photos are restored from the bucket on demand, and the FAISS indexes are rebuilt from Postgres.

### 1. Database
```bash
# dump from the box (custom format)
docker compose exec -T postgres pg_dump -U fitcheck -d fitcheck -Fc > fitcheck.dump
# restore into the new Postgres (Railway/Render/Neon/RDS/Cloud SQL…)
pg_restore --no-owner --no-acl -d "$NEW_DATABASE_URL" fitcheck.dump
```
Set `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db?sslmode=require` on the backend. Alembic brings the schema up to date on boot.

### 2. Photos
```bash
# MinIO → AWS S3 / R2 / B2 / GCS (S3 interop). Run on the box:
docker run --rm --network host -e MC_HOST_src=http://$MINIO_ROOT_USER:$MINIO_ROOT_PASSWORD@127.0.0.1:9000 \
  -e MC_HOST_dst=https://$KEY:$SECRET@s3.amazonaws.com cgr.dev/chainguard/minio-client mirror src/fitcheck-media dst/<bucket>
# or with the AWS CLI: aws s3 sync s3://fitcheck-media s3://<bucket> --endpoint-url http://127.0.0.1:9000 (source creds), then upload
```
Keep the object keys unchanged (`fitcheck-media/<hash>/…`) and keep `MEDIA_SIGNING_SECRET`, because both the key prefixes and the photo URLs derive from it. Then set `MEDIA_STORE=s3`, `S3_BUCKET`, `S3_REGION` and the keys, leave `S3_ENDPOINT_URL` empty for AWS (or set it for R2/GCS), set `S3_FORCE_PATH_STYLE=0` for AWS, and set `S3_CREATE_BUCKET=0` if the keys can't create buckets. Keep the bucket private.

### 3. Platform notes
- **Plain VM (GCP/AWS/Hetzner):** copy the repo, `.env`, `backend/.env` and models, restore the volumes or use the steps above, run `docker compose up -d`, and put Caddy in front of :3000. This is the simplest option.
- **Railway / Render / Fly:** deploy `backend/` and `frontend/` as two services from their Dockerfiles. Use the platform's managed Postgres and an external S3 bucket (no MinIO). Give the backend at least 2.5 GB RAM, with the models baked into the image or on a persistent disk at `/app/models`. Build the frontend with `--build-arg BACKEND_URL=<internal backend URL>` (Render: `http://<svc>:8000`, Fly: `http://<app>.internal:8000`, Railway: `http://<svc>.railway.internal:8000`). Only the frontend needs a public domain. Set the same env vars as above, and `FORWARDED_ALLOW_IPS=*` on the backend if it sits behind the platform proxy.
- After moving, update the Google OAuth redirect URI and the Resend sending domain to the new public URL.

## Resource footprint (measured on the box)
- RAM: backend ≈1.6 GB with the models loaded; frontend ≈50 MB, Postgres ≈40 MB, MinIO ≈65 MB.
- Disk: backend image 1.8 GB, frontend 236 MB, Postgres 297 MB, MinIO 169 MB, models 1.8 GB, plus data.
