"""Lazy, process-wide singletons for the heavy CPU models (loaded once)."""
from __future__ import annotations

import logging
import threading

from . import config  # noqa: F401  (sets HF_HOME before transformers import)

log = logging.getLogger("fitcheck.models")
_lock = threading.Lock()
_cache: dict = {}


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
