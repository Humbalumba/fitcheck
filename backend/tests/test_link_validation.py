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
