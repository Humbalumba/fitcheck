"""Tests run against a *copy* of the seeded demo DB + FAISS index so the running server isn't touched.

Source data dir: $FITCHECK_TEST_DATA_DIR, else ../../fitcheck-testdata (sibling of the repo) if it exists, else
backend/data. The live closet may be the user's real (small) closet, so seed a scratch dir for tests:
  FITCHECK_DATA_DIR=/path/fitcheck-testdata python scripts/seed_demo_closet.py --reset
Gemini is switched off (FITCHECK_GEMINI_OFF=1) so tests never spend quota; FITCHECK_TEST_GEMINI=1 re-enables it."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
_tmp = Path(tempfile.mkdtemp(prefix="fitcheck_test_"))
import sqlite3  # noqa: E402

_sibling = BACKEND.parent.parent / "fitcheck-testdata"
SRC = Path(os.environ.get("FITCHECK_TEST_DATA_DIR")
           or (_sibling if (_sibling / "fitcheck.db").exists() else BACKEND / "data"))
if (SRC / "fitcheck.db").exists():  # consistent snapshot incl. WAL contents
    src = sqlite3.connect(SRC / "fitcheck.db")
    dst = sqlite3.connect(_tmp / "fitcheck.db")
    src.backup(dst)
    dst.close(); src.close()
for name in ("closet.faiss", "closet.ids.npy"):
    if (SRC / name).exists():
        shutil.copy(SRC / name, _tmp / name)
os.environ["FITCHECK_DATA_DIR"] = str(SRC)  # media paths of the seeded items live under SRC/media
os.environ["FITCHECK_DB"] = str(_tmp / "fitcheck.db")
os.environ["FITCHECK_FAISS"] = str(_tmp / "closet.faiss")
os.environ.setdefault("FITCHECK_PRICE_BACKFILL", "0")  # no startup backfill thread racing the tests (tested directly)
# same local model cache as run.sh, and never reach out to the Hugging Face hub from tests (offline + fast)
os.environ.setdefault("HF_HOME", str(BACKEND / "models" / "hf"))
if (Path(os.environ["HF_HOME"]) / "hub").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
if os.environ.get("FITCHECK_TEST_GEMINI") != "1":
    os.environ["FITCHECK_GEMINI_OFF"] = "1"


@pytest.fixture(scope="session")
def client():
    if not (_tmp / "fitcheck.db").exists():
        pytest.skip("demo closet not seeded: run scripts/seed_demo_closet.py first")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def candidates():
    from app import db
    return {c["attributes"]["test_key"]: c["id"] for c in db.list_items(status="candidate")
            if c["attributes"].get("test_key")}
