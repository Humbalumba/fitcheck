#!/usr/bin/env python3
"""End-to-end check against a running FitCheck (e.g. through the public tunnel). Uses Gemini for exactly ONE detect
and ONE render. Creates two throwaway accounts and deletes them at the end.
  python3 deploy/e2e_check.py https://<public-url> path/to/product.jpg"""
import os
import re
import secrets
import sys
import time

import requests

BASE = sys.argv[1].rstrip("/")
IMG = sys.argv[2]
PW = "e2e " + secrets.token_urlsafe(12)


def check(cond, msg):
    print(("  ok - " if cond else "  FAIL - ") + msg, flush=True)
    if not cond:
        raise SystemExit(1)


class User:
    def __init__(self, tag):
        self.email = f"e2e-{tag}-{secrets.token_hex(3)}@example.com"
        self.s = requests.Session()
        self._tok = None

    def signup(self, name):
        r = self.s.post(f"{BASE}/api/account/signup", json={"email": self.email, "password": PW, "name": name}, timeout=30)
        check(r.status_code == 200, f"sign up {self.email} ({r.status_code})")

    def signin(self):
        csrf = self.s.get(f"{BASE}/api/auth/csrf", timeout=30).json()["csrfToken"]
        r = self.s.post(f"{BASE}/api/auth/callback/credentials", allow_redirects=False, timeout=30,
                        data={"email": self.email, "password": PW, "csrfToken": csrf})
        loc = r.headers.get("location", "")
        check(r.status_code in (302, 303) and "error" not in loc, f"sign in ({r.status_code} -> {loc[:60]})")
        cookie = next((c for c in self.s.cookies if "session-token" in c.name), None)
        check(cookie is not None and cookie.secure and cookie.has_nonstandard_attr("HttpOnly"),
              f"session cookie {cookie.name if cookie else None} is Secure + HttpOnly")

    def h(self):
        if not self._tok:
            self._tok = self.s.get(f"{BASE}/api/auth/token", timeout=30).json()["token"]
        return {"Authorization": f"Bearer {self._tok}"}

    def api(self, method, path, **kw):
        return self.s.request(method, f"{BASE}{path}", headers=self.h(), timeout=kw.pop("timeout", 200), **kw)


a, b = User("a"), User("b")
print("accounts")
a.signup("Ava Tester")
a.signin()
check(a.api("GET", "/api/me").json()["first_name"] == "Ava", "/api/me knows the user")
check(requests.get(f"{BASE}/api/closet/items", timeout=30).status_code == 401, "API without token -> 401")
r = requests.get(f"{BASE}/closet", allow_redirects=False, timeout=30)
check(r.status_code in (302, 307) and "/login" in r.headers.get("location", ""), "signed-out page -> /login")

print("detect (1 Gemini call) + add to closet + render (1 Gemini call)")
t0 = time.time()
with open(IMG, "rb") as f:
    r = a.api("POST", "/api/detect", files={"file": ("product.jpg", f, "image/jpeg")}, data={"purpose": "closet"})
check(r.status_code == 200, f"detect {r.status_code} in {time.time()-t0:.1f}s")
det = r.json()
print("   detector:", det.get("detector"), "| items:", [(i["category"], i["attributes"].get("subcategory")) for i in det["items"]])
check(det.get("detector") == "gemini" and det["items"], "Gemini detected garments")
pick = next((i for i in det["items"] if i["category"] == "outerwear"), det["items"][0])
r = a.api("POST", "/api/closet/items", json={"item_ids": [pick["id"]]})
check(r.status_code == 200, "add one item to closet")
iid = pick["id"]
deadline = time.time() + 240
while time.time() < deadline:
    it = a.api("GET", f"/api/items/{iid}").json()
    if it.get("render_status") in ("done", "failed"):
        break
    time.sleep(4)
print("   render:", it.get("render_status"), it.get("clean_method"))
check(it.get("render_status") == "done" and it.get("clean_image_url"), "clean render finished")
url = it["clean_image_url"]
check(url.startswith("/media/u/") and "sig=" in url, "image URL is per-user and signed")
r = requests.get(BASE + url, timeout=60)
check(r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) > 1000,
      f"photo loads through the tunnel ({len(r.content)} bytes)")
check(requests.get(BASE + url.split("?")[0], timeout=30).status_code == 404, "unsigned photo URL -> 404")
check(requests.get(BASE + re.sub(r"sig=\w+", "sig=" + "0" * 32, url), timeout=30).status_code == 404, "forged signature -> 404")

print("second user")
b.signup("Ben Tester")
b.signin()
check(b.api("GET", "/api/closet/items").json()["items"] == [], "B's closet is empty")
check(b.api("GET", f"/api/items/{iid}").status_code == 404, "B can't read A's item")
check(b.api("DELETE", f"/api/closet/items/{iid}").status_code == 404, "B can't delete A's item")
check(len(a.api("GET", "/api/closet/items").json()["items"]) == 1, "A still has 1 item")
print("MEDIA_URL", url)  # for the bucket check after deletion

print("cleanup")
for u in (a, b):
    r = u.api("DELETE", "/api/account", json={"confirm": "DELETE", "password": PW})
    check(r.status_code == 200, f"deleted {u.email}")
time.sleep(1)
check(requests.get(BASE + url, timeout=30).status_code == 404, "A's photo is gone after deletion")
print("ALL E2E CHECKS PASSED")
