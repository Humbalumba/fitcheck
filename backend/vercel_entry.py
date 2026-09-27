"""Vercel entrypoint shim (backend service). Does not modify backend/app.

Vercel's deployment filesystem is read-only except /tmp, so on the first import in each
function instance we copy the bundled seed snapshot (seed_data/: SQLite DB, FAISS index, media)
into /tmp/fitcheck and point app/config.py at it via FITCHECK_DATA_DIR - unless app/persist.py finds a saved
state in Vercel Blob (BLOB_READ_WRITE_TOKEN), in which case that is restored instead. Every successful write is
saved back to Blob (middleware in app/main.py), so the closet survives cold starts.
Model weights are fetched into backend/models at build time by vercel_build.py.
"""
import os
import shutil
import time
from pathlib import Path

_t0 = time.time()
HERE = Path(__file__).resolve().parent
SEED = HERE / "seed_data"
RUN = Path(os.environ.get("FITCHECK_DATA_DIR", "/tmp/fitcheck"))
USER_TABLES = ("outfit_items", "outfits", "compat_edges", "item_embeddings", "item_render_details", "item_renders",
               "suggestions", "wardrobe_suggestions", "evaluations", "items", "photos")


def _start_empty(run: Path) -> None:
    """Fresh instance = empty closet (keeps only the saved settings / profile). FITCHECK_START_EMPTY=0 keeps the seed."""
    import sqlite3
    c = sqlite3.connect(run / "fitcheck.db")
    c.execute("PRAGMA foreign_keys=OFF")
    for t in USER_TABLES:
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    c.close()
    for f in run.glob("closet.*"):
        f.unlink()
    shutil.rmtree(run / "media", ignore_errors=True)
    (run / "media").mkdir(exist_ok=True)


os.environ["FITCHECK_DATA_DIR"] = str(RUN)
_restored = False
if not (RUN / "fitcheck.db").exists():
    from app import persist  # stdlib only; no-op unless BLOB_READ_WRITE_TOKEN is set (app/persist.py)
    _restored = persist.restore(RUN)  # the closet saved by earlier instances, if any
    if not _restored and SEED.exists():
        shutil.copytree(SEED, RUN, dirs_exist_ok=True)
        if os.environ.get("FITCHECK_START_EMPTY", "1") != "0":
            _start_empty(RUN)
os.environ.setdefault("HF_HOME", str(HERE / "models" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")          # never try to refresh the read-only HF cache
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TORCH_THREADS", "1")           # Hobby = 1 vCPU
os.environ.setdefault("FITCHECK_COMPAT_MODEL_DIR", str(HERE / "models" / "outfit_transformer"))

from app.main import app  # noqa: E402,F401

print(f"[vercel_entry] {'restored' if _restored else 'seeded'} {RUN} and imported app in {time.time() - _t0:.1f}s", flush=True)
