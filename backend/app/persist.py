"""Optional persistence of the data dir to Vercel Blob (serverless instances lose /tmp on cold start).

Active only when BLOB_READ_WRITE_TOKEN is set AND we run on Vercel (env VERCEL) or FITCHECK_PERSIST=blob
(FITCHECK_PERSIST=0 turns it off). Everything else is a no-op, so local runs are unaffected. Stdlib only.

Blob layout under PREFIX (public store, but the prefix is unguessable and listing needs the token):
  snap/<ms-timestamp>-<instance>.tgz   gzip tar of a consistent SQLite backup + closet.faiss + closet.ids.npy.
                                       A new, unique name per save (CDN caches of an overwritten URL can stay
                                       stale for up to 60 s, a fresh name never is); older snapshots are deleted
                                       right after a newer one is uploaded, so there is normally exactly one.
  media/<relative path>                each media file uploaded once (re-uploaded with overwrite if it changes;
                                       deleted from Blob when the local file is deleted).
restore(): list PREFIX, download the newest snapshot + all media. save(): coalesced, locked, never raises.
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("fitcheck.persist")

API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob")
API_VERSION = "12"
PREFIX = os.environ.get("FITCHECK_PERSIST_PREFIX", "fitcheck/state/4e46a7f1bb12e658458b").strip("/") + "/"
SNAP_FILES = ("fitcheck.db", "closet.faiss", "closet.ids.npy")
INSTANCE = secrets.token_hex(3)

_lock = threading.Lock()        # one save at a time
_glock = threading.Lock()       # generation counter
_gen = 0                        # bumped on every "something changed"
_saved_gen = 0                  # highest generation covered by a finished save
_media: dict[str, tuple[int, int, str]] = {}   # rel path -> (size, mtime_ns, blob url) of what's in Blob
_stale_snaps: list[str] = []    # snapshot urls to delete after the next successful save
_disabled = False               # set if restore couldn't read Blob (never overwrite state we couldn't load)


def _token() -> str:
    return os.environ.get("BLOB_READ_WRITE_TOKEN", "")


def enabled() -> bool:
    mode = os.environ.get("FITCHECK_PERSIST", "").lower()
    if _disabled or mode == "0" or not _token():
        return False
    return mode == "blob" or bool(os.environ.get("VERCEL"))


# ------------------------------------------------------------------ Blob REST (same calls as @vercel/blob 2.x)
def _api(method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 30) -> dict:
    tok = _token()
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


def _put(pathname: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    r = _api("PUT", "/?" + urllib.parse.urlencode({"pathname": pathname}), data, {
        "x-vercel-blob-access": "public", "x-add-random-suffix": "0", "x-allow-overwrite": "1",
        "x-content-type": content_type, "x-cache-control-max-age": "60"}, timeout=60)
    return r["url"]


def _list(prefix: str) -> list[dict]:
    out, cursor = [], None
    while True:
        q = {"prefix": prefix, "limit": "1000", **({"cursor": cursor} if cursor else {})}
        r = _api("GET", "?" + urllib.parse.urlencode(q))
        out += r.get("blobs") or []
        cursor = r.get("cursor")
        if not r.get("hasMore") or not cursor:
            return out


def _delete(urls: list[str]) -> None:
    for i in range(0, len(urls), 500):
        _api("POST", "/delete", json.dumps({"urls": urls[i:i + 500]}).encode(), {"content-type": "application/json"})


def _download(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fitcheck"}), timeout=60) as r:
        return r.read()


def _ctype(p: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif", ".json": "application/json"}.get(p.suffix.lower(), "application/octet-stream")


# ------------------------------------------------------------------ restore (called by vercel_entry before the app imports)
def restore(run: Path) -> bool:
    """Fill `run` from the newest saved snapshot. True = restored; False = nothing saved yet (or Blob unreadable,
    in which case saving is disabled for this instance so an empty closet never overwrites the saved one)."""
    global _disabled
    if not enabled():
        return False
    t0 = time.time()
    try:
        blobs = _list(PREFIX)
    except Exception as e:  # noqa: BLE001
        log.error("persist: can't list Blob (%s); starting empty WITHOUT saving", e)
        print(f"[persist] list failed ({e}); saving disabled for this instance", flush=True)
        _disabled = True
        return False
    snaps = sorted((b for b in blobs if b["pathname"].startswith(PREFIX + "snap/")), key=lambda b: b["pathname"])
    if not snaps:
        return False
    try:
        run.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(_download(snaps[-1]["url"])), mode="r:gz") as t:
            t.extractall(run, filter="data")
        media_dir = run / "media"
        media = [b for b in blobs if b["pathname"].startswith(PREFIX + "media/")]

        def fetch(b: dict) -> None:
            rel = b["pathname"][len(PREFIX + "media/"):]
            dest = media_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_download(b["url"]))
            st = dest.stat()
            _media[rel] = (st.st_size, st.st_mtime_ns, b["url"])

        with ThreadPoolExecutor(8) as ex:
            list(ex.map(fetch, media))
    except Exception as e:  # noqa: BLE001
        log.exception("persist: restore failed")
        print(f"[persist] restore failed ({e}); saving disabled for this instance", flush=True)
        _disabled = True
        return False
    _stale_snaps.extend(b["url"] for b in snaps)   # all deleted once this instance saves a newer one
    print(f"[persist] restored {snaps[-1]['pathname'][len(PREFIX):]} + {len(media)} media files "
          f"({sum(b.get('size', 0) for b in media) / 1e6:.1f} MB) in {time.time() - t0:.1f}s", flush=True)
    return True


# ------------------------------------------------------------------ save
def mark_dirty() -> int:
    global _gen
    with _glock:
        _gen += 1
        return _gen


def save(reason: str = "") -> None:
    """Upload the current state (synchronously). Coalesced: if another save that started after this change
    already finished, return at once. Never raises."""
    global _saved_gen
    if not enabled():
        return
    target = mark_dirty()
    with _lock:
        if _saved_gen >= target:
            return
        with _glock:
            covered = _gen
        t0 = time.time()
        try:
            n = _save_once()
            _saved_gen = covered
            log.info("persist: saved (%s) in %.2fs, %d media uploaded", reason, time.time() - t0, n)
        except Exception:
            log.exception("persist: save failed (%s)", reason)


def _save_once() -> int:
    from . import config
    # 1) media first, so a snapshot never points at files that aren't in Blob yet
    local: dict[str, Path] = {}
    if config.MEDIA_DIR.exists():
        for p in config.MEDIA_DIR.rglob("*"):
            if p.is_file() and not p.name.startswith(".") and not p.name.endswith((".part", ".tmp")):
                local[p.relative_to(config.MEDIA_DIR).as_posix()] = p
    todo = []
    for rel, p in local.items():
        st = p.stat()
        have = _media.get(rel)
        if not have or have[:2] != (st.st_size, st.st_mtime_ns):
            todo.append((rel, p, st))

    def up(job):
        rel, p, st = job
        _media[rel] = (st.st_size, st.st_mtime_ns, _put(PREFIX + "media/" + rel, p.read_bytes(), _ctype(p)))

    if todo:
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(up, todo))
    # 2) snapshot: consistent SQLite backup + FAISS files (under the index lock)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = sqlite3.connect(config.DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp / "fitcheck.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        from .vectors import closet_index
        with closet_index._lock:
            for name, f in (("closet.faiss", config.FAISS_PATH), ("closet.ids.npy", config.FAISS_PATH.with_suffix(".ids.npy"))):
                if f.exists():
                    (tmp / name).write_bytes(f.read_bytes())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as t:
            for name in SNAP_FILES:
                if (tmp / name).exists():
                    t.add(tmp / name, arcname=name)
    url = _put(f"{PREFIX}snap/{int(time.time() * 1000):013d}-{INSTANCE}.tgz", buf.getvalue(), "application/gzip")
    # 3) cleanup: older snapshots + media deleted locally (only blobs under our own PREFIX)
    gone = [rel for rel in _media if rel not in local]
    old = [u for u in _stale_snaps if u != url]
    stale = old + [_media[rel][2] for rel in gone]
    _stale_snaps[:] = old + [url]
    if stale:
        try:
            _delete(stale)
            for rel in gone:
                _media.pop(rel, None)
            _stale_snaps[:] = [url]
        except Exception:
            log.exception("persist: cleanup delete failed (harmless, retried next save)")
    return len(todo)
