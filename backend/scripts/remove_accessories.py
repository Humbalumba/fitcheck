#!/usr/bin/env python
"""One-off cleanup: remove every accessory item (any status) + dependent rows, then rebuild the FAISS index.

FitCheck handles clothes and shoes only (app/accessories.py). Deleting an item cascades to item_embeddings,
evaluations (-> outfits, outfit_items, suggestions), outfit_items, item_renders and item_render_details;
compat_edges rows mentioning the item are deleted explicitly (db.delete_item). The item's media files are MOVED
into --media-backup (not deleted). Also drops the `bag_womens` seed candidate from seed_manifest.json and moves
test_images/product_bag_womens.jpg into the backup. Stop the API server first (it keeps the FAISS index in memory).

  FITCHECK_DATA_DIR=/path/to/data python scripts/remove_accessories.py --media-backup DIR [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.accessories import is_accessory_item  # noqa: E402


def _count(c, sql, args) -> int:
    try:
        return c.execute(sql, args).fetchone()[0]
    except Exception:  # table may not exist in older DBs
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media-backup", required=True, help="directory the removed items' media files are moved to")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    bk = Path(args.media_backup)
    print(f"DB: {config.DB_PATH}\nFAISS: {config.FAISS_PATH}")
    acc = [it for it in db.list_items() if is_accessory_item(it)]
    report = []
    with db.get_conn() as c:
        for it in acc:
            iid = it["id"]
            evs = [r[0] for r in c.execute("SELECT id FROM evaluations WHERE candidate_item_id=?", (iid,))]
            q = ",".join("?" * len(evs))
            report.append({
                "id": iid, "status": it["status"], "category": it["category"],
                "subcategory": (it.get("attributes") or {}).get("subcategory"), "label": it.get("label"),
                "embeddings": _count(c, "SELECT COUNT(*) FROM item_embeddings WHERE item_id=?", (iid,)),
                "evaluations": len(evs),
                "outfits": _count(c, f"SELECT COUNT(*) FROM outfits WHERE evaluation_id IN ({q})", evs) if evs else 0,
                "suggestions": _count(c, f"SELECT COUNT(*) FROM suggestions WHERE evaluation_id IN ({q})", evs) if evs else 0,
                "outfit_items": _count(c, "SELECT COUNT(*) FROM outfit_items WHERE item_id=?", (iid,)),
                "compat_edges": _count(c, "SELECT COUNT(*) FROM compat_edges WHERE item_a=? OR item_b=? OR item_c=? "
                                          "OR outfit_key LIKE ?", (iid, iid, iid, f"%{iid}%")),
                "renders": _count(c, "SELECT COUNT(*) FROM item_renders WHERE item_id=?", (iid,))
                + _count(c, "SELECT COUNT(*) FROM item_render_details WHERE item_id=?", (iid,)),
            })
    for r in report:
        print(json.dumps(r))
    if args.dry_run:
        print(f"dry run: {len(report)} accessory items would be removed")
        return
    bk.mkdir(parents=True, exist_ok=True)
    moved = 0
    for it in acc:
        for k in ("crop_path", "cutout_path", "white_path", "context_path"):
            p = Path(it[k]) if it.get(k) else None
            if p and p.exists():
                shutil.move(str(p), str(bk / f"{it['id']}__{k}{p.suffix}"))
                moved += 1
        try:
            from app import render
            render.forget(it["id"])  # clean-image files + render rows (no-op if none)
        except Exception as e:
            print("render cleanup skipped:", e)
        db.delete_item(it["id"])
    # the accessory seed candidate: manifest + test image
    mp = config.DATA_DIR / "seed_manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text())
        if m.get("candidates", {}).pop("bag_womens", None) is not None:
            mp.write_text(json.dumps(m, indent=1))
            print("removed bag_womens from", mp)
    ti = config.TEST_IMAGES_DIR / "product_bag_womens.jpg"
    if ti.exists():
        shutil.move(str(ti), str(bk / ti.name))
        print("moved", ti)
    from app.vectors import closet_index
    closet_index.rebuild()
    print(f"removed {len(acc)} accessory items, moved {moved} media files to {bk}; "
          f"FAISS rebuilt with {len(closet_index.id_map)} closet items")
    left = [it["id"] for it in db.list_items() if is_accessory_item(it)]
    assert not left, left


if __name__ == "__main__":
    main()
