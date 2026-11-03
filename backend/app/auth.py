"""Accounts + request authentication for the FastAPI backend.

Who is the user? The Next.js frontend runs Auth.js (email+password and Google). For every API call the browser
gets a short-lived (5 min) HS256 JWT from the frontend (/api/auth/token, minted from the httpOnly Auth.js session)
and sends it as `Authorization: Bearer`. require_user() verifies signature / issuer / audience / expiry with
BACKEND_JWT_SECRET, loads the user and checks the token_version (bumped on password reset / account deletion, which
revokes older sessions). The user id ONLY ever comes from that verified token, never from the request body/query.

The backend owns the users table: password hashing (argon2id via argon2-cffi), lockout, email verification and
password-reset tokens (random, single use, only sha256 stored), and rate limits. Auth.js calls the /api/internal/*
endpoints server-to-server with X-Internal-Secret (INTERNAL_API_SECRET).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from starlette.concurrency import run_in_threadpool

from . import db, emailer, media_store
from . import models as m
from .ratelimit import RateLimited, clear as rl_clear, hit

log = logging.getLogger("fitcheck.auth")

JWT_ISSUER = "fitcheck-web"
JWT_AUDIENCE = "fitcheck-api"
LOCK_AFTER = int(os.environ.get("AUTH_LOCK_AFTER", "5"))          # failed passwords in a row
LOCK_MINUTES = int(os.environ.get("AUTH_LOCK_MINUTES", "15"))
VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
MIN_PASSWORD = 10

_ph = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
COMMON = {"password", "password1", "password123", "1234567890", "12345678910", "qwertyuiop", "iloveyou12",
          "letmein123", "fitcheck123", "passw0rd123", "abcdefghij", "0123456789", "qwerty12345", "football12",
          "welcome123", "admin12345", "11111111111", "password12", "monkey12345", "sunshine12"}


def require_verified_email() -> bool:
    return os.environ.get("AUTH_REQUIRE_VERIFIED_EMAIL", "1") != "0"


def _jwt_secret() -> str:
    s = os.environ.get("BACKEND_JWT_SECRET", "")
    if len(s) < 32:
        raise HTTPException(503, "server auth is not configured")
    return s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _norm_email(e: str) -> str:
    return (e or "").strip().lower()


def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def client_ip(request: Request) -> str:
    for h in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def password_problem(pw: str, email: str = "") -> str | None:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(pw) > 128:
        return "Use at most 128 characters."
    if len(set(pw)) < 5 or pw.lower() in COMMON:
        return "That password is too easy to guess."
    local = _norm_email(email).split("@")[0]
    if len(local) >= 4 and local in pw.lower():
        return "Don't put your email in your password."
    return None


def _first_name(name: str | None, email: str) -> str:
    n = (name or "").strip().split()
    return (n[0] if n else email.split("@")[0])[:100]


def _user_public(u) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "first_name": u["first_name"],
            "email_verified": u["email_verified_at"] is not None, "ver": u["token_version"]}


def _get_user(**where):
    col, val = next(iter(where.items()))
    with db.tx() as c:
        return c.execute(select(m.users).where(getattr(m.users.c, col) == val)).mappings().first()


def _issue_token(c, uid: str, purpose: str, ttl: timedelta) -> str:
    raw = secrets.token_urlsafe(32)
    c.execute(delete(m.auth_tokens).where(m.auth_tokens.c.user_id == uid, m.auth_tokens.c.purpose == purpose))
    c.execute(m.auth_tokens.insert().values(token_hash=_hash_token(raw), user_id=uid, purpose=purpose,
                                            expires_at=_now() + ttl))
    return raw


def _consume_token(c, raw: str, purpose: str) -> str | None:
    t = m.auth_tokens.c
    row = c.execute(select(t.user_id, t.expires_at, t.used_at).where(
        t.token_hash == _hash_token(raw or ""), t.purpose == purpose).with_for_update()).first()
    if not row or row.used_at is not None or _aware(row.expires_at) < _now():
        return None
    c.execute(update(m.auth_tokens).where(t.token_hash == _hash_token(raw)).values(used_at=_now()))
    return row.user_id


def _limit(key: str, limit: int, window_s: int) -> None:
    try:
        hit(key, limit, window_s)
    except RateLimited as e:
        raise HTTPException(429, "Too many attempts. Please wait a bit and try again.",
                            headers={"Retry-After": str(e.retry_after)})


# ------------------------------------------------------------------ request authentication
_user_cache: dict[str, tuple[float, int]] = {}   # uid -> (checked_at, token_version)
USER_CACHE_S = 20


def _load_ver(uid: str) -> int | None:
    hitc = _user_cache.get(uid)
    if hitc and time.time() - hitc[0] < USER_CACHE_S:
        return hitc[1]
    with db.tx() as c:
        v = c.execute(select(m.users.c.token_version).where(m.users.c.id == uid)).scalar()
    if v is None:
        _user_cache.pop(uid, None)
        return None
    _user_cache[uid] = (time.time(), v)
    return v


def forget_user_cache(uid: str) -> None:
    _user_cache.pop(uid, None)


def verify_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(authorization[7:].strip(), _jwt_secret(), algorithms=["HS256"], audience=JWT_AUDIENCE,
                            issuer=JWT_ISSUER, leeway=10, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please sign in again", headers={"WWW-Authenticate": "Bearer"})
    if claims.get("exp", 0) - claims.get("iat", 0) > 3600:
        raise HTTPException(401, "token lifetime too long")
    uid = str(claims["sub"])
    ver = _load_ver(uid)
    if ver is None or int(claims.get("ver", -1)) != ver:
        raise HTTPException(401, "Session expired, please sign in again", headers={"WWW-Authenticate": "Bearer"})
    return uid


async def require_user(request: Request, authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency for every user-data route. Sets the per-request user context (app/db.py)."""
    uid = await run_in_threadpool(verify_bearer, authorization)
    db.set_user(uid)                 # async dependency -> same context as the (threadpooled) endpoint
    request.state.user_id = uid
    if media_store.enabled():
        await run_in_threadpool(media_store.restore_user, uid)
    return uid


