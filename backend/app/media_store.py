"""Per-user media: signed URLs + a pluggable durable store.

Local layout: MEDIA_DIR/u/<user_id>/<originals|crops|cutouts|white|context|clean|suggest>/<random>.jpg
URLs:  /media/u/<user_id>/...?exp=<unix>&sig=<hmac>  -- HMAC-SHA256(MEDIA_SIGNING_SECRET, "<rel>|<exp>").
       Expiry is bucketed (valid 6-12 h) so the same image keeps the same URL for browser caching.

MEDIA_STORE selects where files are kept durably (the local dir is always the working copy / cache):
  local (default)  only MEDIA_DIR (put it on a persistent volume).
  s3               any S3-compatible bucket via boto3: MinIO now, AWS S3 / GCS (interop) / R2 later. Env:
                   S3_BUCKET, S3_ENDPOINT_URL (blank = AWS), S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
                   S3_FORCE_PATH_STYLE=1 (MinIO), MEDIA_S3_PRESIGN=1 to redirect /media to a 5-minute presigned
                   S3 URL instead of streaming through the backend (needs a bucket endpoint browsers can reach).
  blob             legacy Vercel Blob (FITCHECK_MEDIA_BLOB_TOKEN / BLOB_READ_WRITE_TOKEN). Off unless chosen.
Object key: fitcheck-media/<hmac(user)>/<rel>; the bucket stays private. Files are uploaded after each successful
write request / background job (sync_user) and pulled down on an instance's first request for that user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

log = logging.getLogger("fitcheck.media")

URL_TTL_BUCKET_S = 6 * 3600
API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob")
API_VERSION = "12"
ROOT = "fitcheck-media/"


def store_kind() -> str:
    k = os.environ.get("MEDIA_STORE", "").lower()
    if not k and os.environ.get("FITCHECK_MEDIA_STORE", "").lower() == "blob":  # older name
        k = "blob"
    return k if k in ("s3", "blob") else "local"


# ------------------------------------------------------------------ signing
def _secret() -> bytes:
    s = os.environ.get("MEDIA_SIGNING_SECRET") or os.environ.get("BACKEND_JWT_SECRET") or ""
    if not s:
        if os.environ.get("VERCEL") or os.environ.get("FITCHECK_ENV") == "production":
            raise RuntimeError("MEDIA_SIGNING_SECRET / BACKEND_JWT_SECRET must be set")
        s = "dev-only-insecure-media-secret"
    return hmac.new(s.encode(), b"fitcheck-media-v1", hashlib.sha256).digest()


def _sig(rel: str, exp: int) -> str:
    return hmac.new(_secret(), f"{rel}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_path(rel: str) -> str:
    exp = (int(time.time()) // URL_TTL_BUCKET_S + 2) * URL_TTL_BUCKET_S
    return f"/media/{urllib.parse.quote(rel)}?exp={exp}&sig={_sig(rel, exp)}"


def verify(rel: str, exp: str | None, sig: str | None) -> bool:
    try:
        e = int(exp or "0")
    except ValueError:
        return False
    if not sig or e < time.time() or not rel.startswith("u/") or ".." in rel.split("/"):
        return False
    return hmac.compare_digest(_sig(rel, e), sig)


def user_dir(uid: str) -> Path:
    return config.MEDIA_DIR / "u" / uid


# ------------------------------------------------------------------ durable stores
class S3Store:
    def __init__(self):
        import boto3
        from botocore.config import Config
        self.bucket = os.environ["S3_BUCKET"]
        self.c = boto3.client(
            "s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
            region_name=os.environ.get("S3_REGION") or "us-east-1",
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY") or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3},
                          s3={"addressing_style": "path" if os.environ.get("S3_FORCE_PATH_STYLE", "1") == "1" else "auto"}))

    def ensure_bucket(self, tries: int = 30) -> None:
        """Create the (private) bucket if it doesn't exist; waits for the S3 service to come up."""
        last = None
        for _ in range(tries):
            try:
                try:
                    self.c.head_bucket(Bucket=self.bucket)
                except self.c.exceptions.ClientError as e:
                    if str(e.response.get("Error", {}).get("Code")) not in ("404", "NoSuchBucket", "NotFound"):
                        raise
                    self.c.create_bucket(Bucket=self.bucket)
                    log.info("created bucket %s", self.bucket)
                return
            except Exception as e:  # noqa: BLE001  (service still starting)
                last = e
                time.sleep(2)
        raise RuntimeError(f"S3 bucket {self.bucket} not reachable: {last}")

    def put(self, key: str, data: bytes, ctype: str) -> str:
        self.c.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=ctype)
        return key

    def list(self, prefix: str) -> list[tuple[str, str]]:
        out = []
        for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out += [(o["Key"], o["Key"]) for o in page.get("Contents") or []]
        return out

    def get(self, ref: str) -> bytes:
        return self.c.get_object(Bucket=self.bucket, Key=ref)["Body"].read()

    def delete(self, refs: list[str]) -> None:
        for i in range(0, len(refs), 1000):
            self.c.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in refs[i:i + 1000]]})

    def presign(self, key: str, ttl: int = 300) -> str:
        return self.c.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl)


