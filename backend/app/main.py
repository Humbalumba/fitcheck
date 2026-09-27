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

from . import config, db, gemini, pipeline, suggest, wardrobe_suggest
from .evaluate import evaluate as run_evaluate, sustainability_for, with_current_verdict, with_sustainability
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
        wardrobe_suggest.warm()  # "Worth a look" picks for the current closet, in the background if not cached
    except Exception:
        log.exception("Warmup failed")


@app.on_event("startup")
def _startup():
    db.init_db()
    threading.Thread(target=_warmup, daemon=True).start()
    from .pricing import start_background_backfill
    start_background_backfill()  # price estimates for old unpriced items (1 batched Gemini text call, else table)


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
            "render": _render_status(),
            "closet_size": len(db.list_items(status="closet"))}


def _render_status() -> dict | None:
    try:
        from . import render
        st = dict(render.gemini_status())
        st["template_enabled"] = render.RENDER_TEMPLATE
        st["priority"] = "gemini > template > cleanup"
        return st
    except Exception:
        return None


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
    try:  # accessories are refused (FitCheck handles clothes and shoes only)
        items = pipeline.add_to_closet(body.item_ids, body.attributes_overrides)
    except pipeline.AccessoryNotAllowed as e:
        raise HTTPException(422, str(e))
    wardrobe_suggest.closet_changed()
    return {"items": [pipeline.item_to_api(i) for i in items]}


@app.get("/api/closet/items")
def list_closet(category: Optional[str] = None):
    return {"items": [pipeline.item_to_api(i) for i in db.list_items(status="closet", category=category or None)]}


@app.patch("/api/closet/items/{item_id}")
def patch_item(item_id: str, body: PatchBody):
    _item_or_404(item_id)
    try:  # category can't become 'accessory' (or anything outside top/bottom/dress/outerwear/shoes)
        out = pipeline.item_to_api(pipeline.update_attributes(item_id, body.attributes))
        wardrobe_suggest.closet_changed()
        return out
    except pipeline.AccessoryNotAllowed as e:
        raise HTTPException(422, str(e))


@app.delete("/api/closet/items/{item_id}")
def delete_item(item_id: str):
    _item_or_404(item_id)
    pipeline.delete_item(item_id)
    wardrobe_suggest.closet_changed()
    return {"ok": True}


@app.get("/api/items/{item_id}")
def get_item(item_id: str):
    return pipeline.item_to_api(_item_or_404(item_id))


class RenderBody(BaseModel):
    mode: str = "auto"   # auto (Gemini redraw if available + verified, else template, else cleanup) | gemini | template | cleanup
    wait: bool = False   # true: render synchronously and return the finished item


def _render_mode(mode: str) -> str:
    if mode not in ("auto", "gemini", "template", "cleanup"):
        raise HTTPException(422, "mode must be auto, gemini, template or cleanup")
    return mode


@app.post("/api/items/{item_id}/render")
def render_item(item_id: str, body: Optional[RenderBody] = None):
    """(Re-)create the item's clean product image (app/render.py). Background by default: the response has
    render_status='pending'; poll GET /api/items/{id} until it is 'done' (clean_image_url set) or 'failed'."""
    from . import render
    _item_or_404(item_id)
    body = body or RenderBody()
    mode = _render_mode(body.mode)
    if body.wait:
        try:
            render.render_item(item_id, mode)
        except Exception as e:
            log.exception("render failed")
            raise HTTPException(500, f"render failed: {type(e).__name__}: {e}")
    else:
        render.schedule([item_id], mode)
    return pipeline.item_to_api(_item_or_404(item_id))


@app.post("/api/closet/render")
def render_closet(body: Optional[RenderBody] = None, only_missing: bool = False):
    """Queue clean-image renders for every closet item (only_missing=true: items without a clean image yet)."""
    from . import render
    mode = _render_mode((body or RenderBody()).mode)
    ids = [it["id"] for it in db.list_items(status="closet")
           if not only_missing or not render.api_fields(it)["clean_image_url"]]
    render.schedule(ids, mode)
    return {"queued": ids}


@app.post("/api/evaluate")
def evaluate(body: EvaluateBody):
    _item_or_404(body.item_id)
    if body.price is not None and body.price < 0:
        raise HTTPException(422, "price must be >= 0")
    res = run_evaluate(body.item_id, body.price)
    res.pop("settings", None)
    return res


