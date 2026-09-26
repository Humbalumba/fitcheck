#!/usr/bin/env python
"""Give existing items that have neither a price nor a price estimate an estimate, WITHOUT re-running detection.

One batched text-only Gemini call (from stored brand / type / material / colour / ...) for all such items at once;
per-garment-type table (app/data/price_defaults.json) for the rest or if Gemini is off / out of quota.
Idempotent: items that already have a price or an estimate are skipped (so a 2nd run makes no Gemini call).
The backend also runs this once in the background at startup (disable with FITCHECK_PRICE_BACKFILL=0).

    .venv/bin/python scripts/backfill_price_estimates.py [--no-gemini] [--dry-run] [--statuses closet,candidate,detected]
Uses the same data dir / DB as the backend (FITCHECK_DATA_DIR / FITCHECK_DB).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, pricing  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--no-gemini", action="store_true", help="table estimates only (0 Gemini calls)")
ap.add_argument("--dry-run", action="store_true", help="print the estimates without saving them")
ap.add_argument("--statuses", default="closet,candidate,detected")
ap.add_argument("--json", action="store_true", help="print the full summary as JSON")
args = ap.parse_args()

print(f"DB: {getattr(config, 'DB_PATH', '?')}")
s = pricing.backfill(statuses=tuple(x.strip() for x in args.statuses.split(",") if x.strip()),
                     use_gemini=not args.no_gemini, dry_run=args.dry_run)
if args.json:
    print(json.dumps(s, indent=2))
else:
    print(f"{s['items_needing_estimate']} items needed an estimate; gemini calls: {s['gemini_calls']}, "
          f"gemini estimates: {s['gemini_estimates']}, table estimates: {s['table_estimates']}"
          + (f", error: {s['error']}" if s["error"] else "") + (" (dry run, nothing saved)" if args.dry_run else ""))
    for e in s["estimates"]:
        print(f"  {e['status']:9s} {str(e['brand'] or '-')[:22]:22s} {str(e['type'] or '-')[:22]:22s} "
              f"~${e['estimated_price_usd']:.0f}  {e['price_confidence']:6s} ({e['source']})")
