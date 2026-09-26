"""Lazy, process-wide singletons for the heavy CPU models (loaded once)."""
from __future__ import annotations

import logging
import threading

from . import config  # noqa: F401  (sets HF_HOME before transformers import)

log = logging.getLogger("fitcheck.models")
# One re-entrant lock serializes ALL heavy model loading (segformer, fashion-clip, OutfitTransformer):
# transformers' lazy module imports are not thread-safe ("cannot import name 'CLIPModel' from 'transformers'"
# when the warmup thread and a request thread import concurrently).
LOAD_LOCK = threading.RLock()
_lock = LOAD_LOCK
_cache: dict = {}

# Resolve the lazy transformers symbols once, in the importing thread.
from transformers import (CLIPImageProcessor, CLIPModel, CLIPProcessor, CLIPTextConfig,  # noqa: E402,F401
                          CLIPTextModelWithProjection, CLIPTokenizer, CLIPVisionConfig,
                          CLIPVisionModelWithProjection, SegformerForSemanticSegmentation,
                          SegformerImageProcessor)


def _torch():
    import torch
    torch.set_num_threads(config.TORCH_THREADS)
    return torch


def get_segformer():
    with _lock:
        if "seg" not in _cache:
            _torch()
            from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor
            log.info("Loading segformer %s", config.SEGFORMER_MODEL)
            proc = SegformerImageProcessor.from_pretrained(config.SEGFORMER_MODEL)
            model = SegformerForSemanticSegmentation.from_pretrained(config.SEGFORMER_MODEL).eval()
            _cache["seg"] = (proc, model, threading.Lock())
        return _cache["seg"]


def get_fashion_clip():
    with _lock:
        if "fclip" not in _cache:
            _torch()
            from transformers import CLIPModel, CLIPProcessor
            log.info("Loading fashion-clip %s", config.FASHION_CLIP_MODEL)
            proc = CLIPProcessor.from_pretrained(config.FASHION_CLIP_MODEL)
            model = CLIPModel.from_pretrained(config.FASHION_CLIP_MODEL).eval()
            _cache["fclip"] = (proc, model, threading.Lock())
        return _cache["fclip"]
