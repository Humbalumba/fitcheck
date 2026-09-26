"""FitCheck API (FastAPI). See /workspace/fitcheck/API.md for the contract."""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, gemini, pipeline
from .evaluate import evaluate as run_evaluate
from .scoring import get_scorer, scorer_kind
from .vectors import closet_index

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fitcheck")

app = FastAPI(title="FitCheck API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=".*",  # dev: any origin
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")


def _warmup():
    try:
        db.init_db()
        closet_index.load_or_rebuild()  # loads fashion-clip
        get_scorer()
        from .models_runtime import get_segformer
        get_segformer()
        if gemini.is_configured():
            gemini.model_name()
        log.info("Warmup complete (scorer=%s, gemini=%s)", scorer_kind(), gemini.is_configured())
    except Exception:
        log.exception("Warmup failed")


@app.on_event("startup")
def _startup():
    db.init_db()
    threading.Thread(target=_warmup, daemon=True).start()


# ------------------------------------------------------------------ models
class AddItemsBody(BaseModel):
    item_ids: list[str]
    attributes_overrides: Optional[dict[str, dict[str, Any]]] = None


class PatchBody(BaseModel):
    attributes: dict[str, Any]


class EvaluateBody(BaseModel):
    item_id: str
    price: Optional[float] = None


class SettingsBody(BaseModel):
    compat_threshold: Optional[float] = None
    redundancy_similar_threshold: Optional[float] = None
    redundancy_duplicate_threshold: Optional[float] = None
    redundancy_text_weight: Optional[float] = None
    min_new_outfits: Optional[int] = None
    max_cost_per_outfit: Optional[float] = None
    monthly_budget: Optional[float] = None
    style_goal: Optional[str] = None
    occasions: Optional[list[str]] = None
    match_gender_presentation: Optional[bool] = None
    max_pairs_for_layering: Optional[int] = None
    use_shoes_layer: Optional[bool] = None


def _item_or_404(item_id: str) -> dict:
    it = db.get_item(item_id)
    if it is None:
        raise HTTPException(404, f"item {item_id} not found")
    return it


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return {"ok": True, "gemini": gemini.is_configured(), "scorer": scorer_kind(block=False),
            "gemini_model": gemini._model_name or (config.GEMINI_MODEL if config.GEMINI_MODEL != "auto" else None),
            "gemini_mode": config.GEMINI_MODE,
            "gemini_exhausted": sorted(n for n in gemini._exhausted if not gemini._available(n)),
            "closet_size": len(db.list_items(status="closet"))}


@app.post("/api/detect")
def detect(file: UploadFile = File(...), purpose: Optional[str] = Form(None)):
    if purpose not in (None, "", "closet", "candidate"):
        raise HTTPException(422, "purpose must be 'closet' or 'candidate'")
    data = file.file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        return pipeline.detect(data, purpose or None)
    except Exception as e:
        log.exception("detect failed")
        raise HTTPException(500, f"detection failed: {type(e).__name__}: {e}")


@app.post("/api/closet/items")
def add_closet_items(body: AddItemsBody):
    for iid in body.item_ids:
        _item_or_404(iid)
    items = pipeline.add_to_closet(body.item_ids, body.attributes_overrides)
    return {"items": [pipeline.item_to_api(i) for i in items]}


@app.get("/api/closet/items")
def list_closet(category: Optional[str] = None):
    return {"items": [pipeline.item_to_api(i) for i in db.list_items(status="closet", category=category or None)]}


@app.patch("/api/closet/items/{item_id}")
def patch_item(item_id: str, body: PatchBody):
    _item_or_404(item_id)
    return pipeline.item_to_api(pipeline.update_attributes(item_id, body.attributes))


@app.delete("/api/closet/items/{item_id}")
def delete_item(item_id: str):
    _item_or_404(item_id)
    pipeline.delete_item(item_id)
    return {"ok": True}


@app.get("/api/items/{item_id}")
def get_item(item_id: str):
    return pipeline.item_to_api(_item_or_404(item_id))


@app.post("/api/evaluate")
def evaluate(body: EvaluateBody):
    _item_or_404(body.item_id)
    if body.price is not None and body.price < 0:
        raise HTTPException(422, "price must be >= 0")
    res = run_evaluate(body.item_id, body.price)
    res.pop("settings", None)
    return res


@app.post("/api/candidate/{item_id}/add-to-closet")
def candidate_to_closet(item_id: str):
    _item_or_404(item_id)
    return pipeline.item_to_api(pipeline.add_to_closet([item_id], purchased=True)[0])


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: SettingsBody):
    # exclude_unset: only fields the client sent; monthly_budget may be explicitly null (= no budget cap)
    partial = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "monthly_budget"}
    return db.update_settings(partial)
