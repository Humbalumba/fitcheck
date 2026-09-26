#!/usr/bin/env python
"""Re-render the clean product image of every closet item.

  python scripts/render_closet.py                    # mode auto: Gemini redraw (if available + verified) > template > cleanup
  python scripts/render_closet.py --mode template    # canonical brand-style template (no image generation)
  python scripts/render_closet.py --mode gemini      # force a Gemini redraw attempt (falls back if it fails/unverified)
  python scripts/render_closet.py --mode cleanup     # deterministic photo cleanup only
  python scripts/render_closet.py --dry-run          # show what each item would get; changes nothing
  python scripts/render_closet.py --ids it_a,it_b    # only these items

By default it drives the RUNNING backend (POST /api/items/{id}/render, wait=true) so the server's render cache stays
in sync. --local renders in this process instead (restart the backend afterwards so it re-reads the records).
Gemini image calls use GEMINI_IMAGE_API_KEY from backend/.env if set (else the main key); a key change resets the
auto-disable automatically. Keys are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _api(base: str, path: str, body: dict | None = None, timeout: float = 600):
    req = urllib.request.Request(base.rstrip("/") + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _gemini_state_readonly() -> dict:
    """Would a Gemini redraw be attempted? (read-only: does not touch render_state.json)."""
    from app import render
    fp, src = render._image_key_info()
    try:
        st = json.loads(render._state_path().read_text())
    except Exception:
        st = {}
    until = float(st.get("disabled_until") or 0)
    key_changed = "key_fp" in st and st.get("key_fp") != fp
    disabled = render.RENDER_GEMINI == "off" or (render.RENDER_GEMINI == "auto" and time.time() < until
                                                 and not key_changed)
    return {"configured": fp is not None, "key_source": src, "key_fingerprint": fp, "disabled": disabled,
            "reason": None if key_changed else st.get("reason"), "key_changed": key_changed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="auto", choices=["auto", "gemini", "template", "cleanup"])
    ap.add_argument("--dry-run", action="store_true", help="list items + planned method; change nothing")
    ap.add_argument("--ids", default="", help="comma-separated item ids (default: every closet item)")
    ap.add_argument("--api", default=os.environ.get("FITCHECK_API", "http://localhost:8000"))
    ap.add_argument("--local", action="store_true", help="render in-process instead of via the running backend")
    args = ap.parse_args()

    from app import db, render, render_template as rt
    items = db.list_items(status="closet")
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [it for it in items if it["id"] in want]
    if not items:
        print("no closet items")
        return 0

    if args.dry_run:
        g = _gemini_state_readonly()
        print(f"Gemini image key: source={g['key_source']} fingerprint={g['key_fingerprint']} "
              f"disabled={g['disabled']}{' (' + g['reason'] + ')' if g['disabled'] and g['reason'] else ''}"
              f"{' [key changed: auto-disable will reset]' if g['key_changed'] else ''}")
        gem = args.mode in ("auto", "gemini") and g["configured"] and not g["disabled"]
        for it in items:
            rec = render.get_render(it["id"]) or {}
            details, frame = rt.get_details(it)
            blk = rt.template_blocker(it, details)
            sel = None if blk else rt.select_template(it, details)
            if args.mode == "cleanup":
                plan = "cleanup"
            else:
                tpl = f"template {sel[0]}/{sel[1]}" if sel else f"cleanup ({blk or 'no template'})"
                plan = (f"gemini redraw -> else {tpl}" if gem else tpl) if args.mode != "template" else tpl
            print(f"{it['id']:<18} {str(it.get('label'))[:34]:<34} now={rec.get('method') or '-':<9} "
                  f"details={'yes/' + frame if details else 'no':<10} plan: {plan}")
        return 0

    use_api = not args.local
    if use_api:
        try:
            _api(args.api, "/api/health", timeout=10)
        except (urllib.error.URLError, OSError) as e:
            print(f"backend not reachable at {args.api} ({e}); use --local to render in-process")
            return 2
    ok = 0
    for it in items:
        t0 = time.time()
        try:
            if use_api:
                j = _api(args.api, f"/api/items/{it['id']}/render", {"mode": args.mode, "wait": True})
                method, status = j.get("clean_method"), j.get("render_status")
                checks = j.get("render_checks") or {}
            else:
                rec = render.render_item(it["id"], args.mode) or {}
                method, status, checks = rec.get("method"), rec.get("status"), rec.get("checks") or {}
            why = ""
            if method != "gemini" and (checks.get("gemini_render") or {}).get("reason"):
                why = f" (gemini: {checks['gemini_render']['reason']})"
            if method == "cleanup" and (checks.get("template") or {}).get("skipped"):
                why += f" (template: {checks['template']['skipped']})"
            tpl = (checks.get("template") or {}).get("template")
            print(f"{it['id']:<18} {str(it.get('label'))[:34]:<34} {status}/{method}"
                  f"{' [' + tpl + ']' if method == 'template' and tpl else ''}{why}  {time.time() - t0:.1f}s")
            ok += status == "done"
        except Exception as e:
            print(f"{it['id']:<18} FAILED: {type(e).__name__}: {str(e)[:200]}")
    if args.local:
        print("rendered in-process: restart the backend (./stop.sh && ./run.sh --bg) so it re-reads the records")
    print(f"{ok}/{len(items)} rendered")
    return 0 if ok == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
