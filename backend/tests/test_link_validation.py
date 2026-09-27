"""Shared live-shopping link validation (app.suggest.check_link / fetch_product): product links must resolve to a
store page that shows THIS product -- never a YouTube video, social post, blog, search page or another garment."""
import pytest

JACKET = {"name": "Denim Trucker Jacket", "brand": "Levi's", "retailer": "Levi's", "category": "outerwear",
          "subcategory": "jacket", "_category": "outerwear", "product_url": "https://www.levi.com/US/p/723340134",
          "image_url": None, "_span": (0, 0)}


def page(title, og=None):
    return (f"<html><head><title>{title}</title>"
            + (f'<meta property="og:title" content="{og}">' if og else "")
            + '<meta property="og:image" content="https://cdn.example.com/p.jpg"></head></html>')


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=s2gWov0Da-0", "https://youtu.be/abc", "https://m.youtube.com/shorts/x",
    "https://www.tiktok.com/@a/video/1", "https://www.instagram.com/p/x/", "https://www.pinterest.com/pin/1/",
    "https://www.reddit.com/r/malefashion/comments/1", "https://x.com/a/status/1", "https://en.wikipedia.org/wiki/Jeans",
    "https://www.gq.com/story/best-denim-jackets", "https://blog.store.com/denim-guide",
    "https://www.nordstrom.com/blog/denim-jackets", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x"])
def test_non_shopping_destinations_rejected(url):
    from app.suggest import check_link
    ok, why = check_link(JACKET, url, 200, page("Denim Trucker Jacket review"))
    assert not ok, url


def test_search_and_error_pages_rejected():
    from app.suggest import check_link
    for url in ("https://oldnavy.gap.com/browse/GeneralNoResults.do", "https://www.target.com/s/denim+jacket",
                "https://www.levi.com/search?q=trucker", "https://www.levi.com/"):
        assert not check_link(JACKET, url, 200, page("Denim Trucker Jacket"))[0], url


def test_denim_jacket_card_never_links_to_a_shoe_page():
    from app.suggest import check_link, text_matches_product
    url = "https://www.levi.com/US/en_US/p/A1234"
    assert not check_link(JACKET, url, 200, page("Men's Leather Sneakers | Levi's", "Leather Sneakers"))[0]
    assert not text_matches_product(JACKET, "Nike Air Force 1 '07 Men's Shoes")[0]
    assert check_link(JACKET, url, 200, page("Levi's Trucker Jacket - Dark Wash", "The Trucker Jacket"))[0]
    # brand name alone isn't a match
    assert not check_link(JACKET, url, 200, page("Levi's® Official Site"))[0]


def test_bot_walled_store_page_needs_the_product_in_the_url():
    from app.suggest import check_link
    ok_url = "https://www.levi.com/US/en_US/clothing/men/outerwear/trucker-jacket/p/723340134"
    assert check_link(JACKET, ok_url, 403, "")[0]
    assert not check_link(JACKET, "https://www.levi.com/US/en_US/p/723340134", 403, "")[0]
    nb = {"name": "997H", "brand": "New Balance", "retailer": "New Balance", "category": "shoes", "_category": "shoes"}
    assert check_link(nb, "https://www.newbalance.com/pd/997h/CM997HV1-41914.html", 403, "")[0]


def test_page_title_text_reads_title_og_and_jsonld():
    from app.suggest import page_title_text
    h = ('<title>Shop</title><meta content="Chore Coat" property="og:title">'
         '<script type="application/ld+json">{"@type": "Product", "sku": "1", "name": "Gramercy Denim Chore Jacket"}'
         '</script>')
    t = page_title_text(h)
    assert "Chore Coat" in t and "Gramercy Denim Chore Jacket" in t


