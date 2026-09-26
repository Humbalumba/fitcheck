"""Screenshots of the BUY / CONSIDER / SKIP verdict card on the "Should I buy?" result page. NO Gemini calls.
The live frontend (:3000) is used, but every /api and /media request from the page is forwarded to a TEST backend
(default :8001, started on a throwaway copy of /workspace/fitcheck-testdata with FITCHECK_GEMINI_OFF=1). /api/detect is
intercepted with the seeded candidate, /api/evaluate runs for real on the test backend.
Usage: python take_verdict.py [frontend_url] [test_backend_url]
"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

FRONT = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
BACK = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8001"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"  # any image; detect is intercepted
CASES = [("buy", "seedcand_pv151970362", 45), ("consider", "seedcand_pv211404710", 150), ("skip", "seedcand_pv165232718", 28)]
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def get(path):
    return json.load(urllib.request.urlopen(f"{BACK}{path}"))


def forward(route):
    url = route.request.url
    path = url.split("/", 3)[3]
    r = route.fetch(url=f"{BACK}/{path}")
    route.fulfill(response=r)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for case, iid, price in CASES:
        cand = get(f"/api/items/{iid}")
        detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
                  "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
        ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: m.type == "error" and "503" not in m.text and errors.append(m.text[:200]))
        page.route("**/api/**", forward)
        page.route("**/media/**", forward)
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
        page.goto(f"{FRONT}/buy", wait_until="networkidle")
        page.locator("input[type=file]").nth(1).set_input_files(IMG)
        page.wait_for_selector("input[type=number]", timeout=60000)
        page.fill("input[type=number]", str(price))
        page.get_by_role("button", name="Evaluate").click()
        card = page.locator("[data-testid=verdict-card]")
        card.wait_for(timeout=90000)
        page.wait_for_timeout(1500)
        text = card.inner_text()
        dec = card.get_attribute("data-decision")
        print(case, dec, "|", text.replace("\n", " | ")[:300])
        check(dec == case.upper(), f"{case}: decision {dec}")
        check("/100" in text, f"{case}: score shown")
        body = page.locator("body").inner_text()
        check("budget" not in body.lower(), f"{case}: no budget text on the page")
        check("Cost / wear" in body or "COST / WEAR" in body, f"{case}: cost per wear stat")
        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{OUT}/verdict-{case}-phone.png")
        check(not errors, f"{case}: console errors {errors}")
        ctx.close()
    b.close()
print("FAILS:", fails or "none")