def require_internal(x_internal_secret: str | None = Header(default=None)) -> None:
    want = os.environ.get("INTERNAL_API_SECRET", "")
    if len(want) < 32 or not x_internal_secret or not hmac.compare_digest(want, x_internal_secret):
        raise HTTPException(403, "forbidden")


def require_json(request: Request) -> None:
    """CSRF guard for cookie-less public auth endpoints: only JSON bodies (cross-site forms can't send these
    without a CORS preflight, which our CORS policy refuses)."""
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(415, "expected application/json")


# ------------------------------------------------------------------ routes
public = APIRouter(prefix="/api/account", tags=["auth"], dependencies=[Depends(require_json)])
internal = APIRouter(prefix="/api/internal/auth", tags=["internal"], dependencies=[Depends(require_internal)])
account = APIRouter(prefix="/api", tags=["account"], dependencies=[Depends(require_user)])


class SignupBody(BaseModel):
    email: str
    password: str
    name: str = ""


class EmailBody(BaseModel):
    email: str


class TokenBody(BaseModel):
    token: str


class ResetBody(BaseModel):
    token: str
    password: str


class CredentialsBody(BaseModel):
    email: str
    password: str
    ip: str = ""


class OAuthBody(BaseModel):
    provider: str
    provider_account_id: str
    email: str
    email_verified: bool = False
    name: str = ""
    given_name: str = ""


class DeleteBody(BaseModel):
    confirm: str
    password: str = ""


GENERIC_SIGNUP = {"ok": True, "message": "Check your email to confirm your account."}


@public.post("/signup")
def signup(body: SignupBody, request: Request):
    email = _norm_email(body.email)
    ip = client_ip(request)
    _limit(f"signup:ip:{ip}", 10, 3600)
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise HTTPException(422, "Enter a valid email address.")
    prob = password_problem(body.password, email)
    if prob:
        raise HTTPException(422, prob)
    _limit(f"signup:email:{email}", 3, 3600)
    name = (body.name or "").strip()[:200]
    existing = _get_user(email=email)
    if existing:  # don't reveal that the account exists
        _ph.hash(body.password)  # similar timing
        emailer.send_already_registered(email)
        return {**GENERIC_SIGNUP, "verification_required": require_verified_email()}
    uid = "u_" + uuid.uuid4().hex
    with db.tx() as c:
        c.execute(m.users.insert().values(id=uid, email=email, name=name or None, first_name=_first_name(name, email),
                                          password_hash=_ph.hash(body.password)))
        tok = _issue_token(c, uid, "verify_email", VERIFY_TTL)
    emailer.send_verification(email, tok)
    return {**GENERIC_SIGNUP, "verification_required": require_verified_email(), "email_sent": emailer.configured()}


