"""Compatibility scorer loader.

Uses the real OutfitTransformer module (app.compat.CompatibilityScorer, built separately) when it
imports and constructs cleanly; otherwise falls back to a clearly-labelled TEMPORARY stub with the
same interface. Also wraps caching of item compat-embeddings (item_embeddings, kind='compat') and
outfit scores (compat_edges).
"""
from __future__ import annotations

import hashlib
import logging
import threading

import numpy as np

from . import db

log = logging.getLogger("fitcheck.scoring")
COMPAT_KIND = "compat"


class StubCompatibilityScorer:
    """TEMPORARY placeholder until app.compat (OutfitTransformer) lands. NOT a real compatibility model.

    embed_item: fashion-clip image embedding blended with its text embedding.
    score_outfits: mean pairwise cosine of item embeddings squashed through a logistic, plus a small
    deterministic hash jitter so ties break stably. No hand-written color/formality rules.
    """

    name = "stub"
    model_id = "stub-fclip-cosine-v1"
    # logistic calibration: on the demo closet ~1/3 of top+bottom pairs clear the 0.5 default threshold
    CENTER = 0.62
    TAU = 0.05

    def __init__(self, device: str = "cpu"):
        self.device = device

    def embed_item(self, image_path: str, text: str) -> np.ndarray:
        from .vectors import embed_image_path, embed_texts
        img = embed_image_path(image_path)
        if text:
            v = 0.8 * img + 0.2 * embed_texts([text])[0]
        else:
            v = img
        return (v / (np.linalg.norm(v) + 1e-8)).astype(np.float32)

    def score_outfits(self, outfits: list[list[np.ndarray]]) -> list[float]:
        out = []
        for items in outfits:
            if len(items) < 2:
                out.append(0.75)
                continue
            M = np.stack([np.asarray(v, dtype=np.float32) for v in items])
            M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
            S = M @ M.T
            iu = np.triu_indices(len(items), 1)
            c = float(S[iu].mean())
            h = hashlib.md5(np.round(M.sum(0), 3).tobytes()).digest()
            jitter = (h[0] / 255.0 - 0.5) * 0.04
            out.append(float(np.clip(1 / (1 + np.exp(-(c - self.CENTER) / self.TAU)) + jitter, 0, 1)))
        return out


# Import the compat module eagerly in the importing (main) thread: importing torch-heavy modules lazily from
# the warmup thread while a request thread imports other torch/transformers modules can race
# ("partially initialized module") and silently drop us to the stub.
try:
    import os as _os
    if _os.environ.get("FITCHECK_FORCE_STUB") == "1":
        raise ImportError("FITCHECK_FORCE_STUB=1")
    import app.compat as _compat_mod
    _compat_err = None
except Exception as _e:  # noqa: BLE001
    _compat_mod, _compat_err = None, _e

_lock = threading.Lock()
_scorer = None
_kind = None  # "outfit_transformer" | "stub"
_model_id = None


def get_scorer():
    global _scorer, _kind, _model_id
    with _lock:
        if _scorer is None:
            try:
                if _compat_mod is None:
                    raise _compat_err
                from .models_runtime import LOAD_LOCK
                with LOAD_LOCK:
                    _scorer = _compat_mod.get_scorer(device="cpu")  # OutfitTransformer (app/compat) singleton
                _kind = "outfit_transformer"
                _model_id = "outfit_transformer-clip-cp-v1"
                log.info("Using OutfitTransformer compatibility scorer (%s)", _model_id)
            except Exception as e:  # ImportError or construction failure (missing weights...)
                log.warning("!!! app.compat.CompatibilityScorer unavailable (%s: %s) -> using TEMPORARY STUB scorer. "
                            "Outfit scores are placeholders.", type(e).__name__, e)
                _scorer = StubCompatibilityScorer(device="cpu")
                _kind = "stub"
                _model_id = StubCompatibilityScorer.model_id
        return _scorer


def scorer_kind(block: bool = True) -> str:
    """'outfit_transformer' | 'stub'. With block=False (health checks during warmup) predict without loading."""
    if _kind is None and not block:
        return "outfit_transformer" if _compat_mod is not None else "stub"
    get_scorer()
    return _kind


def scorer_model_id() -> str:
    get_scorer()
    return _model_id


