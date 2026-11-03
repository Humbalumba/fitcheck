"""Storage layer: SQLAlchemy Core over Postgres (DATABASE_URL, e.g. Neon) or SQLite for local dev.

Multi-user: every function here works on the CURRENT user's rows only. The user comes from a ContextVar set by
the auth dependency (app/auth.py) for each request, and copied into background work via CtxExecutor / bind().
If no user is set, data functions raise NoUserContext (fail closed) instead of touching anyone's data.
Media paths are stored relative to MEDIA_DIR (u/<user_id>/...) and returned as absolute local paths.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import and_, create_engine, delete, event, func, or_, select, update
from sqlalchemy.engine import Engine

from . import config
from . import models as m

log = logging.getLogger("fitcheck.db")

# ------------------------------------------------------------------ engine
_engine: Engine | None = None
_engine_lock = threading.Lock()


def database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
    if not url:
        return f"sqlite:///{config.DATA_DIR / 'fitcheck-local.db'}"  # dev only; never the legacy fitcheck.db
    for pre in ("postgres://", "postgresql://"):
        if url.startswith(pre):
            return "postgresql+psycopg://" + url[len(pre):]
    return url


def engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                url = database_url()
                if url.startswith("sqlite"):
                    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

                    @event.listens_for(eng, "connect")
                    def _fk(dbapi_conn, _):  # noqa: ANN001
                        dbapi_conn.execute("PRAGMA foreign_keys = ON")
                        dbapi_conn.execute("PRAGMA journal_mode = WAL")
                else:
                    # prepare_threshold=None: safe behind Neon's PgBouncer (transaction pooling)
                    eng = create_engine(url, pool_pre_ping=True, pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
                                        max_overflow=5, pool_recycle=300, connect_args={"prepare_threshold": None})
                _engine = eng
    return _engine


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"


_migrated = False
_mig_lock = threading.Lock()


def init_db() -> None:
    """Bring the schema to the newest Alembic revision (idempotent, cheap when already current)."""
    global _migrated
    with _mig_lock:
        if _migrated:
            return
        from alembic import command
        from alembic.config import Config
        cfg = Config(str(config.BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(config.BACKEND_DIR / "migrations"))
        cfg.attributes["engine"] = engine()
        command.upgrade(cfg, "head")
        _migrated = True


@contextmanager
def tx():
    with engine().begin() as c:
        yield c


def _upsert(conn, table, values: dict | list[dict], keys: list[str], update_cols: list[str] | None = None):
    rows = values if isinstance(values, list) else [values]
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _ins
    else:
        from sqlalchemy.dialects.sqlite import insert as _ins
    stmt = _ins(table).values(rows)
    cols = update_cols if update_cols is not None else [k for k in rows[0] if k not in keys]
    stmt = stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in cols}) if cols \
        else stmt.on_conflict_do_nothing(index_elements=keys)
    conn.execute(stmt)


# ------------------------------------------------------------------ current user
class NoUserContext(RuntimeError):
    pass


_current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("fitcheck_user", default=None)


def current_user_id() -> str:
    uid = _current_user.get()
    if not uid:
        raise NoUserContext("no authenticated user in this context")
    return uid


def maybe_user_id() -> str | None:
    return _current_user.get()


def set_user(uid: str | None):
    return _current_user.set(uid)


@contextmanager
def as_user(uid: str):
    tok = _current_user.set(uid)
    try:
        yield
    finally:
        _current_user.reset(tok)


def bind(fn):
    """fn bound to a copy of the current context (user id) -- for threads / timers."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.run(fn, *a, **k)


class CtxExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose tasks run with the submitter's context (so db calls stay scoped to that user)."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


# ------------------------------------------------------------------ helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


def _rel(p) -> str | None:
    """Absolute path under MEDIA_DIR -> 'u/<uid>/...' (what is stored)."""
    if not p:
        return None
    p = Path(p)
    try:
        return p.resolve().relative_to(config.MEDIA_DIR.resolve()).as_posix()
    except ValueError:
        return str(p)


def abs_media(p) -> str | None:
    if not p:
        return None
    return str(p) if os.path.isabs(str(p)) else str(config.MEDIA_DIR / p)


_PATH_COLS = ("crop_path", "cutout_path", "white_path", "context_path")


# ---------------------------------------------------------------- settings
def get_settings() -> dict:
    uid = current_user_id()
    with tx() as c:
        row = c.execute(select(m.settings.c.data).where(m.settings.c.user_id == uid)).first()
    data = dict(config.DEFAULT_SETTINGS)
    if row:
        data.update({k: v for k, v in json.loads(row.data).items() if k in config.DEFAULT_SETTINGS})
    return data