@public.post("/verify-email")
def verify_email(body: TokenBody, request: Request):
    _limit(f"verify:ip:{client_ip(request)}", 30, 3600)
    with db.tx() as c:
        uid = _consume_token(c, body.token, "verify_email")
        if not uid:
            raise HTTPException(400, "This link is invalid or has expired. Request a new one from the sign-in page.")
        c.execute(update(m.users).where(m.users.c.id == uid).values(email_verified_at=_now(), updated_at=_now()))
    return {"ok": True}


@public.post("/resend-verification")
def resend_verification(body: EmailBody, request: Request):
    email = _norm_email(body.email)
    _limit(f"resend:ip:{client_ip(request)}", 10, 3600)
    _limit(f"resend:email:{email}", 3, 3600)
    u = _get_user(email=email)
    if u and u["email_verified_at"] is None:
        with db.tx() as c:
            tok = _issue_token(c, u["id"], "verify_email", VERIFY_TTL)
        emailer.send_verification(email, tok)
    return {"ok": True, "message": "If that account needs confirming, we've sent a new link."}


@public.post("/forgot-password")
def forgot_password(body: EmailBody, request: Request):
    email = _norm_email(body.email)
    _limit(f"forgot:ip:{client_ip(request)}", 10, 3600)
    _limit(f"forgot:email:{email}", 3, 3600)
    u = _get_user(email=email)
    if u:
        with db.tx() as c:
            tok = _issue_token(c, u["id"], "reset_password", RESET_TTL)
        emailer.send_reset(email, tok)
    return {"ok": True, "message": "If an account exists for that email, we've sent a reset link."}


@public.post("/reset-password")
def reset_password(body: ResetBody, request: Request):
    _limit(f"reset:ip:{client_ip(request)}", 20, 3600)
    with db.tx() as c:
        uid = _consume_token(c, body.token, "reset_password")
        if not uid:
            raise HTTPException(400, "This reset link is invalid or has expired. Request a new one.")
        email = c.execute(select(m.users.c.email).where(m.users.c.id == uid)).scalar()
        prob = password_problem(body.password, email or "")
        if prob:  # roll back so the token can be used again with a better password
            raise HTTPException(422, prob)
        ver = c.execute(select(m.users.c.token_version).where(m.users.c.id == uid)).scalar() or 0
        c.execute(update(m.users).where(m.users.c.id == uid).values(
            password_hash=_ph.hash(body.password), token_version=ver + 1, failed_logins=0, locked_until=None,
            email_verified_at=func.coalesce(m.users.c.email_verified_at, _now()),  # they proved the inbox
            updated_at=_now()))
    forget_user_cache(uid)
    rl_clear(f"login:email:{email}")
    return {"ok": True}


