#!/usr/bin/env python
"""Write a contact sheet of closet + candidate items to /tmp/sheet.jpg (debug helper)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw  # noqa: E402
from app import db  # noqa: E402
items = db.list_items(status="closet") + db.list_items(status="candidate")
W, th = 9, 130
sheet = Image.new("RGB", (W * th, ((len(items) + W - 1) // W) * (th + 26)), "white")
d = ImageDraw.Draw(sheet)
for i, it in enumerate(items):
    im = Image.open(it["white_path"]); im.thumbnail((th - 6, th - 6))
    x, y = (i % W) * th, (i // W) * (th + 26)
    sheet.paste(im, (x + 3, y + 3))
    a = it["attributes"]
    d.text((x + 2, y + th), f"{it['status'][:4]} {a.get('gender_presentation','')[:1]} {a['primary_color']}"[:24], fill="black")
    d.text((x + 2, y + th + 12), f"{a['subcategory']}"[:24], fill="black")
sheet.save(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sheet.jpg", quality=85)
