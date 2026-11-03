"""Vercel entrypoint shim (backend service).

All user data lives in Postgres (DATABASE_URL, e.g. Neon via the Vercel integration); media files live under
/tmp/fitcheck/media/u/<user>/ as a per-instance cache and in Vercel Blob (app/media_store.py). The old single-user
SQLite snapshot persistence (app/persist.py, last-write-wins) is retired. Schema migrations (Alembic) run on the
first import of each instance, serialized by a Postgres advisory lock.
Model weights are fetched into backend/models at build time by vercel_build.py.
"""
import os
import time
from pathlib import Path

_t0 = time.time()
HERE = Path(__file__).resolve().parent
RUN = Path(os.environ.get("FITCHECK_DATA_DIR", "/tmp/fitcheck"))
RUN.mkdir(parents=True, exist_ok=True)
os.environ["FITCHECK_DATA_DIR"] = str(RUN)
if not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")):
    raise RuntimeError("DATABASE_URL is not set: connect the Neon Postgres database to this project")
os.environ.setdefault("HF_HOME", str(HERE / "models" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")          # never try to refresh the read-only HF cache
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TORCH_THREADS", "1")           # Hobby = 1 vCPU
os.environ.setdefault("FITCHECK_COMPAT_MODEL_DIR", str(HERE / "models" / "outfit_transformer"))

from app import db  # noqa: E402
db.init_db()
from app.main import app  # noqa: E402,F401

print(f"[vercel_entry] migrated + imported app in {time.time() - _t0:.1f}s", flush=True)
