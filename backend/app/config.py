"""Central configuration. Everything overridable via environment variables."""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FITCHECK_DATA_DIR", BACKEND_DIR / "data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = Path(os.environ.get("FITCHECK_DB", DATA_DIR / "fitcheck.db"))
FAISS_PATH = Path(os.environ.get("FITCHECK_FAISS", DATA_DIR / "closet.faiss"))
TEST_IMAGES_DIR = DATA_DIR / "test_images"
MODELS_DIR = BACKEND_DIR / "models"

# Keep HF weights inside backend/models (gitignored) unless the user set HF_HOME.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))

# --- Gemini -----------------------------------------------------------------
# "auto" = list models via the API and pick the newest stable-ish Flash model.
# Default chosen by scripts/test_gemini.py comparison (Sep 2026): gemini-3-flash-preview gives the same tight
# boxes as the newest gemini-3.8-flash but is 2-4x faster (supports thinking_level=minimal) and less often
# overloaded. gemini-2.5-flash returns 404 for new API keys. "auto" = newest Flash first.
# Whatever is first, the rest of the Flash models form a failover chain (free tier: 20 requests/day/model).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
# gemini-2.5-flash now returns 404 "no longer available to new users" for new keys; the alias always resolves.
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-latest")
GEMINI_RETRIES = int(os.environ.get("GEMINI_RETRIES", "2"))
GEMINI_CHAIN_MAX = int(os.environ.get("GEMINI_CHAIN_MAX", "6"))  # how many models to fail over through
# single: one call per photo returns boxes + attributes; two_stage: detect boxes, then one identify call per item
GEMINI_MODE = os.environ.get("GEMINI_MODE", "single")
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "60"))
GEMINI_MAX_PARALLEL = int(os.environ.get("GEMINI_MAX_PARALLEL", "3"))

# --- Models -----------------------------------------------------------------
SEGFORMER_MODEL = os.environ.get("SEGFORMER_MODEL", "mattmdjaga/segformer_b2_clothes")
FASHION_CLIP_MODEL = os.environ.get("FASHION_CLIP_MODEL", "patrickjohncyh/fashion-clip")
TORCH_THREADS = int(os.environ.get("TORCH_THREADS", "4"))

# --- Image processing -------------------------------------------------------
MAX_IMAGE_SIDE = int(os.environ.get("MAX_IMAGE_SIDE", "1600"))
CROP_PAD_FRAC = float(os.environ.get("CROP_PAD_FRAC", "0.06"))

# --- Default user settings (stored in the settings table, editable via API) --
DEFAULT_SETTINGS = {
    "compat_threshold": 0.5,  # "match strictness" 0..1; 0.5 == balanced per-size cutoff
    "redundancy_similar_threshold": 0.80,
    "redundancy_duplicate_threshold": 0.88,
    "redundancy_text_weight": 0.3,  # blend: (1-w)*image cosine + w*attribute-text cosine
    "min_new_outfits": 3,
    "max_cost_per_outfit": 10.0,
    "monthly_budget": 200.0,
    "style_goal": "",
    "occasions": [],
    # extra (not in original contract, backwards compatible):
    "match_gender_presentation": True,  # don't pair mens-only with womens-only items
    "max_pairs_for_layering": 40,       # cap on top+bottom pairs used for outerwear search
    "use_shoes_layer": True,            # complete each look with the best closet shoe (if any)
}

CATEGORIES = ["top", "bottom", "dress", "outerwear", "shoes", "accessory"]

for d in (DATA_DIR, MEDIA_DIR, TEST_IMAGES_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)
for sub in ("originals", "crops", "cutouts", "white", "context", "suggest"):
    (MEDIA_DIR / sub).mkdir(parents=True, exist_ok=True)


def media_url(path: str | Path | None) -> str | None:
    """Absolute filesystem path under MEDIA_DIR -> '/media/...' URL."""
    if not path:
        return None
    p = Path(path)
    try:
        rel = p.resolve().relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return None
    return "/media/" + rel.as_posix()