def update_settings(partial: dict) -> dict:
    uid = current_user_id()
    cur = get_settings()
    cur.update({k: v for k, v in partial.items() if k in config.DEFAULT_SETTINGS})
    with tx() as c:
        _upsert(c, m.settings, {"user_id": uid, "data": json.dumps(cur), "updated_at": now_iso()}, ["user_id"])
    return cur


# ---------------------------------------------------------------- photos / items
def insert_photo(path: str, purpose: str | None, width: int, height: int, source: str = "upload") -> str:
    uid = current_user_id()
    pid = new_id("ph_")
    with tx() as c:
        c.execute(m.photos.insert().values(id=pid, user_id=uid, path=_rel(path), purpose=purpose, width=width,
                                           height=height, source=source, created_at=now_iso()))
    return pid


def get_photo(photo_id: str | None) -> dict | None:
    if not photo_id:
        return None
    uid = current_user_id()
    with tx() as c:
        row = c.execute(select(m.photos).where(m.photos.c.id == photo_id, m.photos.c.user_id == uid)).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["path"] = abs_media(d["path"])
    return d


def insert_item(*, status: str, category: str | None, attributes: dict, photo_id: str | None = None,
                bbox=None, label: str | None = None, crop_path=None, cutout_path=None, white_path=None,
                context_path=None, source: str = "upload", item_id: str | None = None) -> str:
    uid = current_user_id()
    iid = item_id or new_id("it_")
    ts = now_iso()
    with tx() as c:
        c.execute(m.items.insert().values(
            id=iid, user_id=uid, status=status, category=category, attributes=json.dumps(attributes),
            photo_id=photo_id, bbox=json.dumps(bbox) if bbox else None, label=label, crop_path=_rel(crop_path),
            cutout_path=_rel(cutout_path), white_path=_rel(white_path), context_path=_rel(context_path),
            source=source, created_at=ts, updated_at=ts))
    return iid


def _row_to_item(row) -> dict:
    d = dict(row)
    d.pop("user_id", None)
    d["attributes"] = json.loads(d.get("attributes") or "{}")
    d["bbox"] = json.loads(d["bbox"]) if d.get("bbox") else None
    for k in _PATH_COLS:
        d[k] = abs_media(d.get(k))
    return d


def get_item(item_id: str) -> dict | None:
    uid = current_user_id()
    with tx() as c:
        row = c.execute(select(m.items).where(m.items.c.id == item_id, m.items.c.user_id == uid)).mappings().first()
    return _row_to_item(row) if row else None


def list_items(status: str | None = None, category: str | None = None) -> list[dict]:
    uid = current_user_id()
    q = select(m.items).where(m.items.c.user_id == uid)
    if status:
        q = q.where(m.items.c.status == status)
    if category:
        q = q.where(m.items.c.category == category)
    with tx() as c:
        rows = c.execute(q.order_by(m.items.c.created_at, m.items.c.id)).mappings().all()
    return [_row_to_item(r) for r in rows]


def update_item(item_id: str, *, status: str | None = None, attributes: dict | None = None,
                category: str | None = None) -> None:
    uid = current_user_id()
    vals: dict = {"updated_at": now_iso()}
    if status is not None:
        vals["status"] = status
    if attributes is not None:
        vals["attributes"] = json.dumps(attributes)
        if category is None and attributes.get("category"):
            category = attributes["category"]
    if category is not None:
        vals["category"] = category
    with tx() as c:
        c.execute(update(m.items).where(m.items.c.id == item_id, m.items.c.user_id == uid).values(**vals))


def _compat_touching(item_id: str):
    t = m.compat_edges.c
    return or_(t.item_a == item_id, t.item_b == item_id, t.item_c == item_id, t.outfit_key.contains(item_id))


def delete_item(item_id: str) -> None:
    uid = current_user_id()
    with tx() as c:
        c.execute(delete(m.compat_edges).where(m.compat_edges.c.user_id == uid, _compat_touching(item_id)))
        c.execute(delete(m.items).where(m.items.c.id == item_id, m.items.c.user_id == uid))


def invalidate_compat(item_id: str) -> None:
    uid = current_user_id()
    with tx() as c:
        c.execute(delete(m.compat_edges).where(m.compat_edges.c.user_id == uid,
                                               m.compat_edges.c.outfit_key.contains(item_id)))


def referenced_item_ids() -> set[str]:
    """Item ids still used by an evaluation (as candidate) or a saved outfit."""
    uid = current_user_id()
    with tx() as c:
        a = c.execute(select(m.evaluations.c.candidate_item_id).where(
            m.evaluations.c.user_id == uid, m.evaluations.c.candidate_item_id.is_not(None))).scalars().all()
        b = c.execute(select(m.outfit_items.c.item_id).where(m.outfit_items.c.user_id == uid)).scalars().all()
    return set(a) | set(b)


