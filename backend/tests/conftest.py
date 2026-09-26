"""Tests run against a *copy* of the seeded demo DB + FAISS index so the running server isn't touched."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
_tmp = Path(tempfile.mkdtemp(prefix="fitcheck_test_"))
for name in ("fitcheck.db", "closet.faiss", "closet.ids.npy"):
    src = BACKEND / "data" / name
    if src.exists():
        shutil.copy(src, _tmp / name)
os.environ["FITCHECK_DB"] = str(_tmp / "fitcheck.db")
os.environ["FITCHECK_FAISS"] = str(_tmp / "closet.faiss")


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