class BlobStore:  # legacy Vercel Blob REST (same calls as @vercel/blob 2.x)
    def _token(self) -> str:
        return os.environ.get("FITCHECK_MEDIA_BLOB_TOKEN") or os.environ.get("BLOB_READ_WRITE_TOKEN") or ""

    def _access(self) -> str:
        default = "private" if os.environ.get("FITCHECK_MEDIA_BLOB_TOKEN") else "public"
        return "public" if os.environ.get("FITCHECK_MEDIA_BLOB_ACCESS", default).lower() == "public" else "private"

    def _api(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 30):
        tok = self._token()
        h = {"authorization": f"Bearer {tok}", "x-api-version": API_VERSION,
             "x-vercel-blob-store-id": (tok.split("_") + [""] * 4)[3], **(headers or {})}
        if body is not None:
            h["x-content-length"] = str(len(body))
        req = urllib.request.Request(API + path, data=body, method=method, headers=h)
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500 and e.code != 429:
                    raise RuntimeError(f"blob {method} {e.code}: {e.read()[:200]!r}") from None
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"blob {method} failed: {last}")

    def put(self, key: str, data: bytes, ctype: str) -> str:
        return self._api("PUT", "/?" + urllib.parse.urlencode({"pathname": key}), data, {
            "x-vercel-blob-access": self._access(), "x-add-random-suffix": "0", "x-allow-overwrite": "1",
            "x-content-type": ctype, "x-cache-control-max-age": "60"}, timeout=60)["url"]

    def list(self, prefix: str) -> list[tuple[str, str]]:
        out, cursor = [], None
        while True:
            q = {"prefix": prefix, "limit": "1000", **({"cursor": cursor} if cursor else {})}
            r = self._api("GET", "?" + urllib.parse.urlencode(q))
            out += [(b["pathname"], b["url"]) for b in r.get("blobs") or []]
            cursor = r.get("cursor")
            if not r.get("hasMore") or not cursor:
                return out

    def get(self, ref: str) -> bytes:
        h = {"User-Agent": "fitcheck"}
        if self._access() == "private":
            h["Authorization"] = f"Bearer {self._token()}"
        with urllib.request.urlopen(urllib.request.Request(ref, headers=h), timeout=60) as r:
            return r.read()

    def delete(self, refs: list[str]) -> None:
        for i in range(0, len(refs), 500):
            self._api("POST", "/delete", json.dumps({"urls": refs[i:i + 500]}).encode(),
                      {"content-type": "application/json"})

    def presign(self, key: str, ttl: int = 300) -> str | None:
        return None


_store = None
_store_lock = threading.Lock()


def store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = S3Store() if store_kind() == "s3" else BlobStore()
    return _store


def enabled() -> bool:
    k = store_kind()
    if k == "s3":
        return bool(os.environ.get("S3_BUCKET"))
    if k == "blob":
        return bool(BlobStore()._token())
    return False


def ensure_ready() -> None:
    """Startup: with MEDIA_STORE=s3 and S3_CREATE_BUCKET=1 (default), make sure the bucket exists."""
    if store_kind() == "s3" and enabled() and os.environ.get("S3_CREATE_BUCKET", "1") == "1":
        store().ensure_bucket()


