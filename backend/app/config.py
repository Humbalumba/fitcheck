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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "auto")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "60"))
GEMINI_MAX_PARALLEL = int(os.environ.get("GEMINI_MAX_PARALLEL", "6"))

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
for sub in ("originals", "crops", "cutouts", "white", "context"):
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