def item_text(item: dict) -> str:
    """Short lowercase product-name style text for the compat model, e.g. 'navy striped cotton t-shirt'.
    (OutfitTransformer was trained on Polyvore url_name strings like 'floral jacquard trousers'.)"""
    a = item.get("attributes") or {}
    color = (a.get("primary_color") or "").lower()
    pattern = (a.get("pattern") or "").lower()
    if pattern in ("solid", "plain", "none", "unknown"):
        pattern = ""
    fabric = (a.get("fabric_guess") or "").lower()
    if fabric in ("unknown", "none", "n/a"):
        fabric = ""
    fabric = " ".join(fabric.split()[:2])
    sub = (a.get("subcategory") or a.get("category") or item.get("category") or "").lower()
    words = []
    for w in " ".join([color, pattern, fabric, sub]).split():
        if w not in words:
            words.append(w)
    return " ".join(words) or (item.get("label") or "clothing item").lower()


# ---------------------------------------------------------------- calibration
# Raw OutfitTransformer scores are NOT comparable across outfit sizes (Polyvore: real top+bottom pairs
# average ~0.35, random ~0.15; real 3-item sets ~0.60; full outfits ~0.80). We map raw -> calibrated with a
# per-size piecewise-linear curve through the knots (0 -> 0), (balanced cutoff t_n -> 0.5), (1 -> 1),
# where t_n = app.compat.threshold_for(n) (2: 0.15, 3: 0.35, 4+: 0.6).
# The user-facing compat_threshold s (0..1 "match strictness") keeps an outfit iff calibrated >= s,
# i.e. raw >= cutoff_n(s): s=0.5 -> exactly threshold_for(n); s=0 -> everything passes; s=1 -> only raw==1.
# (An earlier variant used upper knots < 1; on the demo closet that saturated most 3-4 item outfits at 1.0.)
CALIB_HIGH = {}
CALIB_HIGH_LONG = 1.0


def balanced_cutoff(n: int) -> float:
    if scorer_kind() == "outfit_transformer":
        try:
            return float(_compat_mod.threshold_for(n))
        except Exception:
            pass
        return {1: 0.5, 2: 0.15, 3: 0.35}.get(n, 0.6)
    return 0.5  # stub scores are already size-independent


def _high(n: int) -> float:
    return CALIB_HIGH.get(n, CALIB_HIGH_LONG)


def calibrate(raw: float, n: int) -> float:
    t, h = balanced_cutoff(n), _high(n)
    if raw <= t:
        c = 0.5 * raw / t if t > 0 else 0.5
    else:
        c = 0.5 + 0.5 * (raw - t) / (h - t)
    return float(min(1.0, max(0.0, c)))


def raw_cutoff(strictness: float, n: int) -> float:
    """Inverse of calibrate(): raw score needed for an n-item outfit at a given strictness."""
    t, h = balanced_cutoff(n), _high(n)
    s = min(1.0, max(0.0, float(strictness)))
    return s / 0.5 * t if s <= 0.5 else t + (s - 0.5) / 0.5 * (h - t)


def calibration_table(strictness: float) -> dict:
    return {str(n) if n < 4 else "4+": round(raw_cutoff(strictness, n), 4) for n in (2, 3, 4)}


def ensure_compat_embeddings(items: list[dict]) -> dict[str, np.ndarray]:
    scorer = get_scorer()
    mid = scorer_model_id()
    have = db.get_embeddings([i["id"] for i in items], COMPAT_KIND, mid)
    for it in items:
        if it["id"] not in have:
            from .render import embedding_image_path  # verified Gemini render if any, else the cutout on white
            path = embedding_image_path(it) or it.get("white_path") or it.get("cutout_path") or it.get("crop_path")
            v = np.asarray(scorer.embed_item(path, item_text(it)), dtype=np.float32)
            db.put_embedding(it["id"], COMPAT_KIND, mid, v)
            have[it["id"]] = v
    return have


def score_outfits_cached(outfits: list[list[str]], emb: dict[str, np.ndarray], batch: int = 128) -> list[float]:
    """Score outfits (lists of item ids) with caching in compat_edges."""
    mid = scorer_model_id()
    keys = [db.outfit_key(o) for o in outfits]
    cached = db.get_compat_scores(list(set(keys)), mid)
    todo = [(k, o) for k, o in zip(keys, outfits) if k not in cached]
    seen = set()
    todo = [(k, o) for k, o in todo if not (k in seen or seen.add(k))]
    if todo:
        scorer = get_scorer()
        new_entries = []
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            scores = scorer.score_outfits([[emb[iid] for iid in o] for _, o in chunk])
            for (k, o), s in zip(chunk, scores):
                cached[k] = float(s)
                new_entries.append((o, float(s)))
        db.put_compat_scores(new_entries, mid)
    return [cached[k] for k in keys]
