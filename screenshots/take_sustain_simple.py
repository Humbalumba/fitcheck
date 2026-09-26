"""Screenshots of the simplified sustainability card on the 'Should I buy?' result (no Gemini calls: detect/evaluate
are answered from a saved evaluation, suggestions stubbed). Usage: python take_sustain_simple.py [eval_id] [suffix]"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
OUT = "/workspace/fitcheck/screenshots"
EV = sys.argv[1] if len(sys.argv) > 1 else "ev_37e01e6a83ed"
SUFFIX = sys.argv[2] if len(sys.argv) > 2 else ""
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"
ev = json.load(urllib.request.urlopen(f"http://localhost:8000/api/evaluations/{EV}"))
cand = ev["item"]
detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
          "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}


def run(p, name, vp, mobile):
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport=vp, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and print("  console error:", m.text[:200]))
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluate", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(ev)))
    page.route("**/api/evaluations/*/suggestions*", lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":"screenshot stub"}'))
    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    if not page.input_value("input[type=number]"):
        page.fill("input[type=number]", str(ev.get("value", {}).get("price") or 45))
    page.get_by_role("button", name="Evaluate").click()
    card = page.locator("[data-testid=sustainability-card]")
    card.wait_for(timeout=60000)
    page.wait_for_timeout(1500)
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    print(" ", name, "collapsed text:", card.inner_text().replace("\n", " | "))
    assert page.locator("[data-testid=sustainability-details]").count() == 0, "details should start collapsed"
    card.screenshot(path=f"{OUT}/sustain_simple_{name}{SUFFIX}.png")
    card.get_by_role("button", name="See the details").click()
    page.wait_for_timeout(400)
    card.screenshot(path=f"{OUT}/sustain_simple_{name}{SUFFIX}_details.png")
    print(" ", name, "card width:", card.bounding_box()["width"], "doc scrollWidth:", page.evaluate("document.documentElement.scrollWidth"))
    b.close()


with sync_playwright() as p:
    run(p, "desktop", {"width": 1280, "height": 800}, False)
    run(p, "mobile", {"width": 390, "height": 844}, True)
