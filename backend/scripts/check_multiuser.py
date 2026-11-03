"""Targeted multi-user / auth check against a real Postgres (no ML models, no Gemini).

  DATABASE_URL=postgresql://... python scripts/check_multiuser.py
Creates two users through the real sign-up / verify / login endpoints, gives each a closet item, and asserts that
neither can read, change, delete, render, evaluate or see the media of the other's data; then exercises lockout,
password reset (revokes old tokens), Google linking and account deletion. Uses a throwaway data dir.
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("FITCHECK_DATA_DIR", tempfile.mkdtemp(prefix="fc_mu_"))
os.environ.update(BACKEND_JWT_SECRET="t" * 48, INTERNAL_API_SECRET="i" * 48, FITCHECK_WARMUP="0",
                  FITCHECK_WARDROBE_WARM="0", RENDER_ENABLED="0", FITCHECK_GEMINI_OFF="1", FITCHECK_DEV_EMAIL_LOG="1",
                  FITCHECK_PRICE_BACKFILL="0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import types  # noqa: E402

if "transformers" not in sys.modules:  # no torch on this box: stub the model loader module
    try:
        import transformers  # noqa: F401
    except ImportError:
        stub = types.ModuleType("app.models_runtime")
        stub.get_segformer = stub.get_fashion_clip = lambda: (_ for _ in ()).throw(RuntimeError("no models"))
        import threading
        stub.LOAD_LOCK = threading.Lock()
        sys.modules["app.models_runtime"] = stub

import jwt  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, db, emailer, pipeline, vectors  # noqa: E402
from app.main import app  # noqa: E402

rng = np.random.default_rng(0)
vectors.embed_image_path = lambda p: (lambda v: v / np.linalg.norm(v))(rng.normal(size=512).astype(np.float32))
pipeline.ensure_compat_embeddings = lambda items: None
I = {"X-Internal-Secret": "i" * 48}
ok = 0


def check(cond, msg):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    ok += 1
    print("  ok -", msg)


def token(user, ver=None, secret="t" * 48, ttl=300):
    now = int(time.time())
    return jwt.encode({"sub": user["id"], "ver": user["ver"] if ver is None else ver, "iat": now, "exp": now + ttl,
                       "iss": "fitcheck-web", "aud": "fitcheck-api"}, secret, algorithm="HS256")


def H(user, **kw):
    return {"Authorization": f"Bearer {token(user, **kw)}"}


def last_link(kind):
    m = re.search(rf"/{kind}\?token=([\w-]+)", emailer.outbox[-1]["text"])
    return m.group(1)


with TestClient(app) as c:
    from sqlalchemy import delete as _del
    from app import models as _m
    with db.tx() as _c:  # scratch DB: start with fresh rate-limit windows
        _c.execute(_del(_m.rate_events))
    sfx = os.urandom(3).hex()
    users = {}
    print("sign-up / verification / login")
    r = c.post("/api/account/signup", data={"email": "x@y.co", "password": "x"})
    check(r.status_code == 415, "form-encoded (CSRF-style) POST refused")
    r = c.post("/api/account/signup", json={"email": f"a{sfx}@ex.com", "password": "short"})
    check(r.status_code == 422, "weak password refused")
    for n, name in (("a", "Alice Smith"), ("b", "Bob")):
        email = f"{n}{sfx}@ex.com"
        r = c.post("/api/account/signup", json={"email": email, "password": f"correct horse {n} battery", "name": name})
        check(r.status_code == 200, f"signup {n}")
        vt = last_link("verify-email")
        r = c.post("/api/internal/auth/credentials", headers=I, json={"email": email, "password": f"correct horse {n} battery"})
        check(r.json() == {"ok": False, "error": "unverified"}, f"{n} can't sign in before verifying email")
        check(c.post("/api/account/verify-email", json={"token": vt}).status_code == 200, f"verify {n}")
        check(c.post("/api/account/verify-email", json={"token": vt}).status_code == 400, "verification link is single-use")
        r = c.post("/api/internal/auth/credentials", headers=I, json={"email": email.upper(), "password": f"correct horse {n} battery"})
        check(r.json()["ok"], f"{n} signs in (email case-insensitive)")
        users[n] = r.json()["user"]
    A, B = users["a"], users["b"]
    r = c.post("/api/account/signup", json={"email": f"a{sfx}@ex.com", "password": "another good pass 1"})
    check(r.status_code == 200 and "already" in emailer.outbox[-1]["subject"], "duplicate signup: generic reply, no enumeration")
    check(c.post("/api/internal/auth/credentials", json={"email": "a", "password": "b"}).status_code == 403,
          "internal endpoint needs the shared secret")

    print("request authentication")
    check(c.get("/api/closet/items").status_code == 401, "no token -> 401")
    check(c.get("/api/closet/items", headers=H(A, secret="z" * 48)).status_code == 401, "forged signature -> 401")
    check(c.get("/api/closet/items", headers=H(A, ttl=-60)).status_code == 401, "expired token -> 401")
    check(c.get("/api/closet/items", headers=H(A, ver=A["ver"] + 1)).status_code == 401, "stale token version -> 401")
    check(c.get("/api/closet/items?user_id=" + B["id"], headers=H(A)).json()["items"] == [], "client-supplied user id ignored")
    me = c.get("/api/me", headers=H(A)).json()
    check(me["first_name"] == "Alice" and me["email"] == f"a{sfx}@ex.com", "/api/me greets the right user")

    print("data isolation")
    ids = {}
    for n, u in users.items():
        with db.as_user(u["id"]):
            d = config.user_media_dir("white")
            pth = d / f"{n}.jpg"
            Image.new("RGB", (64, 64), (200, 10, 10)).save(pth)
            pid = db.insert_photo(str(pth), "closet", 64, 64)
            ids[n] = db.insert_item(status="detected", category="top", photo_id=pid, white_path=pth, crop_path=pth,
                                    cutout_path=pth, attributes={"category": "top", "subcategory": "t-shirt",
                                                                 "primary_color": "red", "price": 20})
        r = c.post("/api/closet/items", headers=H(u), json={"item_ids": [ids[n]]})
        check(r.status_code == 200, f"{n} adds own item to closet")
    ia, ib = ids["a"], ids["b"]
    la = c.get("/api/closet/items", headers=H(A)).json()["items"]
    lb = c.get("/api/closet/items", headers=H(B)).json()["items"]
    check([i["id"] for i in la] == [ia] and [i["id"] for i in lb] == [ib], "each user lists only their own closet")
    check(c.post("/api/closet/items", headers=H(B), json={"item_ids": [ia]}).status_code == 404, "B can't add A's item")
    check(c.get(f"/api/items/{ia}", headers=H(B)).status_code == 404, "B can't read A's item")
    check(c.patch(f"/api/closet/items/{ia}", headers=H(B), json={"attributes": {"brand": "x"}}).status_code == 404, "B can't edit A's item")
    check(c.delete(f"/api/closet/items/{ia}", headers=H(B)).status_code == 404, "B can't delete A's item")
    check(c.post(f"/api/items/{ia}/render", headers=H(B), json={}).status_code == 404, "B can't render A's item")
    check(c.post("/api/evaluate", headers=H(B), json={"item_id": ia}).status_code == 404, "B can't evaluate A's item")
    check(c.get(f"/api/items/{ia}/sustainability", headers=H(B)).status_code == 404, "B can't read A's sustainability")
    with db.as_user(A["id"]):
        eid = db.save_evaluation(ia, 20, "BUY", {"verdict": {"decision": "BUY"}}, [{"template": "t", "score": 1, "item_ids": [ia]}])
        check([i for i, _ in vectors.closet_index.search(np.ones(512, np.float32))] == [ia], "A's look-alike index has only A's items")
    with db.as_user(B["id"]):
        check([i for i, _ in vectors.closet_index.search(np.ones(512, np.float32))] == [ib], "B's look-alike index has only B's items")
    check(c.get(f"/api/evaluations/{eid}", headers=H(A)).status_code == 200, "A reads own evaluation")
    check(c.get(f"/api/evaluations/{eid}", headers=H(B)).status_code == 404, "B can't read A's evaluation")
    check(c.get(f"/api/evaluations/{eid}/suggestions", headers=H(B)).status_code == 404, "B can't read A's suggestions")
    c.put("/api/settings", headers=H(A), json={"style_goal": "minimal"})
    check(c.get("/api/settings", headers=H(B)).json()["style_goal"] == "", "settings are per user")
    check(c.get("/api/settings", headers=H(A)).json()["style_goal"] == "minimal", "A's settings saved")
    r = c.patch(f"/api/closet/items/{ia}", headers=H(A), json={"attributes": {"brand": "Acme"}})
    check(r.status_code == 200 and r.json()["attributes"]["brand"] == "Acme", "A edits own item")

    print("media")
    url = la[0]["image_url"]
    check(url.startswith(f"/media/u/{A['id']}/") and "sig=" in url, "media URL is per-user and signed")
    check(c.get(url).status_code == 200, "signed URL loads")
    check(c.get(url.replace("sig=", "sig=0")).status_code == 404, "tampered signature -> 404")
    check(c.get(url.split("?")[0]).status_code == 404, "unsigned path -> 404")
    check(c.get(url.replace(A["id"], B["id"]).replace("/a.jpg", "/b.jpg")).status_code == 404, "can't reuse a signature for B's file")

    print("lockout + password reset")
    for i in range(5):
        r = c.post("/api/internal/auth/credentials", headers=I, json={"email": f"a{sfx}@ex.com", "password": "wrong password!"})
    check(r.json()["error"] == "locked", "5 wrong passwords lock the account")
    r = c.post("/api/internal/auth/credentials", headers=I, json={"email": f"a{sfx}@ex.com", "password": "correct horse a battery"})
    check(r.json()["error"] == "locked", "correct password refused while locked")
    check(c.post("/api/account/forgot-password", json={"email": f"nobody{sfx}@ex.com"}).status_code == 200, "forgot: unknown email gets same reply")
    c.post("/api/account/forgot-password", json={"email": f"a{sfx}@ex.com"})
    rt = last_link("reset-password")
    check(c.post("/api/account/reset-password", json={"token": rt, "password": "short"}).status_code == 422, "reset enforces password rules")
    check(c.post("/api/account/reset-password", json={"token": rt, "password": "brand new secret 42"}).status_code == 200, "reset works")
    check(c.post("/api/account/reset-password", json={"token": rt, "password": "brand new secret 43"}).status_code == 400, "reset link single-use")
    from app import auth as _auth
    _auth.forget_user_cache(A["id"])
    check(c.get("/api/closet/items", headers=H(A)).status_code == 401, "reset revokes existing sessions")
    r = c.post("/api/internal/auth/credentials", headers=I, json={"email": f"a{sfx}@ex.com", "password": "brand new secret 42"})
    check(r.json()["ok"], "new password works and clears the lock")
    A = r.json()["user"]
    for i in range(4):
        c.post("/api/account/forgot-password", json={"email": f"a{sfx}@ex.com"})
    check(c.post("/api/account/forgot-password", json={"email": f"a{sfx}@ex.com"}).status_code == 429, "reset requests are rate limited")

    print("google sign-in")
    g = {"provider": "google", "provider_account_id": "g" + sfx, "email": f"g{sfx}@ex.com", "email_verified": True,
         "name": "Gina Lopez", "given_name": "Gina"}
    r1 = c.post("/api/internal/auth/oauth", headers=I, json=g).json()
    r2 = c.post("/api/internal/auth/oauth", headers=I, json=g).json()
    check(r1["user"]["id"] == r2["user"]["id"] and r1["user"]["first_name"] == "Gina", "Google creates one user, then reuses it")
    c.post("/api/account/signup", json={"email": f"h{sfx}@ex.com", "password": "attacker pass 123", "name": "Mallory"})
    r = c.post("/api/internal/auth/oauth", headers=I, json={**g, "provider_account_id": "h" + sfx, "email": f"h{sfx}@ex.com"})
    r = c.post("/api/internal/auth/credentials", headers=I, json={"email": f"h{sfx}@ex.com", "password": "attacker pass 123"})
    check(not r.json()["ok"], "Google takeover of an UNVERIFIED signup drops the unverified password")
    check(c.post("/api/internal/auth/oauth", headers=I, json={**g, "provider_account_id": "z" + sfx, "email": f"z{sfx}@ex.com",
                                                              "email_verified": False}).status_code == 403, "unverified Google email refused")

    print("account deletion")
    bdir = config.MEDIA_DIR / "u" / B["id"]
    check(bdir.exists(), "B has media on disk")
    check(c.request("DELETE", "/api/account", headers=H(B), json={"confirm": "DELETE", "password": "nope"}).status_code == 403, "delete needs password")
    check(c.request("DELETE", "/api/account", headers=H(B), json={"confirm": "DELETE", "password": "correct horse b battery"}).status_code == 200, "B deletes account")
    check(not bdir.exists(), "B's media removed")
    from sqlalchemy import text
    with db.tx() as conn:
        left = sum(conn.execute(text(f"SELECT count(*) FROM {t.name} WHERE user_id=:u"), {"u": B["id"]}).scalar() for t in __import__("app.models", fromlist=["x"]).USER_DATA_TABLES)
        users_left = conn.execute(text("SELECT count(*) FROM users WHERE id=:u"), {"u": B["id"]}).scalar()
    check(left == 0 and users_left == 0, "B's rows are all gone")
    _auth.forget_user_cache(B["id"])
    check(c.get("/api/closet/items", headers=H(B)).status_code == 401, "deleted user's token rejected")
    check(len(c.get("/api/closet/items", headers=H(A)).json()["items"]) == 1, "A's closet untouched")
print(f"\nALL {ok} CHECKS PASSED")
