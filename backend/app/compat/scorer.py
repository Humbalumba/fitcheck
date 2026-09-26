"""OutfitTransformer outfit-compatibility scorer (CPU-friendly).

Model: OutfitTransformer (Sarkar et al., WACV 2023) as re-implemented by
github.com/owj0421/outfit-transformer (MIT), "CLIP" variant, trained for the
Compatibility Prediction task on Polyvore Outfits (nondisjoint); reported CP AUC 0.95.

Item embedding (1024-d) = [L2norm(fashion-clip image_embeds, 512) ; L2norm(fashion-clip text_embeds, 512)]
exactly as in the original repo (CLIPItemEncoder, concat aggregation). The frozen fashion-clip
weights come from the same checkpoint. The outfit is fed to a 6-layer transformer encoder with a
learned task token prepended; a sigmoid head on the task-token output gives P(compatible).
No hand-written matching rules anywhere.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Sequence

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = Path(os.environ.get("FITCHECK_COMPAT_MODEL_DIR",
                                        BACKEND_DIR / "models" / "outfit_transformer"))
EMBED_DIM = 1024
DEFAULT_THRESHOLD = 0.5  # only sensible for 4+ item outfits; use threshold_for(n_items). See README.md
# Balanced-accuracy-optimal thresholds measured on the real Polyvore test split (real sub-outfits
# vs random item combos). Scores are strongly length-dependent: real 2-item top+bottom pairs average ~0.35.
LENGTH_THRESHOLDS = {1: 0.5, 2: 0.15, 3: 0.35}
LONG_OUTFIT_THRESHOLD = 0.6


def threshold_for(n_items: int) -> float:
    """Recommended default decision threshold for an outfit with n_items items."""
    return LENGTH_THRESHOLDS.get(n_items, LONG_OUTFIT_THRESHOLD)


class _OutfitTransformerHead(nn.Module):
    """Mirror of owj0421 OutfitTransformer minus the item encoder (param names match checkpoint)."""

    def __init__(self, cfg: dict, d_model: int = EMBED_DIM):
        super().__init__()
        self.d_model = d_model
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=cfg["transformer_n_head"], dim_feedforward=cfg["transformer_d_ffn"],
            dropout=cfg["transformer_dropout"], batch_first=True, norm_first=True, activation=F.mish)
        self.style_enc = nn.TransformerEncoder(layer, num_layers=cfg["transformer_n_layers"],
                                               enable_nested_tensor=False)
        self.predict_ffn = nn.Sequential(nn.Dropout(cfg["transformer_dropout"]), nn.Linear(d_model, 1), nn.Sigmoid())
        self.embed_ffn = nn.Sequential(nn.Linear(d_model, cfg["d_embed"], bias=False))
        self.task_emb = nn.Parameter(torch.zeros(d_model // 2))
        self.predict_emb = nn.Parameter(torch.zeros(d_model // 2))
        self.embed_emb = nn.Parameter(torch.zeros(d_model // 2))
        self.pad_emb = nn.Parameter(torch.zeros(d_model))

    @staticmethod
    def _norm_halves(x: torch.Tensor) -> torch.Tensor:
        h = x.shape[-1] // 2
        return torch.cat([F.normalize(x[..., :h], dim=-1), F.normalize(x[..., h:], dim=-1)], dim=-1)

    def forward(self, embs: torch.Tensor, pad_mask: torch.Tensor) -> torch.Tensor:
        """embs [B,L,D] (padding rows arbitrary), pad_mask [B,L] True=pad -> scores [B]."""
        B = embs.shape[0]
        embs = torch.where(pad_mask.unsqueeze(-1), self.pad_emb.view(1, 1, -1), embs)
        task = torch.cat([self.task_emb, self.predict_emb]).view(1, 1, -1).expand(B, 1, -1)
        x = torch.cat([task, embs], dim=1)
        mask = torch.cat([torch.zeros(B, 1, dtype=torch.bool, device=embs.device), pad_mask], dim=1)
        h = self.style_enc(self._norm_halves(x), src_key_padding_mask=mask)
        return self.predict_ffn(h[:, 0, :]).squeeze(-1)


class CompatibilityScorer:
    """Load once, reuse. Thread-safe (inference guarded by a lock)."""

    def __init__(self, device: str = "cpu", model_dir: str | os.PathLike | None = None,
                 num_threads: int | None = None):
        from safetensors.torch import load_file
        from transformers import (CLIPImageProcessor, CLIPTextConfig, CLIPTextModelWithProjection,
                                  CLIPTokenizer, CLIPVisionConfig, CLIPVisionModelWithProjection)

        if num_threads:
            torch.set_num_threads(num_threads)
        self.device = torch.device(device)
        mdir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        if not (mdir / "head.safetensors").exists():
            raise FileNotFoundError(f"{mdir}/head.safetensors missing - run `python -m app.compat.prepare_weights`")
        clip_dir = mdir / "fashion-clip"
        self.max_items = 16
        cfg = json.loads((mdir / "head_config.json").read_text())
        self.max_items = cfg.get("max_length", 16)

        self.vision = CLIPVisionModelWithProjection(CLIPVisionConfig.from_pretrained(clip_dir))
        self.vision.load_state_dict(load_file(str(mdir / "clip_vision.safetensors")), strict=True)
        self.text = CLIPTextModelWithProjection(CLIPTextConfig.from_pretrained(clip_dir))
        self.text.load_state_dict(load_file(str(mdir / "clip_text.safetensors")), strict=True)
        self.head = _OutfitTransformerHead(cfg)
        missing, unexpected = self.head.load_state_dict(load_file(str(mdir / "head.safetensors")), strict=True)
        for m in (self.vision, self.text, self.head):
            m.to(self.device).eval().requires_grad_(False)
        self.processor = CLIPImageProcessor.from_pretrained(clip_dir)
        self.tokenizer = CLIPTokenizer.from_pretrained(clip_dir)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ items
    @staticmethod
    def _load_rgb(image) -> Image.Image:
        img = image if isinstance(image, Image.Image) else Image.open(image)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))  # Polyvore items are on white
            bg.paste(img, mask=img.split()[-1])
            return bg
        return img.convert("RGB")

    @torch.inference_mode()
    def embed_items(self, images: Sequence, texts: Sequence[str]) -> np.ndarray:
        """Batch version. images: paths or PIL images. Returns float32 [N,1024]."""
        assert len(images) == len(texts)
        pil = [self._load_rgb(i) for i in images]
        with self._lock:
            pix = self.processor(images=pil, return_tensors="pt")["pixel_values"].to(self.device)
            img = self.vision(pixel_values=pix).image_embeds
            tok = self.tokenizer(text=[t or "" for t in texts], max_length=64, padding="max_length",
                                 truncation=True, return_tensors="pt").to(self.device)
            txt = self.text(**tok).text_embeds
        out = torch.cat([F.normalize(img, dim=-1), F.normalize(txt, dim=-1)], dim=-1)
        return out.cpu().numpy().astype(np.float32)

    def embed_item(self, image_path: str, text: str) -> np.ndarray:
        """1024-d float32 item embedding (cache this per wardrobe item)."""
        return self.embed_items([image_path], [text])[0]

    # ---------------------------------------------------------------- outfits
    @torch.inference_mode()
    def score_outfits(self, outfits: list[list[np.ndarray]], batch_size: int = 256) -> list[float]:
        """Each outfit = list of item embeddings (2..16 items). Returns P(compatible) in [0,1]."""
        if not outfits:
            return []
        scores: list[float] = []
        for s in range(0, len(outfits), batch_size):
            chunk = outfits[s:s + batch_size]
            L = min(self.max_items, max(len(o) for o in chunk))
            embs = np.zeros((len(chunk), L, EMBED_DIM), dtype=np.float32)
            pad = np.ones((len(chunk), L), dtype=bool)
            for i, o in enumerate(chunk):
                o = o[:L]
                if len(o):
                    embs[i, :len(o)] = np.stack([np.asarray(e, dtype=np.float32).reshape(-1) for e in o])
                pad[i, :len(o)] = False
            with self._lock:
                out = self.head(torch.from_numpy(embs).to(self.device), torch.from_numpy(pad).to(self.device))
            scores.extend(out.float().cpu().tolist())
        return scores


_SINGLETON: CompatibilityScorer | None = None
_SINGLETON_LOCK = threading.Lock()


def get_scorer(device: str = "cpu") -> CompatibilityScorer:
    """Process-wide lazily created scorer (avoid loading the ~800MB model twice)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = CompatibilityScorer(device=device)
        return _SINGLETON
