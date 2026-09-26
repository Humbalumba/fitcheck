"""SQLite storage: a small graph in relational form.

Nodes: photos, items, outfits, evaluations. Edges: compat_edges (item<->item(s)
compatibility cache), outfit_items (outfit -> item), items.photo_id (item -> photo).
Plain sqlite3; one connection per call, WAL mode so the API threads can share.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np

from . import config

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    purpose TEXT,
    width INTEGER, height INTEGER,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('closet','candidate','detected','suggestion')),
    category TEXT,
    attributes TEXT NOT NULL DEFAULT '{}',
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    bbox TEXT,
    label TEXT,
    crop_path TEXT,
    cutout_path TEXT,
    white_path TEXT,
    context_path TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, category);
CREATE TABLE IF NOT EXISTS item_embeddings (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- 'fclip' | 'compat'
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, kind)
);
CREATE TABLE IF NOT EXISTS compat_edges (
    outfit_key TEXT NOT NULL,     -- sorted item ids joined by '|'
    model TEXT NOT NULL,
    item_a TEXT NOT NULL,
    item_b TEXT,
    item_c TEXT,
    n_items INTEGER NOT NULL,
    score REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (outfit_key, model)
);
CREATE INDEX IF NOT EXISTS idx_compat_a ON compat_edges(item_a);
CREATE INDEX IF NOT EXISTS idx_compat_b ON compat_edges(item_b);
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    candidate_item_id TEXT REFERENCES items(id) ON DELETE CASCADE,
    price REAL,
    verdict TEXT,
    results TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outfits (
    id TEXT PRIMARY KEY,
    evaluation_id TEXT REFERENCES evaluations(id) ON DELETE CASCADE,
    template TEXT NOT NULL,
    score REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outfit_items (
    outfit_id TEXT NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position INTEGER,
    PRIMARY KEY (outfit_id, item_id)
);
-- shopping suggestions per evaluation (cached so re-opening a result doesn't spend Gemini quota)
CREATE TABLE IF NOT EXISTS suggestions (
    evaluation_id TEXT PRIMARY KEY REFERENCES evaluations(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,           -- 'alternatives' (verdict SKIP) | 'pairings' (verdict BUY)
    results TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    updated_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized and config.DB_PATH.exists():
            return
        conn = _connect()
        _migrate_items_status(conn)
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row is None:
            conn.execute("INSERT INTO settings(id, data, updated_at) VALUES (1, ?, ?)",
                         (json.dumps(config.DEFAULT_SETTINGS), now_iso()))
        conn.commit()
        conn.close()
        _initialized = True


ITEM_COLUMNS = ("id, status, category, attributes, photo_id, bbox, label, crop_path, cutout_path, white_path, "
                "context_path, source, created_at, updated_at")


def _migrate_items_status(conn: sqlite3.Connection) -> None:
    """Older DBs have CHECK (status IN ('closet','candidate','detected')). SQLite can't ALTER a CHECK, so rebuild
    the items table (standard 12-step procedure: FKs off, copy rows, swap, verify) keeping every row, then
    re-enable FKs. Embeddings / outfits / evaluations keep pointing at the same item ids."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
    if row is None or "'suggestion'" in (row[0] or ""):
        return
    items_ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS items (", 1)[1].split(");", 1)[0]
    n_before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    old_iso = conn.isolation_level
    conn.isolation_level = None  # manual transaction control (PRAGMA foreign_keys is a no-op inside one)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS items_new")
        conn.execute(f"CREATE TABLE items_new ({items_ddl})")
        conn.execute(f"INSERT INTO items_new ({ITEM_COLUMNS}) SELECT {ITEM_COLUMNS} FROM items")
        n_after = conn.execute("SELECT COUNT(*) FROM items_new").fetchone()[0]
        if n_after != n_before:
            raise RuntimeError(f"items migration copied {n_after}/{n_before} rows")
        conn.execute("DROP TABLE items")
        conn.execute("ALTER TABLE items_new RENAME TO items")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, category)")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign key check failed after items migration: {bad[:3]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = old_iso
    import logging
    logging.getLogger("fitcheck.db").info("Migrated items table: status CHECK now allows 'suggestion' (%d rows kept)",
                                          n_before)