# ---------------------------------------------------------------- embeddings
def put_embedding(item_id: str, kind: str, model: str, vec: np.ndarray) -> None:
    uid = current_user_id()
    v = np.asarray(vec, dtype=np.float32).ravel()
    with tx() as c:
        if c.execute(select(m.items.c.id).where(m.items.c.id == item_id, m.items.c.user_id == uid)).first() is None:
            return
        _upsert(c, m.item_embeddings, {"item_id": item_id, "kind": kind, "user_id": uid, "model": model,
                                       "dim": int(v.shape[0]), "vector": v.tobytes(), "created_at": now_iso()},
                ["item_id", "kind"])


def delete_embeddings(item_id: str, kinds: list[str]) -> None:
    uid = current_user_id()
    with tx() as c:
        c.execute(delete(m.item_embeddings).where(m.item_embeddings.c.user_id == uid,
                                                  m.item_embeddings.c.item_id == item_id,
                                                  m.item_embeddings.c.kind.in_(kinds)))


def get_embedding(item_id: str, kind: str, model: str | None = None) -> np.ndarray | None:
    return get_embeddings([item_id], kind, model).get(item_id)


def get_embeddings(item_ids: list[str], kind: str, model: str | None = None) -> dict[str, np.ndarray]:
    if not item_ids:
        return {}
    uid = current_user_id()
    t = m.item_embeddings.c
    out = {}
    with tx() as c:
        for i in range(0, len(item_ids), 500):
            rows = c.execute(select(t.item_id, t.model, t.vector).where(
                t.user_id == uid, t.kind == kind, t.item_id.in_(item_ids[i:i + 500]))).all()
            for r in rows:
                if model is None or r.model == model:
                    out[r.item_id] = np.frombuffer(bytes(r.vector), dtype=np.float32).copy()
    return out


# ---------------------------------------------------------------- compat cache
def outfit_key(ids: list[str]) -> str:
    return "|".join(sorted(ids))