def test_fetch_product_skips_video_grounding_link_and_uses_matching_store_page(monkeypatch):
    """The reported bug: stated URL 404s, the grounding chunk behind the product is a YouTube video about shoes.
    The video must be skipped, the next candidate (a real store page for the jacket) used, and its photo taken."""
    from PIL import Image
    import io
    from app import suggest
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), "navy").save(buf, "JPEG")
    pages = {
        "https://www.levi.com/US/p/723340134": (404, "https://www.levi.com/US/p/723340134", "text/html", b""),
        "https://redirect/video": (200, "https://www.youtube.com/watch?v=s2gWov0Da-0", "text/html",
                                   page("Best sneakers of 2026 - YouTube").encode()),
        "https://redirect/store": (200, "https://www.levi.com/US/en_US/trucker-jacket/p/723340134", "text/html",
                                   page("The Trucker Jacket | Levi's").encode()),
        "https://cdn.example.com/p.jpg": (200, "https://cdn.example.com/p.jpg", "image/jpeg", buf.getvalue()),
    }
    monkeypatch.setattr(suggest, "_get", lambda c, u, accept, mx: pages.get(u, (None, u, None, b"")))
    suggest._page_cache.clear()
    monkeypatch.setattr(suggest, "grounding_urls_for", lambda p, g: ["https://redirect/video", "https://redirect/store"])
    out = suggest.fetch_product(dict(JACKET), {})
    assert out["product_url"] == "https://www.levi.com/US/en_US/trucker-jacket/p/723340134"
    assert out["link_ok"] is True and out["image"] is not None and not out.get("fetch_error")
    assert out["image_source_url"] == "https://cdn.example.com/p.jpg"
    # only the video: product dropped, no link shown
    suggest._page_cache.clear()
    monkeypatch.setattr(suggest, "grounding_urls_for", lambda p, g: ["https://redirect/video"])
    out = suggest.fetch_product(dict(JACKET), {})
    assert out["product_url"] is None and out["image"] is None and "no store link" in out["fetch_error"]



WRANGLER = """<html><head><title>Wrangler\u00ae Blanket Lined Denim Jacket | COLLECTIONS | Wrangler\u00ae</title>
<meta property="og:title" content="Blanket Lined Denim Jacket">
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "BreadcrumbList"},
 {"@type": "Product", "name": "Wrangler&reg; Blanket Lined Denim Jacket", "brand": {"@type": "Brand", "name": "Wrangler"},
  "image": ["//www.wrangler.com/img/flat.jpg", "https://www.wrangler.com/img/model.jpg"], "category": "Jackets",
  "offers": [{"@type": "Offer", "price": "79.00", "priceCurrency": "USD"}]}]}</script></head></html>"""


def test_page_product_info_and_clean_name():
    from app.suggest import clean_product_name, page_product_info
    info = page_product_info(WRANGLER)
    assert info["price"] == 79.0 and info["brand"] == "Wrangler" and "Jackets" in info["types"]
    assert info["images"][0] == "https://www.wrangler.com/img/flat.jpg"
    p = {"brand": "Wrangler", "retailer": "Wrangler"}
    assert clean_product_name(info["name"], p) == "Blanket Lined Denim Jacket"
    assert clean_product_name("Wrangler\u00ae Blanket Lined Denim Jacket | COLLECTIONS | Wrangler\u00ae", p) == \
        "Blanket Lined Denim Jacket"
    assert clean_product_name("Men's Premier Low Top In White Leather - Thursday Boot Company",
                              {"retailer": "Thursday Boot Co."}) == "Men's Premier Low Top In White Leather"
    assert page_product_info("") == {}
    meta_only = '<meta property="og:title" content="Chino Pant"><meta property="product:price:amount" content="45.00">'
    assert page_product_info(meta_only)["price"] == 45.0


# ------------------------------------------------------------------ store blocklist: no J.Crew, ever
@pytest.mark.parametrize("p", [
    {"name": "Wallace & Barnes Chore Jacket", "brand": "Wallace & Barnes", "retailer": "J.Crew",
     "product_url": "https://www.jcrew.com/p/mens/categories/clothing/BH123"},
    {"name": "Broken-in Chino", "brand": "Other", "retailer": "Other", "product_url": "https://factory.jcrew.com/p/x"},
    {"name": "Slim Oxford Shirt", "brand": "J.Crew Factory", "retailer": "Nordstrom",
     "product_url": "https://www.nordstrom.com/s/oxford/1"},
    {"name": "Crewneck Sweater", "brand": "J. Crew", "retailer": "Macy's", "product_url": "https://www.macys.com/p/1"},
    {"name": "J.Crew Classic Rain Jacket", "brand": None, "retailer": "Poshmark", "product_url": "https://poshmark.com/l/1"},
    {"name": "Chino", "brand": "X", "retailer": "X", "product_url": "https://x.com/p/1",
     "image_url": "https://www.jcrew.com/s7-img-facade/BH123.jpg"},
])
def test_blocked_store_is_dropped(p):
    from app.suggest import blocked_store, drop_blocked
    assert blocked_store(p), p
    rej = []
    assert drop_blocked([p], rej) == [] and "blocked store" in rej[0]["reason"]