@contextmanager
def get_conn():
    init_db()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- settings
def get_settings() -> dict:
    with get_conn() as c:
        row = c.execute("SELECT data FROM settings WHERE id=1").fetchone()
    data = dict(config.DEFAULT_SETTINGS)
    if row:
        data.update(json.loads(row["data"]))
    return data


def update_settings(partial: dict) -> dict:
    cur = get_settings()
    cur.update({k: v for k, v in partial.items() if k in config.DEFAULT_SETTINGS})
    with get_conn() as c:
        c.execute("UPDATE settings SET data=?, updated_at=? WHERE id=1", (json.dumps(cur), now_iso()))
    return cur


# ---------------------------------------------------------------- photos / items
def insert_photo(path: str, purpose: str | None, width: int, height: int, source: str = "upload") -> str:
    pid = new_id("ph_")
    with get_conn() as c:
        c.execute("INSERT INTO photos(id,path,purpose,width,height,source,created_at) VALUES (?,?,?,?,?,?,?)",
                  (pid, path, purpose, width, height, source, now_iso()))
    return pid


def insert_item(*, status: str, category: str | None, attributes: dict, photo_id: str | None = None,
                bbox=None, label: str | None = None, crop_path=None, cutout_path=None, white_path=None,
                context_path=None, source: str = "upload", item_id: str | None = None) -> str:
    iid = item_id or new_id("it_")
    with get_conn() as c:
        c.execute(
            """INSERT INTO items(id,status,category,attributes,photo_id,bbox,label,crop_path,cutout_path,
               white_path,context_path,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, status, category, json.dumps(attributes), photo_id, json.dumps(bbox) if bbox else None, label,
             str(crop_path) if crop_path else None, str(cutout_path) if cutout_path else None,
             str(white_path) if white_path else None, str(context_path) if context_path else None,
             source, now_iso(), now_iso()))
    return iid


def _row_to_item(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["attributes"] = json.loads(d.get("attributes") or "{}")
    d["bbox"] = json.loads(d["bbox"]) if d.get("bbox") else None
    return d


def get_item(item_id: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def list_items(status: str | None = None, category: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM items WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if category:
        q += " AND category=?"; args.append(category)
    q += " ORDER BY created_at, id"
    with get_conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_row_to_item(r) for r in rows]


def update_item(item_id: str, *, status: str | None = None, attributes: dict | None = None,
                category: str | None = None) -> None:
    sets, args = ["updated_at=?"], [now_iso()]
    if status is not None:
        sets.append("status=?"); args.append(status)
    if attributes is not None:
        sets.append("attributes=?"); args.append(json.dumps(attributes))
        if category is None and attributes.get("category"):
            category = attributes["category"]
    if category is not None:
        sets.append("category=?"); args.append(category)
    args.append(item_id)
    with get_conn() as c:
        c.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", args)


def delete_item(item_id: str) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM compat_edges WHERE item_a=? OR item_b=? OR item_c=? OR outfit_key LIKE ?",
                  (item_id, item_id, item_id, f"%{item_id}%"))
        c.execute("DELETE FROM items WHERE id=?", (item_id,))


def invalidate_compat(item_id: str) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM compat_edges WHERE outfit_key LIKE ?", (f"%{item_id}%",))


# ---------------------------------------------------------------- embeddings
def put_embedding(item_id: str, kind: str, model: str, vec: np.ndarray) -> None:
    v = np.asarray(vec, dtype=np.float32).ravel()
    with get_conn() as c:
        c.execute("""INSERT OR REPLACE INTO item_embeddings(item_id,kind,model,dim,vector,created_at)
                     VALUES (?,?,?,?,?,?)""", (item_id, kind, model, v.shape[0], v.tobytes(), now_iso()))


def get_embedding(item_id: str, kind: str, model: str | None = None) -> np.ndarray | None:
    with get_conn() as c:
        row = c.execute("SELECT model, vector FROM item_embeddings WHERE item_id=? AND kind=?",
                        (item_id, kind)).fetchone()
    if row is None or (model is not None and row["model"] != model):
        return None
    return np.frombuffer(row["vector"], dtype=np.float32).copy()


def get_embeddings(item_ids: list[str], kind: str, model: str | None = None) -> dict[str, np.ndarray]:
    if not item_ids:
        return {}
    out = {}
    with get_conn() as c:
        for i in range(0, len(item_ids), 500):
            chunk = item_ids[i:i + 500]
            rows = c.execute(
                f"SELECT item_id, model, vector FROM item_embeddings WHERE kind=? AND item_id IN ({','.join('?'*len(chunk))})",
                [kind, *chunk]).fetchall()
            for r in rows:
                if model is None or r["model"] == model:
                    out[r["item_id"]] = np.frombuffer(r["vector"], dtype=np.float32).copy()
    return out


# ---------------------------------------------------------------- compat cache
def outfit_key(ids: list[str]) -> str:
    return "|".join(sorted(ids))


def get_compat_scores(keys: list[str], model: str) -> dict[str, float]:
    out = {}
    if not keys:
        return out
    with get_conn() as c:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            rows = c.execute(
                f"SELECT outfit_key, score FROM compat_edges WHERE model=? AND outfit_key IN ({','.join('?'*len(chunk))})",
                [model, *chunk]).fetchall()
            out.update({r["outfit_key"]: r["score"] for r in rows})
    return out


def put_compat_scores(entries: list[tuple[list[str], float]], model: str) -> None:
    ts = now_iso()
    rows = []
    for ids, score in entries:
        s = sorted(ids)
        rows.append((outfit_key(s), model, s[0], s[1] if len(s) > 1 else None, s[2] if len(s) > 2 else None,
                     len(s), float(score), ts))
    with get_conn() as c:
        c.executemany("""INSERT OR REPLACE INTO compat_edges(outfit_key,model,item_a,item_b,item_c,n_items,score,created_at)
                         VALUES (?,?,?,?,?,?,?,?)""", rows)


# ---------------------------------------------------------------- evaluations
def save_evaluation(candidate_id: str, price, verdict: str, results: dict, outfits: list[dict]) -> str:
    eid = new_id("ev_")
    ts = now_iso()
    with get_conn() as c:
        c.execute("INSERT INTO evaluations(id,candidate_item_id,price,verdict,results,created_at) VALUES (?,?,?,?,?,?)",
                  (eid, candidate_id, price, verdict, json.dumps(results), ts))
        for o in outfits:
            oid = new_id("of_")
            c.execute("INSERT INTO outfits(id,evaluation_id,template,score,created_at) VALUES (?,?,?,?,?)",
                      (oid, eid, o["template"], o["score"], ts))
            c.executemany("INSERT OR IGNORE INTO outfit_items(outfit_id,item_id,position) VALUES (?,?,?)",
                          [(oid, iid, pos) for pos, iid in enumerate(o["item_ids"])])
    return eid


def get_evaluation(eid: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM evaluations WHERE id=?", (eid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["results"] = json.loads(d["results"])
    return d


def get_suggestions(eid: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT results FROM suggestions WHERE evaluation_id=?", (eid,)).fetchone()
    return json.loads(row["results"]) if row else None


def save_suggestions(eid: str, mode: str, results: dict) -> None:
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO suggestions(evaluation_id, mode, results, created_at) VALUES (?,?,?,?)",
                  (eid, mode, json.dumps(results), now_iso()))


def reset_all() -> None:
    """Drop everything (used by seed --reset)."""
    global _initialized
    with _init_lock:
        for p in (config.DB_PATH, config.DB_PATH.with_suffix(".db-wal"), config.DB_PATH.with_suffix(".db-shm")):
            if p.exists():
                p.unlink()
        _initialized = False
    init_db()