def get_compat_scores(keys: list[str], model: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not keys:
        return out
    uid = current_user_id()
    t = m.compat_edges.c
    with tx() as c:
        for i in range(0, len(keys), 500):
            rows = c.execute(select(t.outfit_key, t.score).where(
                t.user_id == uid, t.model == model, t.outfit_key.in_(keys[i:i + 500]))).all()
            out.update({r.outfit_key: r.score for r in rows})
    return out


def put_compat_scores(entries: list[tuple[list[str], float]], model: str) -> None:
    uid = current_user_id()
    ts = now_iso()
    rows = {}
    for ids, score in entries:
        s = sorted(ids)
        rows[outfit_key(s)] = {"user_id": uid, "outfit_key": outfit_key(s), "model": model, "item_a": s[0],
                               "item_b": s[1] if len(s) > 1 else None, "item_c": s[2] if len(s) > 2 else None,
                               "n_items": len(s), "score": float(score), "created_at": ts}
    vals = list(rows.values())
    with tx() as c:
        for i in range(0, len(vals), 500):
            _upsert(c, m.compat_edges, vals[i:i + 500], ["user_id", "outfit_key", "model"])


# ---------------------------------------------------------------- evaluations
def save_evaluation(candidate_id: str, price, verdict: str, results: dict, outfits: list[dict]) -> str:
    uid = current_user_id()
    eid = new_id("ev_")
    ts = now_iso()
    with tx() as c:
        c.execute(m.evaluations.insert().values(id=eid, user_id=uid, candidate_item_id=candidate_id, price=price,
                                                verdict=verdict, results=json.dumps(results), created_at=ts))
        for o in outfits:
            oid = new_id("of_")
            c.execute(m.outfits.insert().values(id=oid, user_id=uid, evaluation_id=eid, template=o["template"],
                                                score=o["score"], created_at=ts))
            seen, rows = set(), []
            for pos, iid in enumerate(o["item_ids"]):
                if iid not in seen:
                    seen.add(iid)
                    rows.append({"outfit_id": oid, "item_id": iid, "user_id": uid, "position": pos})
            if rows:
                c.execute(m.outfit_items.insert(), rows)
    return eid


def get_evaluation(eid: str) -> dict | None:
    uid = current_user_id()
    with tx() as c:
        row = c.execute(select(m.evaluations).where(m.evaluations.c.id == eid,
                                                    m.evaluations.c.user_id == uid)).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d.pop("user_id", None)
    d["results"] = json.loads(d["results"])
    return d


def latest_evaluation_for_item(item_id: str) -> dict | None:
    uid = current_user_id()
    t = m.evaluations.c
    with tx() as c:
        eid = c.execute(select(t.id).where(t.user_id == uid, t.candidate_item_id == item_id)
                        .order_by(t.created_at.desc(), t.id.desc()).limit(1)).scalar()
    return get_evaluation(eid) if eid else None


def get_suggestions(eid: str) -> dict | None:
    uid = current_user_id()
    t = m.suggestions.c
    with tx() as c:
        r = c.execute(select(t.results).where(t.evaluation_id == eid, t.user_id == uid)).scalar()
    return json.loads(r) if r else None


def save_suggestions(eid: str, mode: str, results: dict) -> None:
    uid = current_user_id()
    if get_evaluation(eid) is None:
        return
    with tx() as c:
        _upsert(c, m.suggestions, {"evaluation_id": eid, "user_id": uid, "mode": mode, "results": json.dumps(results),
                                   "created_at": now_iso()}, ["evaluation_id"])


# ---------------------------------------------------------------- render records (app/render.py)
def _render_row(r) -> dict:
    d = dict(r)
    d.pop("user_id", None)
    try:
        d["checks"] = json.loads(d.get("checks") or "null")
    except Exception:
        d["checks"] = None
    d["clean_path"] = abs_media(d.get("clean_path"))
    return d


def render_records() -> dict[str, dict]:
    uid = current_user_id()
    with tx() as c:
        rows = c.execute(select(m.item_renders).where(m.item_renders.c.user_id == uid)).mappings().all()
    return {r["item_id"]: _render_row(r) for r in rows}


def render_upsert(rec: dict) -> bool:
    """False if the item no longer exists (deleted meanwhile)."""
    uid = current_user_id()
    with tx() as c:
        if c.execute(select(m.items.c.id).where(m.items.c.id == rec["item_id"], m.items.c.user_id == uid)).first() is None:
            return False
        _upsert(c, m.item_renders, {
            "item_id": rec["item_id"], "user_id": uid, "status": rec.get("status") or "pending",
            "method": rec.get("method"), "clean_path": _rel(rec.get("clean_path")), "model": rec.get("model"),
            "checks": json.dumps(rec.get("checks")), "embed_source": rec.get("embed_source"),
            "error": rec.get("error"), "created_at": rec["created_at"], "updated_at": rec["updated_at"]}, ["item_id"])
    return True


def render_delete(item_id: str) -> None:
    uid = current_user_id()
    with tx() as c:
        c.execute(delete(m.item_renders).where(m.item_renders.c.item_id == item_id, m.item_renders.c.user_id == uid))


def details_get(item_id: str) -> tuple[dict, str] | None:
    uid = current_user_id()
    t = m.item_render_details.c
    with tx() as c:
        r = c.execute(select(t.details, t.frame).where(t.item_id == item_id, t.user_id == uid)).first()
    return (json.loads(r.details), r.frame) if r else None


def details_save(item_id: str, details: dict, frame: str, model: str | None) -> None:
    uid = current_user_id()
    with tx() as c:
        if c.execute(select(m.items.c.id).where(m.items.c.id == item_id, m.items.c.user_id == uid)).first() is None:
            return
        _upsert(c, m.item_render_details, {"item_id": item_id, "user_id": uid, "details": json.dumps(details),
                                           "frame": frame, "model": model, "created_at": now_iso()}, ["item_id"])


def details_delete(item_id: str) -> None:
    uid = current_user_id()
    t = m.item_render_details
    with tx() as c:
        c.execute(delete(t).where(t.c.item_id == item_id, t.c.user_id == uid))


# ---------------------------------------------------------------- "Worth a look" cache
def wardrobe_get(key: str) -> dict | None:
    uid = current_user_id()
    t = m.wardrobe_suggestions.c
    with tx() as c:
        r = c.execute(select(t.results).where(t.user_id == uid, t.closet_key == key)).scalar()
    return json.loads(r) if r else None


def wardrobe_put(key: str, res: dict) -> None:
    uid = current_user_id()
    t = m.wardrobe_suggestions
    with tx() as c:
        c.execute(delete(t).where(t.c.user_id == uid, t.c.closet_key != key))
        _upsert(c, t, {"user_id": uid, "closet_key": key, "results": json.dumps(res), "created_at": now_iso()},
                ["user_id", "closet_key"])


# ---------------------------------------------------------------- whole-user operations
def delete_all_user_data(uid: str) -> None:
    """Remove every row owned by uid (the users row itself is deleted by app/auth.py; FKs cascade anyway)."""
    with tx() as c:
        for t in m.USER_DATA_TABLES:
            c.execute(delete(t).where(t.c.user_id == uid))


def reset_all() -> None:
    """Drop the current user's closet data (used by seed --reset)."""
    delete_all_user_data(current_user_id())
