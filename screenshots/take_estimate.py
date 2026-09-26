"""Screenshot of the result page with an ESTIMATED price (no price entered), then enter a price and re-check.
Same setup as take_verdict.py: live frontend (:3000), /api + /media forwarded to a TEST backend (:8001, throwaway copy
of the test data, Gemini off), /api/detect intercepted with a seeded candidate whose price was removed. NO Gemini calls.
Usage: python take_estimate.py [candidate_id] [frontend_url] [test_backend_url]
"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

IID = sys.argv[1] if len(sys.argv) > 1 else "seedcand_pv151970362"
FRONT = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:3000"
BACK = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8001"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"  # any image; detect is intercepted
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def forward(route):
    url = route.request.url
    r = route.fetch(url=f"{BACK}/{url.split('/', 3)[3]}")
    route.fulfill(response=r)


cand = json.load(urllib.request.urlopen(f"{BACK}/api/items/{IID}"))
assert cand["attributes"].get("price") in (None, ""), "candidate must have no price"
detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
          "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: m.type == "error" and "503" not in m.text and "404" not in m.text and errors.append(m.text[:200]))
    # the closet sheet asks /api/items/{id}/sustainability, which is a documented 404 for never-evaluated items
    page.on("response", lambda r: r.status == 404 and "/sustainability" not in r.url and errors.append(r.url))
    page.route("**/api/**", forward)
    page.route("**/media/**", forward)
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluations/*/suggestions", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                        body=json.dumps({"status": "idle", "products": []})))
    page.goto(f"{FRONT}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    body = page.locator("body").inner_text()
    check("optional" in body.lower(), "price input marked optional")
    check(page.locator("[data-testid=price-optional-hint]").count() == 1, "blank-price hint shown")
    btn = page.get_by_role("button", name="Evaluate")
    check(btn.is_enabled(), "Evaluate enabled with no price")
    btn.click()
    card = page.locator("[data-testid=verdict-card]")
    card.wait_for(timeout=90000)
    page.wait_for_timeout(1500)
    hero = card.inner_text()
    print("hero:", hero.replace("\n", " | ")[:300])
    check("est. ~$60" in hero, "hero shows est. ~$60")
    note = page.locator("[data-testid=price-estimate-note]")
    check(note.count() == 1 and "Estimated from brand and type" in note.inner_text(), "estimate note shown")
    stats = page.locator("body").inner_text()
    check("PRICE · EST." in stats.upper(), "price stat labelled est.")
    # frame: verdict hero + stats card with the estimate note
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{OUT}/verdict-estimated-price-phone.png")
    page.evaluate("document.querySelector('[data-testid=price-estimate-note]').scrollIntoView({block: 'center'})")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{OUT}/verdict-estimated-price-stats-phone.png")
    # enter the real price and re-check
    note.locator("input[type=number]").fill("45")
    note.get_by_role("button", name="Re-check").click()
    page.wait_for_function("() => !document.querySelector('[data-testid=price-estimate-note]')", timeout=60000)
    page.wait_for_timeout(800)
    hero2 = card.inner_text()
    print("hero after re-check:", hero2.replace("\n", " | ")[:300])
    check("$45" in hero2 and "est." not in hero2, "user price replaces the estimate")
    check(page.locator("[data-testid=change-price]").count() == 1, "change price link offered")
    # closet item sheet: estimated price with "est." label + editable price
    page.goto(f"{FRONT}/closet", wait_until="networkidle")
    page.locator("main button.group, button.group").first.click()
    page.wait_for_timeout(800)
    est = page.locator("[data-testid=closet-est-price]")
    check(est.count() == 1 and "est. ~$" in est.inner_text(), "closet sheet shows est. price")
    check(page.locator("input[type=number]").count() >= 1, "closet sheet price editable")
    page.screenshot(path=f"{OUT}/closet-estimated-price-phone.png")
    check(not errors, f"console errors {errors}")
    ctx.close()
    b.close()
print("FAILS:", fails or "none")
