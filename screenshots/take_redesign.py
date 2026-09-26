"""Redesign screenshots + console/hydration check. No Gemini calls: detect/evaluate are replayed from a saved
evaluation, suggestions are stubbed. Usage: python take_redesign.py [only=closet,buy,prefs,loading,doors,console]"""
import json, sys, urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
OUT = "/workspace/fitcheck/screenshots"
EV = "ev_37e01e6a83ed"
IMG = "/workspace/fitcheck/backend/data/test_images/product_bottom_womens.jpg"
ONLY = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else None
MOBILE = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
SEEN = "sessionStorage.setItem('fitcheck:doors-seen','1')"
problems = []


def want(k):
    return ONLY is None or k in ONLY


def new_page(b, opts, skip_doors=True, tag=""):
    ctx = b.new_context(**opts)
    if skip_doors:
        ctx.add_init_script(SEEN)
    page = ctx.new_page()
    def on_console(m):
        t = m.text
        if m.type in ("error", "warning") or "hydrat" in t.lower():
            if "screenshot stub" in t or "Download the React DevTools" in t:
                return
            problems.append(f"[{tag}] {m.type}: {t[:300]}")
    page.on("console", on_console)
    page.on("response", lambda r: r.status >= 400 and problems.append(f"[{tag}] HTTP {r.status} {r.url}"))
    page.on("pageerror", lambda e: problems.append(f"[{tag}] pageerror: {e}"))
    return ctx, page


