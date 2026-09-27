"""Vercel entrypoint shim (backend service). Does not modify backend/app.

Vercel's deployment filesystem is read-only except /tmp, so on the first import in each
function instance we copy the bundled seed snapshot (seed_data/: SQLite DB, FAISS index, media)
into /tmp/fitcheck and point app/config.py at it via FITCHECK_DATA_DIR. Writes (new closet items,
buy checks) live only as long as that instance - demo-grade persistence.
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
if not (RUN / "fitcheck.db").exists() and SEED.exists():
    shutil.copytree(SEED, RUN, dirs_exist_ok=True)
os.environ["FITCHECK_DATA_DIR"] = str(RUN)
os.environ.setdefault("HF_HOME", str(HERE / "models" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")          # never try to refresh the read-only HF cache
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TORCH_THREADS", "1")           # Hobby = 1 vCPU
os.environ.setdefault("FITCHECK_COMPAT_MODEL_DIR", str(HERE / "models" / "outfit_transformer"))

from app.main import app  # noqa: E402,F401

print(f"[vercel_entry] seeded {RUN} and imported app in {time.time() - _t0:.1f}s", flush=True)
