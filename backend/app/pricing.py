"""Price ESTIMATES for items without a user-entered price.

Where an estimate comes from (first that applies):
  1. Gemini, for NEW items, inside the existing garment-detection call (no extra request): the detection schema has
     `estimated_price_usd` (typical new US retail for brand + garment type + material; mid-market if the brand is
     unknown) and `price_confidence` (high = brand clearly identified, medium, low).  -> price_estimate_source "gemini"
  2. Gemini, for EXISTING items (saved before estimates existed): ONE batched text-only call built from the stored
     attributes (brand, type, material, colour, ...) for all unpriced items at once, cached on the items
     (`backfill()`, run in the background at startup and by scripts/backfill_price_estimates.py).
                                                                              -> price_estimate_source "gemini_text"
  3. A deterministic per-garment-type median price table (app/data/price_defaults.json, ROUGH US mid-market
     defaults) when Gemini is unavailable (fallback detector, quota errors, FITCHECK_GEMINI_OFF).
                                                                              -> price_estimate_source "table"

Price precedence everywhere (effective_price): user-entered price > price read off a tag > estimate.
Attribute keys: price (+ price_source "user" | "tag" | "seed"), estimated_price_usd, price_confidence,
price_estimate_source.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("fitcheck.pricing")

TABLE_PATH = Path(__file__).parent / "data" / "price_defaults.json"
CONFIDENCES = ("high", "medium", "low")
EST_MIN, EST_MAX = 3.0, 5000.0
ESTIMATE_KEYS = ("estimated_price_usd", "price_confidence", "price_estimate_source")
BATCH_MAX_ITEMS = 80   # one Gemini request covers up to this many items; the rest fall back to the table


# ------------------------------------------------------------------ table (fallback)
@lru_cache(maxsize=1)
def load_table() -> dict:
    raw = json.loads(TABLE_PATH.read_text())
    rules = [(re.compile(pat, re.I), float(p)) for pat, p in raw["rules"]]
    return {"rules": rules, "category_defaults": {k: float(v) for k, v in raw["category_defaults"].items()},
            "version": raw.get("version", 1)}


def table_price(attrs: dict | None, category: str | None = None) -> float:
    """Deterministic typical price for the item's garment type (subcategory first, then label/description)."""
    a = attrs or {}
    t = load_table()
    for text in (a.get("subcategory"), a.get("label"), a.get("description")):
        if not text:
            continue
        for rx, p in t["rules"]:
            if rx.search(str(text)):
                return p
    cat = (category or a.get("category") or "").lower()
    d = t["category_defaults"]
    return d.get(cat, d["_other"])


def table_estimate(attrs: dict | None, category: str | None = None) -> dict:
    return {"estimated_price_usd": table_price(attrs, category), "price_confidence": "low",
            "price_estimate_source": "table"}


# ------------------------------------------------------------------ helpers
def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f else None


def _clean_estimate(v) -> float | None:
    f = _num(v)
    if f is None:
        return None
    return float(round(min(max(f, EST_MIN), EST_MAX)))


def _clean_confidence(conf, brand) -> str:
    c = str(getattr(conf, "value", conf) or "").strip().lower()
    if c not in CONFIDENCES:
        c = "medium" if brand else "low"
    if c == "high" and not (brand and str(brand).strip()):
        c = "medium"   # "high" is reserved for a clearly identified brand
    return c


def normalize_estimate(attrs: dict, category: str | None = None, *, source: str = "gemini") -> dict:
    """Validate/fill the estimate keys on an attribute dict IN PLACE (and return it). A missing/invalid model
    estimate falls back to the table so every detected item has one. A price with no price_source came off a tag."""
    est = _clean_estimate(attrs.get("estimated_price_usd"))
    if est is None:
        attrs.update(table_estimate(attrs, category))
    else:
        attrs["estimated_price_usd"] = est
        attrs["price_confidence"] = _clean_confidence(attrs.get("price_confidence"), attrs.get("brand"))
        attrs["price_estimate_source"] = source
    if _num(attrs.get("price")) is not None and not attrs.get("price_source"):
        attrs["price_source"] = "tag"
    return attrs


