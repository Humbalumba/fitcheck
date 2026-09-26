"""Screenshots of the sustainability card (Should I buy? result), the leaf chips on suggestion cards and the closet
item sheet. desktop 1280x800 + phone 390x844. NO Gemini calls: /api/detect is intercepted with the seeded candidate,
/api/evaluate is answered with the backend's saved evaluation (GET /api/evaluations/{id}, sustainability computed on
read) and suggestions come from the backend's cache.
Run against a frontend whose /api proxy targets a TEST backend on a throwaway DB copy -- the closet step marks the
trousers as bought ("I bought it") in that DB.
Usage: python take_sustain.py [base_url]
"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3001"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"  # any image; detect is intercepted
CASES = {"buy": "ev_1cddffd5bd44", "skip": "ev_63d079818d12"}
VIEWPORTS = {"desktop": ({"width": 1280, "height": 800}, False), "phone": ({"width": 390, "height": 844}, True)}
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def get(path, method="GET"):
    return json.load(urllib.request.urlopen(urllib.request.Request(f"{BASE}{path}", method=method)))


def browser(p, vp, mobile):
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport=vp, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and "404" not in m.text and print("  console error:", m.text[:200]))
    return b, page


def scroll_to(page, selector_js):
    page.evaluate(f"""() => {{ const el = {selector_js};
        window.scrollTo({{top: el.getBoundingClientRect().top + window.scrollY - 68, behavior: 'instant'}}); }}""")
    page.wait_for_timeout(400)


def run_case(p, case, eid, name):
    vp, mobile = VIEWPORTS[name]
    ev = get(f"/api/evaluations/{eid}")
    sus = ev["sustainability"]
    sugg = get(f"/api/evaluations/{eid}/suggestions")["suggestions"]
    cand = get(f"/api/items/{ev['item']['id']}")
    detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
              "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
    b, page = browser(p, vp, mobile)
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluate", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(ev)))
    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    page.fill("input[type=number]", str(ev["value"]["price"]))
    page.get_by_role("button", name="Evaluate").click()
    card = page.locator("[data-testid=sustainability-card]")
    card.wait_for(timeout=30000)
    page.locator("[role=button][aria-label^='See outfits with']").first.wait_for(timeout=60000)
    page.wait_for_timeout(1500)
    text = card.inner_text()
    check(str(sus["score"]) in text and f"{sus['grade']} · {sus['label']}" in text,
          f"card shows {sus['score']} {sus['grade']} {sus['label']}")
    check("kg CO₂e over" in text and "expected wears" in text,
          "footprint line: " + next((ln for ln in text.split("\n") if "kg CO₂e over" in ln), "?"))
    check(all(r.replace("CO2e", "CO₂e")[:40] in text for r in sus["reasons"]), "both reasons shown")
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{OUT}/sustain-{case}-{name}.png")

    btn = card.get_by_role("button", name="How this estimate works")
    btn.click()
    check(btn.get_attribute("aria-expanded") == "true" and "WRAP" in card.inner_text(), "info toggle explains the method")
    scroll_to(page, "document.querySelector('[data-testid=sustainability-card]')")
    page.screenshot(path=f"{OUT}/sustain-{case}-{name}-info.png")

    chips = page.locator("section span[aria-label^='Sustainability estimate']")
    got = [chips.nth(i).inner_text().strip() for i in range(chips.count())]
    want = [f"{s['sustainability']['score']} · {s['sustainability']['grade']}" for s in sugg]
    check(got == want, f"suggestion leaf chips {got}")
    title = "Better picks instead" if case == "skip" else "Pairs well with this"
    scroll_to(page, f"[...document.querySelectorAll('h2')].find(h => h.textContent.includes('{title}'))")
    page.screenshot(path=f"{OUT}/sustain-{case}-{name}-picks.png")
    b.close()
    return ev


def run_closet(p, item_id, name):
    vp, mobile = VIEWPORTS[name]
    b, page = browser(p, vp, mobile)
    page.goto(f"{BASE}/closet", wait_until="networkidle")
    item = get(f"/api/items/{item_id}")
    img = item["image_url"].rsplit("/", 1)[-1]
    tile = page.locator(f"button:has(img[src*='{img}'])").first
    tile.scroll_into_view_if_needed()
    tile.click()
    box = page.locator("[data-testid=item-sustainability]")
    box.wait_for(timeout=15000)
    page.wait_for_timeout(800)
    s = get(f"/api/items/{item_id}/sustainability")
    check(f"{s['score']} · {s['grade']}" in box.inner_text(), f"closet sheet: {box.inner_text()!r}")
    page.screenshot(path=f"{OUT}/sustain-closet-{name}.png")
    # a never-evaluated closet item shows nothing (404 -> hidden)
    page.keyboard.press("Escape")
    b.close()


with sync_playwright() as p:
    for case, eid in CASES.items():
        for name in VIEWPORTS:
            print(f"{case} {name}")
            ev = run_case(p, case, eid, name)
    trousers = get(f"/api/evaluations/{CASES['buy']}")["item"]["id"]
    if get(f"/api/items/{trousers}")["status"] != "closet":
        get(f"/api/candidate/{trousers}/add-to-closet", method="POST")  # "I bought it" (throwaway test DB)
    for name in VIEWPORTS:
        print(f"closet sheet {name}")
        run_closet(p, trousers, name)
print("FAILS:", fails or "none")
