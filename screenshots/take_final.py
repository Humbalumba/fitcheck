"""Mobile (390x844) screenshots of the real app (Gemini on). Usage: python take_final.py [base_url]"""
import sys, time
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images"
only = set(sys.argv[2:])

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and print("console error:", m.text[:200]))

    if not only or "closet" in only:
        page.goto(f"{BASE}/closet", wait_until="networkidle")
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/final-closet.png")
        print("closet ok")

    for name, photo in (("add-flatlay", "flatlay_athleisure.jpg"), ("add-messy", "messy_bed_flatlay.jpg")):
        if only and name not in only:
            continue
        page.goto(f"{BASE}/add", wait_until="networkidle")
        page.locator("input[type=file]").nth(1).set_input_files(f"{IMG}/{photo}")
        t = time.time()
        page.wait_for_selector("text=/item(s)? found/", timeout=180000)
        print(name, "detected in %.1fs" % (time.time() - t))
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/final-{name}.png")
        page.evaluate("window.scrollTo(0, 430)")
        page.wait_for_timeout(800)
        page.screenshot(path=f"{OUT}/final-{name}-cards.png")

    if not only or "buy" in only:
        page.goto(f"{BASE}/buy", wait_until="networkidle")
        page.locator("input[type=file]").nth(1).set_input_files(f"{IMG}/product_bottom_womens.jpg")
        page.wait_for_selector("input[type=number]", timeout=180000)
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{OUT}/final-buy-select.png")
        page.fill("input[type=number]", "45")
        page.get_by_role("button", name="Evaluate").click()
        page.wait_for_selector("text=/BUY|SKIP|MAYBE|Buy it|Skip/i", timeout=180000)
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/final-buy-result.png")
        page.get_by_text("Outfits it unlocks").scroll_into_view_if_needed()
        page.evaluate("window.scrollBy(0, -80)")
        page.wait_for_timeout(1000)
        page.screenshot(path=f"{OUT}/final-buy-result-outfits.png")
        print("buy ok")
    b.close()
