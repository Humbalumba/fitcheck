"""fashion-clip embeddings + FAISS closet index (cosine via inner product on L2-normalized vectors)."""
from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

from . import config, db
from .models_runtime import get_fashion_clip

log = logging.getLogger("fitcheck.vectors")
FCLIP_KIND = "fclip"
FCLIP_DIM = 512


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.clip(n, 1e-8, None)


def embed_images(images: list[Image.Image]) -> np.ndarray:
    proc, model, mlock = get_fashion_clip()
    import torch
    with mlock, torch.inference_mode():
        inputs = proc(images=[im.convert("RGB") for im in images], return_tensors="pt")
        feats = model.get_image_features(**inputs)
    return _norm(feats.numpy())


def embed_texts(texts: list[str]) -> np.ndarray:
    proc, model, mlock = get_fashion_clip()
    import torch
    with mlock, torch.inference_mode():
        inputs = proc(text=texts, return_tensors="pt", padding=True, truncation=True, max_length=77)
        feats = model.get_text_features(**inputs)
    return _norm(feats.numpy())


def embed_image_path(path: str) -> np.ndarray:
    with Image.open(path) as im:
        return embed_images([im])[0]


def ensure_fclip(item: dict) -> np.ndarray:
    """Get (or compute + store) the fashion-clip embedding of an item's white-background cutout."""
    v = db.get_embedding(item["id"], FCLIP_KIND, config.FASHION_CLIP_MODEL)
    if v is None:
        v = embed_image_path(item["white_path"] or item["cutout_path"] or item["crop_path"])
        db.put_embedding(item["id"], FCLIP_KIND, config.FASHION_CLIP_MODEL, v)
    return v


class ClosetIndex:
    """IndexIDMap(IndexFlatIP) over closet items; int64 ids map to item ids via a side table."""

    def __init__(self):
        self._lock = threading.RLock()
        self.index = None
        self.id_map: dict[int, str] = {}

    def _new(self):
        import faiss
        return faiss.IndexIDMap(faiss.IndexFlatIP(FCLIP_DIM))

    def load_or_rebuild(self):
        import faiss
        with self._lock:
            items = db.list_items(status="closet")
            ids = [it["id"] for it in items]
            path_map = config.FAISS_PATH.with_suffix(".ids.npy")
            if config.FAISS_PATH.exists() and path_map.exists():
                try:
                    idx = faiss.read_index(str(config.FAISS_PATH))
                    arr = np.load(path_map, allow_pickle=True)
                    id_map = {int(i): str(s) for i, s in arr}
                    if sorted(id_map.values()) == sorted(ids) and idx.ntotal == len(ids):
                        self.index, self.id_map = idx, id_map
                        log.info("Loaded FAISS index with %d closet items", idx.ntotal)
                        return
                except Exception as e:  # corrupt / stale -> rebuild
                    log.warning("FAISS load failed (%s); rebuilding", e)
            self.rebuild(items)

    def rebuild(self, items: list[dict] | None = None):
        with self._lock:
            items = items if items is not None else db.list_items(status="closet")
            self.index, self.id_map = self._new(), {}
            vecs, fids = [], []
            for n, it in enumerate(items):
                vecs.append(ensure_fclip(it))
                fids.append(n + 1)
                self.id_map[n + 1] = it["id"]
            if vecs:
                self.index.add_with_ids(np.stack(vecs).astype(np.float32), np.array(fids, dtype=np.int64))
            self.save()
            log.info("Rebuilt FAISS index with %d closet items", len(vecs))

    def save(self):
        import faiss
        with self._lock:
            faiss.write_index(self.index, str(config.FAISS_PATH))
            np.save(config.FAISS_PATH.with_suffix(".ids.npy"),
                    np.array(list(self.id_map.items()), dtype=object), allow_pickle=True)

    def add(self, item: dict):
        with self._lock:
            if self.index is None:
                self.load_or_rebuild()
            if item["id"] in self.id_map.values():
                return
            v = ensure_fclip(item)
            fid = (max(self.id_map) + 1) if self.id_map else 1
            self.index.add_with_ids(v[None, :].astype(np.float32), np.array([fid], dtype=np.int64))
            self.id_map[fid] = item["id"]
            self.save()

    def remove(self, item_id: str):
        with self._lock:
            if self.index is None:
                return
            fids = [f for f, s in self.id_map.items() if s == item_id]
            if fids:
                self.index.remove_ids(np.array(fids, dtype=np.int64))
                for f in fids:
                    del self.id_map[f]
                self.save()

    def search(self, vec: np.ndarray, k: int | None = None) -> list[tuple[str, float]]:
        with self._lock:
            if self.index is None:
                self.load_or_rebuild()
            if self.index.ntotal == 0:
                return []
            k = min(k or self.index.ntotal, self.index.ntotal)
            D, I = self.index.search(_norm(vec)[None, :].astype(np.float32), k)
            return [(self.id_map[int(i)], float(d)) for d, i in zip(D[0], I[0]) if int(i) in self.id_map]


closet_index = ClosetIndex()
