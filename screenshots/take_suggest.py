"""Screenshots of the live-shopping suggestions on the "Should I buy?" results (desktop 1280x800 + phone 390x844).

Uses NO Gemini calls. Point it at a frontend whose /api proxy targets a TEST backend that already has cached
suggestions for the two evaluations below (e.g. `BACKEND_URL=http://localhost:8001 next dev -p 3001`):
  - /api/detect is intercepted and answered with the seeded candidate item (as in take_modal.py)
  - /api/evaluate is answered with the stored response of the real evaluation (from the test DB) so the page
    requests suggestions for that evaluation id -> served from the suggestions cache (real live products).
Usage: python take_suggest.py [base_url] [test_db]
"""
import json, sqlite3, sys, urllib.request
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3001"
DB = sys.argv[2] if len(sys.argv) > 2 else "/workspace/fitcheck-testdata/fitcheck.db"
OUT = "/workspace/fitcheck/screenshots"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"  # any image; detect is intercepted
CASES = {"skip": "ev_63d079818d12", "buy": "ev_1cddffd5bd44"}
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def stored_eval(eid):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cand, res = c.execute("SELECT candidate_item_id, results FROM evaluations WHERE id=?", (eid,)).fetchone()
    c.close()
    return cand, {**json.loads(res), "evaluation_id": eid}


def run(p, case, eid, name, viewport, mobile, extras=False):
    cand_id, ev = stored_eval(eid)
    cand = json.load(urllib.request.urlopen(f"{BASE}/api/items/{cand_id}"))
    detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
              "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport=viewport, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and print("  console error:", m.text[:200]))
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluate", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(ev)))
    hold = {"on": extras}

    def on_sugg(route):  # optionally hold the (cached) response so the loading skeleton can be captured
        if hold["on"]:
            page.wait_for_timeout(2500)
        route.continue_()
    page.route("**/api/evaluations/*/suggestions*", on_sugg)

    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    page.fill("input[type=number]", str(ev["value"]["price"]))
    page.get_by_role("button", name="Evaluate").click()
    title = "Better picks instead" if case == "skip" else "Pairs well with this"
    page.wait_for_selector(f"h2:has-text('{title}')", timeout=30000)
    if not extras:  # verdict + the section right under it, once the cards are in
        page.locator("[role=button][aria-label^='See outfits with']").first.wait_for(timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{OUT}/suggest-{case}-{name}-top.png")
    scroll = f"""() => {{ const el = [...document.querySelectorAll('h2')].find(h => h.textContent.includes('{title}'));
        window.scrollTo({{top: el.getBoundingClientRect().top + window.scrollY - 68, behavior: 'instant'}}); }}"""
    if extras:
        page.evaluate(scroll)
        page.wait_for_timeout(600)
        page.screenshot(path=f"{OUT}/suggest-{case}-{name}-loading.png")
    cards = page.locator("[role=button][aria-label^='See outfits with']")
    cards.first.wait_for(timeout=60000)
    page.wait_for_timeout(1500)
    imgs = page.locator("section img")
    page.evaluate(scroll)
    page.wait_for_function("() => [...document.querySelectorAll('section img')].every(i => i.complete)", timeout=15000)
    page.wait_for_timeout(500)
    loaded = page.evaluate("() => [...document.querySelectorAll('section img')].map(i => i.naturalWidth > 0)")
    n = cards.count()
    check(n == 3, f"{n} suggestion cards")
    check(all(loaded) and len(loaded) == n, f"product photos loaded: {loaded}")
    links = page.locator("section a[target=_blank]")
    check(links.count() == n, f"{links.count()} 'View at' links")
    rels = [links.nth(i).get_attribute("rel") or "" for i in range(links.count())]
    check(all("noopener" in r for r in rels), "links rel=noopener")
    print("  links:", [links.nth(i).inner_text() for i in range(links.count())])
    tops = [round(cards.nth(i).bounding_box()["y"]) for i in range(n)]
    check((len(set(tops)) == 1) if not mobile else (len(set(tops)) == n), f"layout {'3 across' if not mobile else '1 column'}: y={tops}")
    page.screenshot(path=f"{OUT}/suggest-{case}-{name}.png")

    if extras:  # full section, link opens a real product page in a new tab, card opens the outfit modal
        page.locator("section").first.screenshot(path=f"{OUT}/suggest-{case}-{name}-section.png")
        with page.expect_popup() as pop:
            links.first.click()
        popup = pop.value
        popup.wait_for_load_state("domcontentloaded", timeout=30000)
        check(popup.url.startswith("https://"), f"link opened new tab: {popup.url[:90]}")
        check(page.locator("[data-testid=outfit-modal]").count() == 0, "link click does not open the modal")
        popup.close()
        cards.first.click()
        modal = page.locator("[data-testid=outfit-modal]")
        modal.wait_for(state="visible")
        page.wait_for_timeout(1200)
        check((modal.get_attribute("aria-label") or "").startswith("Outfit 1 of"), f"modal: {modal.get_attribute('aria-label')}")
        page.screenshot(path=f"{OUT}/suggest-{case}-{name}-modal.png")
        page.keyboard.press("Escape")
    b.close()


def run_error(p, name, viewport, mobile):
    """Quota-exhausted state (503 from the endpoint) -- friendly message, page intact."""
    cand_id, ev = stored_eval(CASES["buy"])
    cand = json.load(urllib.request.urlopen(f"{BASE}/api/items/{cand_id}"))
    detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed", "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport=viewport, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluate", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({**ev, "evaluation_id": "ev_quota_demo"})))
    page.route("**/api/evaluations/*/suggestions*", lambda r: r.fulfill(status=503, content_type="application/json",
               body=json.dumps({"detail": "Gemini is out of free quota right now (quota resets 3 AM ET)"})))
    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    page.fill("input[type=number]", "45")
    page.get_by_role("button", name="Evaluate").click()
    page.wait_for_selector("text=taking a break", timeout=30000)
    check(page.locator("text=Outfits it unlocks").count() == 1, "quota error: rest of the page intact")
    page.evaluate("""() => { const el = [...document.querySelectorAll('h2')].find(h => h.textContent.includes('Pairs well'));
        window.scrollTo({top: el.getBoundingClientRect().top + window.scrollY - 68, behavior: 'instant'}); }""")
    page.wait_for_timeout(400)
    page.screenshot(path=f"{OUT}/suggest-quota-{name}.png")
    b.close()


with sync_playwright() as p:
    for case, eid in CASES.items():
        print(f"{case} desktop 1280x800")
        run(p, case, eid, "desktop", {"width": 1280, "height": 800}, False, extras=True)
        run(p, case, eid, "desktop", {"width": 1280, "height": 800}, False, extras=False)
        print(f"{case} phone 390x844")
        run(p, case, eid, "phone", {"width": 390, "height": 844}, True, extras=True)
        run(p, case, eid, "phone", {"width": 390, "height": 844}, True, extras=False)
    print("quota error phone")
    run_error(p, "phone", {"width": 390, "height": 844}, True)
print("FAILS:", fails or "none")
