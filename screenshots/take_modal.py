"""Screenshots of the outfit zoom modal on the "Should I buy?" results (desktop 1280x800 + phone 390x844).

Uses NO Gemini calls: /api/detect is intercepted and answered with the seeded white-trousers candidate
(seedcand_pv151970362), then the real /api/evaluate runs once (its response is reused for the 2nd viewport).
Usage: python take_modal.py [base_url]
"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"
CAND = "seedcand_pv151970362"

cand = json.load(urllib.request.urlopen(f"{BASE}/api/items/{CAND}"))
detect_payload = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
                  "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
cached_eval = {}


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)


def run(p, name, viewport, mobile):
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport=viewport, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and print("  console error:", m.text[:200]))
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect_payload)))

    def on_eval(route):
        if cached_eval:
            route.fulfill(status=200, content_type="application/json", body=cached_eval["body"])
        else:
            resp = route.fetch(timeout=180000)
            cached_eval["body"] = resp.text()
            route.fulfill(response=resp)
    page.route("**/api/evaluate", on_eval)

    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    if not page.input_value("input[type=number]"):
        page.fill("input[type=number]", "45")
    page.get_by_role("button", name="Evaluate").click()
    page.wait_for_selector("text=Outfits it unlocks", timeout=180000)
    page.wait_for_timeout(1500)
    page.evaluate("""() => { const el = [...document.querySelectorAll('h2')].find(h => h.textContent.includes('Outfits it unlocks'));
        window.scrollTo({top: el.getBoundingClientRect().top + window.scrollY - 68, behavior: 'instant'}); }""")
    page.wait_for_timeout(400)
    cards = page.locator("[role=button][aria-label^='View outfit larger']")
    check(cards.count() > 0, f"{cards.count()} clickable outfit cards")
    check(cards.first.evaluate("e => getComputedStyle(e).cursor") == "pointer", "cards have pointer cursor")
    if not mobile:
        cards.nth(1).hover()
    page.wait_for_timeout(1200)
    page.screenshot(path=f"{OUT}/modal-{name}-grid.png")

    modal = page.locator("[data-testid=outfit-modal]")
    cards.first.click()
    modal.wait_for(state="visible")
    page.wait_for_timeout(1200)  # animations + images
    check(page.evaluate("document.body.style.overflow") == "hidden", "body scroll locked")
    label = modal.get_attribute("aria-label")
    check(label.startswith("Outfit 1 of"), f"indicator: {label}")
    page.screenshot(path=f"{OUT}/modal-{name}-open.png")

    # arrow keys: find a 4-piece outfit (top+bottom+outerwear+shoes) and screenshot it
    total = int(label.split(" of ")[1])
    page.keyboard.press("ArrowRight")
    check(modal.get_attribute("aria-label") == f"Outfit 2 of {total}", "ArrowRight -> Outfit 2")
    page.keyboard.press("ArrowLeft")
    check(modal.get_attribute("aria-label") == f"Outfit 1 of {total}", "ArrowLeft -> Outfit 1")
    page.keyboard.press("ArrowLeft")
    check(modal.get_attribute("aria-label") == f"Outfit {total} of {total}", "ArrowLeft wraps to last")
    if modal.locator("figure").count() >= 4:
        page.wait_for_timeout(900)
        page.screenshot(path=f"{OUT}/modal-{name}-open-4piece.png")
    page.get_by_role("button", name="Next outfit").last.click()
    check(modal.get_attribute("aria-label") == f"Outfit 1 of {total}", "Next button wraps to first")

    page.keyboard.press("Escape")
    check(modal.count() == 0, "Esc closes")
    check(page.evaluate("document.body.style.overflow") != "hidden", "scroll unlocked after close")
    cards.first.click()
    modal.wait_for(state="visible")
    page.mouse.click(4, 4)
    page.wait_for_timeout(200)
    check(modal.count() == 0, "backdrop click closes")
    cards.nth(1).click()
    modal.wait_for(state="visible")
    modal.get_by_role("button", name="Close").click()
    check(modal.count() == 0, "X button closes")
    cards.first.focus()
    page.keyboard.press("Enter")
    check(modal.count() == 1, "Enter on focused card opens")
    page.keyboard.press("Escape")
    b.close()


with sync_playwright() as p:
    print("desktop 1280x800")
    run(p, "desktop", {"width": 1280, "height": 800}, False)
    print("phone 390x844")
    run(p, "phone", {"width": 390, "height": 844}, True)