@app.get("/api/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str):
    """A saved evaluation (same shape as POST /api/evaluate). Evaluations saved before the sustainability score or
    the BUY / CONSIDER / SKIP score existed get them computed on read (pure Python, no model calls)."""
    ev = db.get_evaluation(evaluation_id)
    if ev is None:
        raise HTTPException(404, f"evaluation {evaluation_id} not found")
    res = with_current_verdict(with_sustainability(ev["results"]))
    res = {k: v for k, v in res.items() if k != "settings"}
    return {**res, "evaluation_id": evaluation_id, "created_at": ev.get("created_at")}


@app.get("/api/items/{item_id}/sustainability")
def item_sustainability(item_id: str):
    """Sustainability from the item's latest saved evaluation (e.g. a closet item bought via "Should I buy?").
    The stats (new outfits, redundancy) are as of that evaluation. 404 if the item was never evaluated."""
    item = _item_or_404(item_id)
    ev = db.latest_evaluation_for_item(item_id)
    sus = None
    if ev:  # current attributes (e.g. a fabric fixed later) + the outfit/redundancy stats stored with the evaluation
        r = ev["results"]
        price = (r.get("value") or {}).get("price")
        sus = sustainability_for(item, r.get("total_new_outfits") or 0, (r.get("redundancy") or {}).get("top_similarity"),
                                 price if price is not None else (item.get("attributes") or {}).get("price"),
                                 r.get("settings") or db.get_settings())
    if not sus:
        raise HTTPException(404, f"no sustainability estimate for item {item_id} (never evaluated)")
    return {**sus, "evaluation_id": ev["id"], "evaluated_at": ev.get("created_at"),
            "n_new_outfits_at_evaluation": ev["results"].get("total_new_outfits")}


@app.post("/api/evaluations/{evaluation_id}/suggestions")
def evaluation_suggestions(evaluation_id: str, refresh: bool = False):
    """Live-shopping suggestions for an evaluation (SKIP -> better alternatives, BUY -> pairings). One grounded
    Gemini call, cached per evaluation (refresh=true recomputes)."""
    if db.get_evaluation(evaluation_id) is None:
        raise HTTPException(404, f"evaluation {evaluation_id} not found")
    try:
        return suggest.get_or_create(evaluation_id, refresh=refresh)
    except suggest.GeminiUnavailable as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        log.exception("suggestions failed")
        raise HTTPException(500, f"suggestions failed: {type(e).__name__}: {e}")


@app.get("/api/evaluations/{evaluation_id}/suggestions")
def get_evaluation_suggestions(evaluation_id: str):
    cached = db.get_suggestions(evaluation_id)
    if cached is None:
        raise HTTPException(404, f"no suggestions yet for evaluation {evaluation_id}")
    return {**suggest.add_sustainability(cached), "cached": True}


@app.get("/api/suggestions/wardrobe")
def wardrobe_suggestions(refresh: bool = False, wait: float = 0.0):
    """"Worth a look": 3-5 real products that fill gaps in the current closet (app/wardrobe_suggest.py).
    Never blocks by default: status 'ready' (cached per closet contents), 'pending' (live search running in the
    background; poll again) or 'error' (suggestions=[] + reason). wait=N blocks up to N seconds (max 150)."""
    try:
        return wardrobe_suggest.get(refresh=refresh, wait_s=max(0.0, min(150.0, wait)))
    except Exception as e:
        log.exception("wardrobe suggestions failed")
        return {"status": "error", "suggestions": [], "title": wardrobe_suggest.TITLE,
                "subtitle": wardrobe_suggest.SUBTITLE, "reason": "Couldn't look through stores right now.",
                "detail": f"{type(e).__name__}: {e}"}


@app.post("/api/candidate/{item_id}/add-to-closet")
def candidate_to_closet(item_id: str):
    _item_or_404(item_id)
    try:
        out = pipeline.item_to_api(pipeline.add_to_closet([item_id], purchased=True)[0])
        wardrobe_suggest.closet_changed()
        return out
    except pipeline.AccessoryNotAllowed as e:
        raise HTTPException(422, str(e))


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: SettingsBody):
    # exclude_unset: only fields the client sent (unknown / retired keys are ignored by the model)
    partial = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    out = db.update_settings(partial)
    wardrobe_suggest.closet_changed()
    return out