def test_blocklist_keeps_other_stores_and_crewnecks():
    from app.suggest import blocked_host, blocked_store, blocked_stores_prompt, stores
    for p in ({"name": "Crewneck Sweatshirt", "brand": "Gap", "retailer": "Gap", "product_url": "https://www.gap.com/p/1"},
              {"name": "Crew Socks-free Loafer", "brand": "Crew Clothing", "retailer": "Crew Clothing",
               "product_url": "https://www.crewclothing.co.uk/p/1"},
              {"name": "Chino", "brand": "Madewell", "retailer": "Madewell", "product_url": "https://notjcrew.com/p/1"}):
        assert blocked_store(p) is None, p
    assert blocked_host("https://jcrew.com/p/1") == "jcrew.com" and blocked_host("https://factory.jcrew.com/") == "jcrew.com"
    assert not any(blocked_host("https://" + d) for d, _, _ in stores())
    assert "J.Crew" in blocked_stores_prompt() and "J.Crew Factory" in blocked_stores_prompt()


def test_search_prompts_tell_gemini_no_jcrew():
    from app import suggest, wardrobe_suggest as ws
    summary = {"size": 1, "categories": {"top": {"count": 1, "colors": ["black"], "types": ["t-shirt"]}}}
    cand = {"category": "top", "attributes": {"subcategory": "t-shirt", "primary_color": "black"}}
    for mode in ("alternatives", "pairings"):
        pr = suggest.build_prompt(cand, mode, summary, {"verdict": {"decision": "SKIP", "reasons": []}})
        assert "NEVER suggest products from J.Crew" in pr and "Old Navy, J.Crew" not in pr
        assert "J.Crew" in suggest.build_plan_prompt(cand, mode, summary, {"verdict": {"decision": "SKIP", "reasons": []}})
    closet = [{"category": "top", "attributes": {"primary_color": "black", "subcategory": "t-shirt"}}]
    for grounded in (True, False):
        assert "NEVER suggest products from J.Crew" in ws.build_prompt(closet, summary, "mens", grounded=grounded)


def test_fetch_product_skips_a_jcrew_page_for_a_non_jcrew_product(monkeypatch):
    from app import suggest
    pages = {"https://redirect/jc": (200, "https://www.jcrew.com/p/trucker-jacket/BH1", "text/html",
                                     page("Levi's Trucker Jacket | J.Crew").encode())}
    monkeypatch.setattr(suggest, "_get", lambda c, u, accept, mx: pages.get(u, (None, u, None, b"")))
    suggest._page_cache.clear()
    monkeypatch.setattr(suggest, "grounding_urls_for", lambda p, g: ["https://redirect/jc"])
    out = suggest.fetch_product({**JACKET, "product_url": None}, {})
    assert out["product_url"] is None and "blocked store" in out["fetch_error"]


def test_card_color_follows_the_page_and_photo_not_gemini():
    """The Old Navy bomber bug: Gemini said olive, the linked variant / photo is black."""
    from PIL import Image
    from app import suggest
    black = Image.new("RGB", (200, 200), "white")
    black.paste(Image.new("RGB", (120, 140), (20, 20, 22)), (40, 30))
    p = {"color": "olive", "product_url": "https://oldnavy.gap.com/browse/product.do?pid=1"}
    suggest.verify_color(p, {"name": "Water-Resistant Zip Bomber Jacket for Men"}, black)
    assert p["color"] == "black" and p["color_verified"] and p["gemini_color"] == "olive"
    p = {"color": "olive", "product_url": "https://x.com/p/1?color=navy"}
    suggest.verify_color(p, {}, None)
    assert p["color"] == "navy" and p["color_verified"]
    p = {"color": "olive", "product_url": "https://x.com/p/1"}
    suggest.verify_color(p, {"color": "Olive Night"}, None)
    assert p["color"] == "olive" and p["color_verified"]
    p = {"color": "olive", "product_url": "https://x.com/p/1"}
    suggest.verify_color(p, {}, None)
    assert p["color_verified"] is False
    assert suggest.first_color("Sail/Gum Yellow/Varsity Royal") == "cream"
    assert suggest.first_color("Black/White") == "black" and suggest.first_color("Beech") is None
    assert suggest.page_product_info('<script type="application/ld+json">{"@type": "Product", "name": "Bomber", '
                                     '"color": "Black"}</script>')["color"] == "Black"