def _user_prefix(uid: str) -> str:
    seg = hmac.new(_secret(), f"blob|{uid}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ROOT}{seg}/"


def _ctype(p: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif"}.get(p.suffix.lower(), "application/octet-stream")


def presigned_url(rel: str) -> str | None:
    """S3 presigned URL for a verified rel path when MEDIA_S3_PRESIGN=1 (else None = stream via the backend)."""
    if store_kind() != "s3" or os.environ.get("MEDIA_S3_PRESIGN") != "1" or not enabled():
        return None
    parts = rel.split("/", 2)  # u / <uid> / rest
    return store().presign(_user_prefix(parts[1]) + parts[2])


# ------------------------------------------------------------------ per-user sync
_known: dict[str, dict[str, tuple[int, int, str]]] = {}   # uid -> rel-in-user-dir -> (size, mtime_ns, url)
_restored: set[str] = set()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(uid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(uid, threading.Lock())


def restore_user(uid: str) -> None:
    """Once per instance per user: download that user's media from Blob (missing files only). Never raises."""
    if not enabled() or uid in _restored:
        return
    with _lock(uid):
        if uid in _restored:
            return
        t0 = time.time()
        try:
            pre = _user_prefix(uid)
            blobs = store().list(pre)
            base = user_dir(uid)
            known = _known.setdefault(uid, {})

            def fetch(b: tuple[str, str]) -> None:
                rel = b[0][len(pre):]
                if ".." in rel.split("/"):
                    return
                dest = base / rel
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(store().get(b[1]))
                    tmp.rename(dest)
                st = dest.stat()
                known[rel] = (st.st_size, st.st_mtime_ns, b[1])

            with ThreadPoolExecutor(8) as ex:
                list(ex.map(fetch, blobs))
            _restored.add(uid)
            log.info("media: restored %d files for a user in %.1fs", len(blobs), time.time() - t0)
        except Exception:
            log.exception("media restore failed (will retry on the next request)")


def sync_user(uid: str | None, reason: str = "") -> None:
    """Upload new/changed files of this user to Blob and delete removed ones. Never raises."""
    if not uid or not enabled():
        return
    with _lock(uid):
        try:
            base = user_dir(uid)
            known = _known.setdefault(uid, {})
            local: dict[str, Path] = {}
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file() and not p.name.endswith((".part", ".tmp")):
                        local[p.relative_to(base).as_posix()] = p
            todo = []
            for rel, p in local.items():
                st = p.stat()
                if known.get(rel, (None, None))[:2] != (st.st_size, st.st_mtime_ns):
                    todo.append((rel, p, st))
            pre = _user_prefix(uid)

            def up(job):
                rel, p, st = job
                known[rel] = (st.st_size, st.st_mtime_ns, store().put(pre + rel, p.read_bytes(), _ctype(p)))

            if todo:
                with ThreadPoolExecutor(6) as ex:
                    list(ex.map(up, todo))
            gone = [rel for rel in known if rel not in local] if uid in _restored else []
            if gone:
                store().delete([known[r][2] for r in gone])
                for r in gone:
                    known.pop(r, None)
            if todo or gone:
                log.info("media sync (%s): %d up, %d deleted", reason, len(todo), len(gone))
        except Exception:
            log.exception("media sync failed (%s)", reason)


def sync_current(reason: str = "") -> None:
    from .db import maybe_user_id
    sync_user(maybe_user_id(), reason)


def local_file(rel: str) -> Path | None:
    """Local path for a verified rel path, restoring the owner's media from Blob first if needed."""
    p = (config.MEDIA_DIR / rel).resolve()
    if not str(p).startswith(str((config.MEDIA_DIR / "u").resolve()) + os.sep):
        return None
    if not p.exists() and enabled():
        restore_user(rel.split("/")[1])
    return p if p.is_file() else None


def delete_user(uid: str) -> None:
    """Account deletion: remove every media file of this user, locally and in Blob."""
    shutil.rmtree(user_dir(uid), ignore_errors=True)
    _known.pop(uid, None)
    _restored.discard(uid)
    if enabled():
        refs = [ref for _, ref in store().list(_user_prefix(uid))]
        if refs:
            store().delete(refs)