def buy_result(page):
    ev = json.load(urllib.request.urlopen(f"http://localhost:8000/api/evaluations/{EV}"))
    cand = ev["item"]
    detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
              "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
    page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
    page.route("**/api/evaluate", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(ev)))
    page.route("**/api/evaluations/*/suggestions*", lambda r: r.fulfill(status=200, content_type="application/json",
               body=json.dumps({"suggestions": [], "message": "screenshot stub: no live picks"})))
    page.goto(f"{BASE}/buy", wait_until="networkidle")
    page.locator("input[type=file]").nth(1).set_input_files(IMG)
    page.wait_for_selector("input[type=number]", timeout=60000)
    if not page.input_value("input[type=number]"):
        page.fill("input[type=number]", str(ev.get("value", {}).get("price") or 45))
    page.get_by_role("button", name="Evaluate").click()
    page.wait_for_selector("[data-testid=verdict-card]", timeout=60000)
    page.wait_for_timeout(1500)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])

    if want("closet"):
        for name, opts in (("mobile", MOBILE), ("desktop", DESKTOP)):
            ctx, page = new_page(b, opts, tag=f"closet-{name}")
            page.goto(f"{BASE}/", wait_until="networkidle")
            assert page.url.endswith("/closet"), page.url
            page.wait_for_selector("[data-testid=rack-row]")
            page.wait_for_timeout(1500)
            page.screenshot(path=f"{OUT}/redesign_closet_{name}.png")
            print(name, "rows:", page.locator("[data-testid=rack-row]").count(),
                  "refresh btn:", page.locator("[aria-label=Refresh]").count(),
                  "live:", page.get_by_text("Live", exact=True).count(),
                  "scrollW:", page.evaluate("document.documentElement.scrollWidth"))
            if name == "mobile":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(600)
                page.screenshot(path=f"{OUT}/_redesign_tmp/closet_mobile_bottom.png")
                # item sheet
                page.locator("[data-testid=rack-row] button").first.click()
                page.wait_for_timeout(900)
                page.screenshot(path=f"{OUT}/_redesign_tmp/closet_sheet_mobile.png")
                page.keyboard.press("Escape")
                # Add reachable
                page.click("[data-testid=add-clothes]")
                page.wait_for_url("**/add")
                page.wait_for_timeout(800)
                print("add page title:", page.locator("h1").inner_text())
                page.screenshot(path=f"{OUT}/_redesign_tmp/add_mobile.png")
            ctx.close()

    if want("buy"):
        for name, opts in (("mobile", MOBILE), ("desktop", DESKTOP)):
            ctx, page = new_page(b, opts, tag=f"buy-{name}")
            page.goto(f"{BASE}/buy", wait_until="networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=f"{OUT}/_redesign_tmp/buy_start_{name}.png")
            buy_result(page)
            page.screenshot(path=f"{OUT}/redesign_buy_{name}.png")
            nav = page.locator("nav[aria-label=Main] > div").bounding_box()
            bar = page.get_by_role("button", name="Try another").bounding_box()
            print(name, "action bar bottom:", round(bar["y"] + bar["height"]), "nav top:", round(nav["y"]),
                  "(middle circle rises ~18px above)")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
            page.screenshot(path=f"{OUT}/_redesign_tmp/buy_result_end_{name}.png")
            ctx.close()

    if want("prefs"):
        for path, popts in (("/preferences", MOBILE), ("/settings", MOBILE), ("/preferences", DESKTOP)):
            ctx, page = new_page(b, popts, tag=f"prefs{path}")
            page.goto(f"{BASE}{path}", wait_until="networkidle")
            page.wait_for_selector("[data-testid=match-picker]")
            page.wait_for_timeout(600)
            print(path, "h1:", page.locator("h1").inner_text())
            if popts is DESKTOP:
                page.screenshot(path=f"{OUT}/redesign_prefs_desktop.png")
                page.goto(f"{BASE}/add", wait_until="networkidle")
                page.wait_for_timeout(500)
                page.screenshot(path=f"{OUT}/_redesign_tmp/add_desktop.png")
            elif path == "/preferences":
                page.screenshot(path=f"{OUT}/redesign_prefs_mobile.png")
                page.locator("[data-preset=chill]").click()  # make it dirty to show the sticky save bar (not saved)
                page.wait_for_timeout(500)
                page.screenshot(path=f"{OUT}/_redesign_tmp/prefs_dirty_mobile.png")
            ctx.close()

    if want("nav"):
        for name, opts in (("mobile", MOBILE), ("desktop", DESKTOP)):
            ctx, page = new_page(b, opts, tag=f"nav-{name}")
            page.goto(f"{BASE}/closet", wait_until="networkidle")
            page.wait_for_timeout(800)
            bar = page.locator("nav[aria-label=Main] > div")
            boxes = [page.get_by_role("link", name=n).bounding_box() for n in ("Closet", "Should I buy?", "Preferences")]
            bb = bar.bounding_box()
            print(name, "bar:", {k: round(v) for k, v in bb.items()},
                  "icon centers y:", [round(x["y"] + x["height"] / 2, 1) for x in boxes],
                  "sizes:", [f'{round(x["width"])}x{round(x["height"])}' for x in boxes],
                  "gaps:", [round(boxes[i + 1]["x"] - boxes[i]["x"] - boxes[i]["width"], 1) for i in range(2)])
            page.screenshot(path=f"{OUT}/nav_bar_{name}.png", clip={"x": bb["x"] - 24, "y": bb["y"] - 20,
                            "width": bb["width"] + 48, "height": bb["height"] + 36})
            if name == "mobile":
                page.goto(f"{BASE}/buy", wait_until="networkidle")
                page.wait_for_timeout(500)
                bar.screenshot(path=f"{OUT}/_redesign_tmp/nav_bar_buy_active.png")
            ctx.close()

    if want("loading"):
        ctx, page = new_page(b, MOBILE, tag="loading")
        page.route("**/api/closet/items*", lambda r: None)  # never answer -> skeleton stays
        page.goto(f"{BASE}/closet", wait_until="domcontentloaded")
        page.wait_for_selector("[aria-busy=true] [data-testid=rack-row]")
        page.wait_for_timeout(700)
        page.screenshot(path=f"{OUT}/redesign_loading_mobile.png")
        ctx.close()
        # evaluation skeleton: hold /api/evaluate
        ctx, page = new_page(b, MOBILE, tag="loading-eval")
        ev = json.load(urllib.request.urlopen(f"http://localhost:8000/api/evaluations/{EV}"))
        cand = ev["item"]
        detect = {"photo_id": "ph_demo", "image_url": cand["image_url"], "detector": "seed",
                  "items": [{**cand, "bbox": [0, 0, 1000, 1000]}]}
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(detect)))
        page.route("**/api/evaluate", lambda r: None)
        page.goto(f"{BASE}/buy", wait_until="networkidle")
        page.locator("input[type=file]").nth(1).set_input_files(IMG)
        page.wait_for_selector("input[type=number]", timeout=60000)
        page.fill("input[type=number]", "45")
        page.get_by_role("button", name="Evaluate").click()
        page.wait_for_selector("[data-testid=result-skeleton]")
        page.wait_for_timeout(600)
        page.locator("[data-testid=result-skeleton]").scroll_into_view_if_needed()
        page.screenshot(path=f"{OUT}/_redesign_tmp/loading_eval_mobile.png")
        ctx.close()

    if want("doors"):
        ctx, page = new_page(b, MOBILE, skip_doors=False, tag="doors")
        import datetime as _dt
        t0 = _dt.datetime(2026, 9, 26, 18, 0, 0)
        page.clock.install(time=t0)
        page.clock.pause_at(t0 + _dt.timedelta(seconds=1))  # timers only fire when we advance the clock
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_selector("[data-testid=closet-doors]")
        page.wait_for_timeout(1200)  # real time; fake clock keeps the doors shut
        page.screenshot(path=f"{OUT}/redesign_door_1.png")
        page.clock.run_for(500)  # -> opening
        page.wait_for_selector(".closet-doors.is-open")
        page.wait_for_timeout(330)
        page.screenshot(path=f"{OUT}/redesign_door_2.png")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{OUT}/redesign_door_3.png")
        page.clock.run_for(2500)
        page.wait_for_timeout(300)
        print("doors after finish:", page.locator("[data-testid=closet-doors]").count(),
              "seen flag:", page.evaluate("sessionStorage.getItem('fitcheck:doors-seen')"))
        page.reload(wait_until="networkidle")
        # desktop closed + mid frames for a visual check (not deliverables)
        dctx, dpage = new_page(b, DESKTOP, skip_doors=False, tag="doors-desktop")
        dpage.clock.install(time=t0)
        dpage.clock.pause_at(t0 + _dt.timedelta(seconds=1))
        dpage.goto(f"{BASE}/closet", wait_until="domcontentloaded")
        dpage.wait_for_selector("[data-testid=closet-doors]")
        dpage.wait_for_timeout(1000)
        dpage.screenshot(path=f"{OUT}/_redesign_tmp/door_desktop_1.png")
        dpage.clock.run_for(500)
        dpage.wait_for_selector(".closet-doors.is-open")
        dpage.wait_for_timeout(450)
        dpage.screenshot(path=f"{OUT}/_redesign_tmp/door_desktop_2.png")
        dctx.close()
        print("doors visible after reload:", page.locator("[data-testid=closet-doors]").is_visible() if page.locator("[data-testid=closet-doors]").count() else False)
        ctx.close()

    if want("console"):
        for path in ("/", "/closet", "/buy", "/preferences", "/settings", "/add"):
            for name, opts in (("mobile", MOBILE), ("desktop", DESKTOP)):
                ctx, page = new_page(b, opts, skip_doors=(path != "/"), tag=f"console{path}-{name}")
                page.goto(f"{BASE}{path}", wait_until="networkidle")
                page.wait_for_timeout(2600)
                ctx.close()
    b.close()

print("\nCONSOLE PROBLEMS:" if problems else "\nno console errors/warnings/hydration messages")
for x in problems:
    print(" ", x)