def real_price(attrs: dict | None, purchase_price=None) -> tuple[float, str] | None:
    """(price, "user" | "tag") for a price the user entered (or seeded) or that was read off a tag; else None."""
    a = attrs or {}
    ps = a.get("price_source")
    p = _num(a.get("price"))
    if p is None and ps == "user" and a.get("price") not in (None, ""):
        try:
            p = 0.0 if float(a["price"]) == 0 else None   # a user-entered $0 (gift) is a real price
        except (TypeError, ValueError):
            p = None
    if p is not None:
        return p, ("user" if ps in ("user", "seed") else "tag")
    p = _num(purchase_price if purchase_price is not None else a.get("purchase_price"))
    if p is not None:
        return p, "user"
    return None


def estimate_for(attrs: dict | None, category: str | None = None) -> dict:
    """The stored estimate if valid, else a table estimate computed on the fly (never None)."""
    a = attrs or {}
    est = _clean_estimate(a.get("estimated_price_usd"))
    if est is not None:
        return {"estimated_price_usd": est, "price_confidence": _clean_confidence(a.get("price_confidence"),
                                                                                  a.get("brand")),
                "price_estimate_source": a.get("price_estimate_source") or "gemini"}
    return table_estimate(a, category)


def effective_price(attrs: dict | None, category: str | None = None, user_price=None,
                    purchase_price=None) -> dict:
    """The price to use for cost per wear. A user-entered price always wins, then a tag price, then the estimate.
    -> {price, price_source: user|tag|estimated, price_confidence (estimates only), estimated_price, estimate_source}"""
    a = attrs or {}
    est = estimate_for(a, category)
    out = {"estimated_price": est["estimated_price_usd"], "estimate_source": est["price_estimate_source"],
           "price_confidence": None}
    up = _num(user_price)
    if up is not None:
        return {**out, "price": up, "price_source": "user"}
    rp = real_price(a, purchase_price)
    if rp is not None:
        return {**out, "price": rp[0], "price_source": rp[1]}
    return {**out, "price": est["estimated_price_usd"], "price_source": "estimated",
            "price_confidence": est["price_confidence"]}


def needs_estimate(item: dict) -> bool:
    a = item.get("attributes") or {}
    return real_price(a, item.get("purchase_price")) is None and _clean_estimate(a.get("estimated_price_usd")) is None


# ------------------------------------------------------------------ batched Gemini text call (backfill)
class PriceConfidence(str, Enum):
    high = "high"; medium = "medium"; low = "low"


class PriceEstimate(BaseModel):
    id: str
    estimated_price_usd: float = Field(description="typical NEW full retail price in USD")
    price_confidence: PriceConfidence


class PriceEstimates(BaseModel):
    items: list[PriceEstimate]


BATCH_PROMPT = """You are a US fashion retail pricing assistant. For EACH clothing item below, estimate its typical
NEW full retail price in USD (not sale, not resale) for that brand + garment type + material.
If the brand is unknown ("-"), give a typical US mid-market price for that garment type and material
(think Gap / J.Crew / Levi's / Nike tier).
price_confidence: "high" only when the brand is known and its usual price for this item type is well established;
"medium" when the brand is known but prices vary, or the brand is unknown but type + material are specific;
"low" otherwise. Return exactly one entry per id, using the same ids.
Items (id | category | type | brand | material | color | pattern | gender | description):
"""


def _line(i: int, it: dict) -> str:
    a = it.get("attributes") or {}
    f = [str(i), it.get("category") or a.get("category") or "-", a.get("subcategory") or "-", a.get("brand") or "-",
         a.get("fabric_guess") or "-", a.get("primary_color") or "-", a.get("pattern") or "-",
         a.get("gender_presentation") or "-", (a.get("description") or "-")[:120]]
    return " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in f)


