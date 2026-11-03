# Accounts, auth and data isolation

## How it works
- **Sign-in (frontend, Auth.js v5)**: `frontend/src/auth.ts`. Providers: email + password (Credentials) and Google
  (only when `AUTH_GOOGLE_ID`/`AUTH_GOOGLE_SECRET` are set). Session = Auth.js encrypted JWT in an httpOnly, Secure
  (on https), SameSite=Lax cookie, 30 days. Auth.js's CSRF token protects sign-in/sign-out.
- **Passwords (backend)**: `backend/app/auth.py`, argon2id (argon2-cffi). Min 10 chars + common-password and
  "contains your email" checks. 5 wrong passwords lock the account for 15 min; per-IP and per-email rate limits on
  login, sign-up, verification resend, forgot/reset (stored in Postgres, shared by all instances).
- **Email** (`backend/app/emailer.py`, Resend): verification link (24 h) and reset link (1 h); random tokens, only
  the sha256 is stored, single use. Sign-up and "forgot password" never reveal whether an email exists.
  Unverified email+password accounts can't sign in (`AUTH_REQUIRE_VERIFIED_EMAIL=0` turns that off).
- **Google**: linked by Google account id; a Google login with a verified email joins an existing account with that
  email. If that account's email was never verified, its password is wiped first (blocks pre-registration takeover).
- **Every API call**: the browser gets a 5-minute HS256 token from `/api/auth/token` (minted from the session) and sends
  `Authorization: Bearer`. FastAPI (`require_user`) checks signature/issuer/audience/expiry and the user's
  `token_version` (bumped by password reset and account deletion = all sessions revoked). The user id only comes
  from that token. Public routes: `/api/health`, `/api/account/*` (sign-up/verify/forgot/reset; JSON-only),
  `/api/internal/auth/*` (Auth.js server -> backend, `X-Internal-Secret`), `/media/*` (signed URLs only).
- **Data**: Postgres via SQLAlchemy + Alembic (`backend/app/models.py`, `backend/migrations`). Every user table has
  `user_id` (FK -> users, ON DELETE CASCADE) and every query in `backend/app/db.py` filters by the request's user
  (a ContextVar; background renders / searches copy it). No user context = error, never "all rows".
- **Look-alike index**: in-memory FAISS per user, rebuilt from the embeddings stored in Postgres.
- **Media**: `media/u/<user_id>/...`, served only via HMAC-signed, expiring URLs; on Vercel mirrored to Blob under
  `fitcheck-media/<hmac(user)>/...` (private store recommended: `FITCHECK_MEDIA_BLOB_TOKEN`).
- **Account deletion** (Profile -> Account): password + typing DELETE; removes media (disk + Blob), all rows, the user.
- **Pages**: `frontend/src/proxy.ts` sends signed-out visitors to `/login` (auth pages are the only public pages).

## Environment variables
| Name | Where | Notes |
|---|---|---|
| `DATABASE_URL` | backend | Postgres (Neon integration sets it). Unset = local SQLite file (dev only). |
| `BACKEND_JWT_SECRET` | backend + both frontends | same value everywhere, >= 32 chars |
| `INTERNAL_API_SECRET` | backend + both frontends | same value everywhere, >= 32 chars |
| `MEDIA_SIGNING_SECRET` | backend | signs media URLs |
| `AUTH_SECRET` | frontends | Auth.js cookie encryption |
| `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | frontends | optional; Google button appears when set |
| `BACKEND_URL` | frontends | server-to-server URL of the backend |
| `APP_URL` | backend | public site used in email links |
| `ALLOWED_ORIGINS` | backend | comma list for CORS |
| `RESEND_API_KEY`, `EMAIL_FROM` | backend | without them no email is sent (sign-up can't be confirmed) |
| `FITCHECK_MEDIA_BLOB_TOKEN` | backend | token of a PRIVATE Blob store for media (else falls back to `BLOB_READ_WRITE_TOKEN`, public) |

`/workspace/tools/set_auth_env.sh` generates and sets the shared secrets on both Vercel projects (no deploy).

## Local
```
cd backend && DATABASE_URL=postgresql://... alembic upgrade head   # (also runs on startup)
DATABASE_URL=postgresql://... python scripts/check_multiuser.py     # 62 isolation / auth checks
python scripts/migrate_sqlite_to_postgres.py --sqlite data/fitcheck.db --media data/media --email you@x.com
```