# ---- internal (Auth.js server -> backend)
@internal.post("/credentials")
def check_credentials(body: CredentialsBody):
    email = _norm_email(body.email)
    try:
        if body.ip:
            hit(f"login:ip:{body.ip}", 30, 900)
        hit(f"login:email:{email}", 20, 900)
    except RateLimited:
        return {"ok": False, "error": "rate_limited"}
    u = _get_user(email=email)
    if not u or not u["password_hash"]:
        try:
            _ph.verify(_DUMMY_HASH, body.password or "x")
        except Exception:
            pass
        return {"ok": False, "error": "invalid"}
    if u["locked_until"] and _aware(u["locked_until"]) > _now():
        return {"ok": False, "error": "locked"}
    try:
        _ph.verify(u["password_hash"], body.password or "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        n = (u["failed_logins"] or 0) + 1
        vals = {"failed_logins": n}
        if n >= LOCK_AFTER:
            vals.update(locked_until=_now() + timedelta(minutes=LOCK_MINUTES), failed_logins=0)
        with db.tx() as c:
            c.execute(update(m.users).where(m.users.c.id == u["id"]).values(**vals))
        return {"ok": False, "error": "locked" if n >= LOCK_AFTER else "invalid"}
    if require_verified_email() and u["email_verified_at"] is None:
        return {"ok": False, "error": "unverified"}
    vals = {"failed_logins": 0, "locked_until": None}
    if _ph.check_needs_rehash(u["password_hash"]):
        vals["password_hash"] = _ph.hash(body.password)
    with db.tx() as c:
        c.execute(update(m.users).where(m.users.c.id == u["id"]).values(**vals))
    return {"ok": True, "user": _user_public(u)}


@internal.post("/oauth")
def oauth_login(body: OAuthBody):
    """Google sign-in: find the linked account, else link by VERIFIED email, else create the user."""
    email = _norm_email(body.email)
    if body.provider not in ("google",) or not body.provider_account_id or not EMAIL_RE.match(email):
        raise HTTPException(422, "bad oauth profile")
    oa = m.oauth_accounts.c
    with db.tx() as c:
        uid = c.execute(select(oa.user_id).where(oa.provider == body.provider,
                                                 oa.provider_account_id == body.provider_account_id)).scalar()
        if not uid:
            if not body.email_verified:
                raise HTTPException(403, "Google account email is not verified")
            u = c.execute(select(m.users).where(m.users.c.email == email)).mappings().first()
            if u:
                uid = u["id"]
                vals = {"updated_at": _now()}
                if u["email_verified_at"] is None:
                    # an unverified email+password signup could have been made by someone else: drop that password
                    # and revoke its sessions before the real owner (proven by Google) takes the account over
                    vals.update(password_hash=None, email_verified_at=_now(), token_version=u["token_version"] + 1)
                c.execute(update(m.users).where(m.users.c.id == uid).values(**vals))
            else:
                uid = "u_" + uuid.uuid4().hex
                name = (body.name or "").strip()[:200]
                c.execute(m.users.insert().values(id=uid, email=email, name=name or None,
                                                  first_name=(body.given_name or _first_name(name, email))[:100],
                                                  email_verified_at=_now()))
            c.execute(m.oauth_accounts.insert().values(provider=body.provider,
                                                       provider_account_id=body.provider_account_id, user_id=uid))
        u = c.execute(select(m.users).where(m.users.c.id == uid)).mappings().first()
    forget_user_cache(uid)
    return {"ok": True, "user": _user_public(u)}


# ---- signed-in user
@account.get("/me")
def me():
    uid = db.current_user_id()
    u = _get_user(id=uid)
    if not u:
        raise HTTPException(401, "Sign in required")
    with db.tx() as c:
        providers = c.execute(select(m.oauth_accounts.c.provider).where(m.oauth_accounts.c.user_id == uid)).scalars().all()
    out = _user_public(u)
    out.pop("ver", None)
    return {**out, "has_password": bool(u["password_hash"]), "providers": sorted(set(providers))}


@account.delete("/account")
def delete_account(body: DeleteBody):
    """Permanently delete the signed-in user: all rows (closet, evaluations, caches, settings) + all media."""
    uid = db.current_user_id()
    if body.confirm != "DELETE":
        raise HTTPException(422, 'Type DELETE to confirm.')
    u = _get_user(id=uid)
    if not u:
        raise HTTPException(401, "Sign in required")
    if u["password_hash"]:
        _limit(f"delete:user:{uid}", 5, 900)
        try:
            _ph.verify(u["password_hash"], body.password or "")
        except Exception:
            raise HTTPException(403, "That password isn't right.")
    from .vectors import closet_index
    try:
        media_store.delete_user(uid)
    except Exception:
        log.exception("media delete failed; continuing with the account rows")
        raise HTTPException(503, "Couldn't delete your photos right now; nothing was deleted. Try again.")
    db.delete_all_user_data(uid)
    with db.tx() as c:
        c.execute(delete(m.users).where(m.users.c.id == uid))
    forget_user_cache(uid)
    closet_index.forget_user(uid)
    return {"ok": True}
