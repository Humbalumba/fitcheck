"""Alembic environment: online migrations only, URL from app.db.database_url()."""
from alembic import context
from sqlalchemy import text

from app import models

config = context.config
target_metadata = models.metadata
LOCK_ID = 74_210_331  # pg_advisory_lock key: one migrator at a time across serverless instances


def run() -> None:
    eng = config.attributes.get("engine")
    if eng is None:
        from app.db import engine
        eng = engine()
    with eng.connect() as conn:
        pg = conn.dialect.name == "postgresql"
        if pg:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_ID})
            conn.commit()
        try:
            context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=not pg,
                              compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
        finally:
            if pg:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                conn.commit()


if context.is_offline_mode():
    raise SystemExit("offline mode not supported; set DATABASE_URL and run online")
run()
