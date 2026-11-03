"""One-off: import a legacy single-user SQLite closet (backend/data/fitcheck.db + media/) into the multi-user
database for ONE existing account.

  cd backend && DATABASE_URL=postgres://... python scripts/migrate_sqlite_to_postgres.py \
      --sqlite data/fitcheck.db --media data/media --email you@example.com [--dry-run]

Copies photos, items, embeddings, evaluations, outfits, suggestions, settings and render records (ids kept, so
re-running skips rows already imported), assigns them to the account, copies media into media/u/<user_id>/ and
uploads it to Blob when that's configured. Caches (compat_edges, wardrobe_suggestions) are rebuilt on demand.
"""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402

from app import config, db, media_store  # noqa: E402
from app import models as m  # noqa: E402

TABLES = [  # (sqlite table, target table, path columns)
    ("photos", m.photos, ["path"]),
    ("items", m.items, ["crop_path", "cutout_path", "white_path", "context_path"]),
    ("item_embeddings", m.item_embeddings, []),
    ("evaluations", m.evaluations, []),
    ("outfits", m.outfits, []),
    ("outfit_items", m.outfit_items, []),
    ("suggestions", m.suggestions, []),
    ("item_renders", m.item_renders, ["clean_path"]),
    ("item_render_details", m.item_render_details, []),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--media", required=True, help="the legacy media dir (DATA_DIR/media)")
    ap.add_argument("--email", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    old_media = Path(a.media).resolve()
    db.init_db()
    with db.tx() as c:
        uid = c.execute(select(m.users.c.id).where(m.users.c.email == a.email.strip().lower())).scalar()
    if not uid:
        sys.exit(f"no account for {a.email}: sign up first")
    src = sqlite3.connect(f"file:{a.sqlite}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dest_root = config.MEDIA_DIR / "u" / uid
    copied = 0

    def move(p: str | None) -> str | None:
        nonlocal copied
        if not p:
            return None
        pp = Path(p)
        try:
            rel = pp.resolve().relative_to(old_media).as_posix() if pp.is_absolute() else pp.as_posix()
        except ValueError:
            rel = f"legacy/{pp.name}"
            if not pp.exists():
                return None
        srcf = pp if pp.is_absolute() else old_media / rel
        dst = dest_root / rel
        if srcf.exists() and not a.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, dst)
            copied += 1
        return f"u/{uid}/{rel}"

    with db.as_user(uid):
        for name, table, path_cols in TABLES:
            try:
                rows = [dict(r) for r in src.execute(f"SELECT * FROM {name}")]
            except sqlite3.OperationalError:
                continue
            cols = {c.name for c in table.columns}
            out = []
            for r in rows:
                for pc in path_cols:
                    r[pc] = move(r.get(pc))
                r["user_id"] = uid
                out.append({k: v for k, v in r.items() if k in cols})
            if out and not a.dry_run:
                with db.tx() as c:
                    ins = (__import__("sqlalchemy.dialects.postgresql", fromlist=["insert"]).insert if db.is_postgres()
                           else __import__("sqlalchemy.dialects.sqlite", fromlist=["insert"]).insert)
                    for i in range(0, len(out), 500):
                        c.execute(ins(table).values(out[i:i + 500]).on_conflict_do_nothing())
            print(f"{name}: {len(out)} rows")
        row = src.execute("SELECT data FROM settings LIMIT 1").fetchone()
        if row and not a.dry_run:
            db.update_settings(json.loads(row["data"]))
        if not a.dry_run:
            media_store.sync_user(uid, "legacy import")
    print(f"media files copied: {copied}{' (dry run)' if a.dry_run else ''}")


if __name__ == "__main__":
    main()