def gemini_batch_estimates(items: list[dict]) -> dict[str, dict]:
    """ONE text-only Gemini request for all `items` -> {item_id: {estimated_price_usd, price_confidence}}."""
    from . import gemini
    prompt = BATCH_PROMPT + "\n".join(_line(i, it) for i, it in enumerate(items, 1))
    res: PriceEstimates = gemini._generate([prompt], PriceEstimates)
    out: dict[str, dict] = {}
    for e in res.items:
        try:
            idx = int(str(e.id).strip()) - 1
        except ValueError:
            continue
        if 0 <= idx < len(items):
            out[items[idx]["id"]] = {"estimated_price_usd": e.estimated_price_usd,
                                     "price_confidence": e.price_confidence}
    return out


def backfill(statuses=("closet", "candidate", "detected"), use_gemini: bool = True, dry_run: bool = False) -> dict:
    """Give every item that has neither a price nor an estimate an estimate, WITHOUT re-running detection:
    one batched Gemini text call (<= BATCH_MAX_ITEMS items, closet items first), table fallback for the rest or on
    any error (quota). Only the estimate keys are written; each item is re-read right before its update."""
    from . import db, gemini
    todo = [it for s in statuses for it in db.list_items(status=s) if needs_estimate(it)]
    summary = {"items_needing_estimate": len(todo), "gemini_calls": 0, "gemini_estimates": 0,
               "table_estimates": 0, "error": None, "dry_run": dry_run, "estimates": []}
    if not todo:
        return summary
    est_map: dict[str, dict] = {}
    if use_gemini and gemini.is_configured():
        try:
            summary["gemini_calls"] = 1
            est_map = gemini_batch_estimates(todo[:BATCH_MAX_ITEMS])
        except Exception as e:  # quota / network / parse -> table
            summary["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("price backfill: Gemini batch failed (%s); using the table", summary["error"])
    for it in todo:
        fresh = db.get_item(it["id"])
        if not fresh or not needs_estimate(fresh):
            continue
        attrs = dict(fresh.get("attributes") or {})
        g = est_map.get(it["id"])
        if g and _clean_estimate(g.get("estimated_price_usd")) is not None:
            attrs.update(g)
            normalize_estimate(attrs, fresh.get("category"), source="gemini_text")
            summary["gemini_estimates"] += 1
        else:
            attrs.update(table_estimate(attrs, fresh.get("category")))
            summary["table_estimates"] += 1
        summary["estimates"].append({"id": it["id"], "status": fresh.get("status"),
                                     "brand": attrs.get("brand"), "type": attrs.get("subcategory"),
                                     "estimated_price_usd": attrs["estimated_price_usd"],
                                     "price_confidence": attrs["price_confidence"],
                                     "source": attrs["price_estimate_source"]})
        if not dry_run:
            db.update_item(it["id"], attributes=attrs, category=fresh.get("category"))
    return summary


_bg_started = False
_bg_lock = threading.Lock()


def start_background_backfill() -> bool:
    """Run backfill() once per process in a daemon thread (startup). Disable with FITCHECK_PRICE_BACKFILL=0."""
    global _bg_started
    if os.environ.get("FITCHECK_PRICE_BACKFILL", "1") == "0":
        return False
    with _bg_lock:
        if _bg_started:
            return False
        _bg_started = True

    def run():
        try:
            s = backfill()
            if s["items_needing_estimate"]:
                log.info("price backfill: %d items (%d gemini, %d table)%s", s["items_needing_estimate"],
                         s["gemini_estimates"], s["table_estimates"], f"; {s['error']}" if s["error"] else "")
        except Exception:
            log.exception("price backfill failed")

    threading.Thread(target=run, name="price-backfill", daemon=True).start()
    return True
