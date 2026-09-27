"""Paste-a-link on "Should I buy?" (app/product_link.py, POST /api/detect-url).

Fully offline: pages come from tests/fixtures/product_link via httpx.MockTransport, DNS is monkeypatched, and the
photo pipeline (pipeline.detect: Gemini + segformer) is stubbed, so no network, no quota and no model downloads."""
import io
import socket
from pathlib import Path

import httpx
import pytest
from PIL import Image

FIX = Path(__file__).parent / "fixtures" / "product_link"
PUBLIC_IP = "93.184.216.34"


def jpeg(bg=(255, 255, 255), fg=(90, 120, 90), size=(400, 500)) -> bytes:
    img = Image.new("RGB", size, bg)
    img.paste(Image.new("RGB", (size[0] // 2, size[1] // 2), fg), (size[0] // 4, size[1] // 4))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


WHITE = jpeg()
BUSY = jpeg(bg=(180, 90, 60))


def routes_transport(routes: dict, log: list | None = None):
    """routes: url (without query) -> (status, content_type, body) | ('redirect', location)."""
    def handler(req: httpx.Request):
        key = str(req.url.copy_with(query=None))
        if log is not None:
            log.append(str(req.url))
        r = routes.get(str(req.url)) or routes.get(key)
        if r is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        if r[0] == "redirect":
            return httpx.Response(302, headers={"location": r[1]})
        st, ct, body = r
        return httpx.Response(st, content=body if isinstance(body, bytes) else body.encode(), headers={"content-type": ct})
    return httpx.MockTransport(handler)


def html(name):
    return (FIX / name).read_text()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    from app import product_link
    private = {"intranet.test": "10.0.0.5", "metadata.test": "169.254.169.254", "rebind.test": "127.0.0.1"}
    monkeypatch.setattr(product_link, "_resolve", lambda h: [private.get(h, PUBLIC_IP)])


def client(routes, log=None):
    from app.product_link import make_client
    return make_client(transport=routes_transport(routes, log))


# ------------------------------------------------------------------ URL validation / SSRF
@pytest.mark.parametrize("url", ["", "   ", "ftp://store.test/x", "javascript:alert(1)", "file:///etc/passwd",
                                 "mailto:a@b.test", "http://user:pw@store.test/p", "https://store.test:8443/p"])
def test_rejects_non_web_urls(url):
    from app.product_link import LinkError, check_public_url
    with pytest.raises(LinkError):
        check_public_url(url)


@pytest.mark.parametrize("url", ["http://localhost/x", "http://127.0.0.1/", "http://[::1]/", "http://10.1.2.3/",
                                 "http://192.168.0.10/", "http://169.254.169.254/latest/meta-data",
                                 "http://intranet.test/p", "http://metadata.test/", "http://printer.local/",
                                 "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "localhost:3000/products/x"])
def test_blocks_private_addresses(url):
    from app.product_link import LinkError, check_public_url
    with pytest.raises(LinkError) as e:
        check_public_url(url)
    assert e.value.status == 400


def test_allow_nets_override_never_unblocks_loopback(monkeypatch):
    from app import product_link
    monkeypatch.setenv("FITCHECK_LINK_ALLOW_NETS", "198.18.0.0/15,127.0.0.0/8")
    monkeypatch.setattr(product_link, "_resolve", lambda h: ["198.18.0.7"])
    assert product_link.check_public_url("https://fake-ip.test/p") == "https://fake-ip.test/p"
    with pytest.raises(product_link.LinkError):
        product_link.check_public_url("http://127.0.0.1/")
    monkeypatch.delenv("FITCHECK_LINK_ALLOW_NETS")
    with pytest.raises(product_link.LinkError):
        product_link.check_public_url("https://fake-ip.test/p")


def test_bare_domain_gets_https_and_fragment_dropped():
    from app.product_link import check_public_url
    assert check_public_url(" Store.test/products/tee?variant=1#reviews ") == "https://store.test/products/tee?variant=1"


def test_unknown_host_is_a_friendly_error(monkeypatch):
    from app import product_link
    def boom(h):
        raise socket.gaierror("nope")
    monkeypatch.setattr(product_link, "_resolve", boom)
    with pytest.raises(product_link.LinkError, match="Couldn't find the website"):
        product_link.check_public_url("https://no-such-store.test/p")


def test_redirect_to_private_address_is_blocked():
    from app.product_link import LinkError, scrape
    routes = {"https://store.test/p/1": ("redirect", "http://169.254.169.254/latest/meta-data")}
    with pytest.raises(LinkError, match="private address"):
        scrape("https://store.test/p/1", client=client(routes))


def test_private_image_urls_are_never_fetched():
    from app.product_link import scrape, LinkError
    page = '<meta property="og:title" content="Tee"><meta property="og:image" content="http://10.0.0.9/secret.jpg">'
    log = []
    with pytest.raises(LinkError, match="usable product photo"):
        scrape("https://store.test/p/tee", client=client({"https://store.test/p/tee": (200, "text/html", page)}, log))
    assert not any("10.0.0.9" in u for u in log)


# ------------------------------------------------------------------ extraction
def test_shopify_product_json_wins_and_prefers_flat_lay_photo():
    from app.product_link import scrape
    routes = {
        "https://store.test/products/organic-linen-camp-shirt": (200, "text/html; charset=utf-8", html("shopify_page.html")),
        "https://store.test/products/organic-linen-camp-shirt.js": (200, "application/json", html("shopify_product.js")),
        "https://cdn.shopify.test/files/camp-shirt-flat.jpg": (200, "image/jpeg", WHITE),
        "https://cdn.shopify.test/files/camp-shirt-model.jpg": (200, "image/jpeg", BUSY),
    }
    s = scrape("https://store.test/products/organic-linen-camp-shirt", client=client(routes))
    L = s["listing"]
    assert L["source"] == "shopify" and L["title"] == "Organic Linen Camp Shirt"
    assert L["brand"] == "Marine Layer" and L["retailer"] == "Marine Layer"
    assert L["price"] == 98.0 and L["currency"] == "USD" and L["color"] == "Sage"
    assert L["description"] == "Breezy organic linen camp-collar shirt." and L["product_type"] == "Shirts"
    assert s["image_source_url"].endswith("camp-shirt-flat.jpg") and s["photo_kind"] == "product_only"
    assert s["image_bytes"] == WHITE


def test_shopify_variant_in_url_picks_variant_price_color_and_photo():
    from app.product_link import scrape
    routes = {
        "https://store.test/products/organic-linen-camp-shirt": (200, "text/html", html("shopify_page.html")),
        "https://store.test/products/organic-linen-camp-shirt.js": (200, "application/json", html("shopify_product.js")),
        "https://cdn.shopify.test/files/camp-shirt-navy-flat.jpg": (200, "image/jpeg", WHITE),
    }
    s = scrape("https://store.test/products/organic-linen-camp-shirt?variant=222", client=client(routes))
    assert s["listing"]["price"] == 88.0 and s["listing"]["color"] == "Navy"
    assert s["image_source_url"].endswith("camp-shirt-navy-flat.jpg")


def test_bot_walled_shopify_page_still_works_via_product_json():
    from app.product_link import scrape
    routes = {
        "https://store.test/products/organic-linen-camp-shirt": (403, "text/html", html("bot_wall.html")),
        "https://store.test/products/organic-linen-camp-shirt.js": (200, "application/json", html("shopify_product.js")),
        "https://cdn.shopify.test/files/camp-shirt-flat.jpg": (200, "image/jpeg", WHITE),
    }
    s = scrape("https://store.test/products/organic-linen-camp-shirt", client=client(routes))
    assert s["listing"]["title"] == "Organic Linen Camp Shirt" and s["listing"]["price"] == 98.0


def test_json_ld_product_page():
    from app.product_link import scrape
    routes = {
        "https://www.example-store.test/p/wide-leg-trousers": (200, "text/html", html("jsonld_page.html")),
        "https://img.example-store.test/trousers-1.jpg": (200, "image/jpeg", BUSY),
        "https://img.example-store.test/trousers-2.jpg": (200, "image/jpeg", WHITE),
    }
    s = scrape("https://www.example-store.test/p/wide-leg-trousers", client=client(routes))
    L = s["listing"]
    assert L["source"] == "json-ld" and L["title"] == "Wide-Leg Pleated Trousers" and L["brand"] == "Example Studio"
    assert L["price"] == 129.5 and L["currency"] == "USD" and L["color"] == "Charcoal"
    assert L["description"] == "High-rise wide-leg trousers in a wool blend."
    assert L["retailer"] == "example-store.test"
    assert s["image_source_url"].endswith("trousers-2.jpg")  # the white-background shot beats the first photo


def test_og_meta_only_page_with_relative_image_and_redirect():
    from app.product_link import scrape
    routes = {
        "https://denim.test/jacket": ("redirect", "/en-us/jacket"),
        "https://denim.test/en-us/jacket": (200, "text/html", html("meta_only.html")),
        "https://denim.test/media/jacket.jpg": (200, "image/jpeg", BUSY),
    }
    s = scrape("https://denim.test/jacket", client=client(routes))
    L = s["listing"]
    assert s["final_url"] == "https://denim.test/en-us/jacket"
    assert L["title"] == "Cropped Denim Jacket" and L["retailer"] == "Denim Co" and L["price"] == 74.0
    assert L["description"] == "A & classic cropped jacket in light wash denim."
    assert L["color"] == "blue"  # no listed color: a color word in the title ("denim")
    assert s["image_source_url"] == "https://denim.test/media/jacket.jpg" and s["photo_kind"] == "photo"


def test_non_usd_price_is_not_prefilled():
    from app.product_link import scrape
    routes = {"https://shop.example.eu/sweater": (200, "text/html", html("eur_page.html")),
              "https://shop.example.eu/sweater.jpg": (200, "image/jpeg", WHITE)}
    L = scrape("https://shop.example.eu/sweater", client=client(routes))["listing"]
    assert L["price"] is None and L["currency"] == "EUR" and L["title"] == "Merino Crew Sweater"


def test_direct_image_link():
    from app.product_link import scrape
    s = scrape("https://cdn.store.test/tee.jpg", client=client({"https://cdn.store.test/tee.jpg": (200, "image/jpeg", WHITE)}))
    assert s["image_bytes"] == WHITE and s["listing"]["source"] == "image"


@pytest.mark.parametrize("routes,match", [
    ({"https://store.test/p": (403, "text/html", html("bot_wall.html"))}, "blocks automated access"),
    ({"https://store.test/p": (200, "text/html", html("bot_wall.html"))}, "blocks automated access"),
    ({"https://store.test/p": (200, "text/html", html("no_image.html"))}, "Couldn't find a product photo"),
    ({}, "doesn't exist"),
    ({"https://store.test/p": ("redirect", "https://store.test/search?q=sold-out-tee"),
      "https://store.test/search": (200, "text/html", html("meta_only.html"))}, "doesn't open a single product"),
    ({"https://store.test/p": (500, "text/html", "oops")}, "Couldn't read that page"),
])
def test_friendly_errors_suggest_uploading_a_photo(routes, match):
    from app.product_link import LinkError, scrape
    with pytest.raises(LinkError, match=match) as e:
        scrape("https://store.test/p", client=client(routes))
    assert e.value.status == 422
    if match not in ("doesn't exist", "doesn't open a single product"):
        assert "upload" in str(e.value).lower()


def test_timeout_is_a_friendly_error():
    from app.product_link import LinkError, make_client, scrape
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(LinkError, match="took too long") as e:
        scrape("https://slow.test/p", client=make_client(transport=httpx.MockTransport(handler)))
    assert e.value.status == 504


def test_listing_attributes_never_override_a_tag_price():
    from app.product_link import listing_attributes
    L = {"title": "Camp Shirt", "brand": "Marine Layer", "retailer": "Marine Layer", "price": 98.0, "currency": "USD",
         "color": "Sage", "description": "Linen."}
    a = listing_attributes(L, "https://store.test/products/x", {"brand": None, "price": None, "source": "gemini",
                                                                  "primary_color": "green"})
    assert a["price"] == 98.0 and a["price_source"] == "listing" and a["brand"] == "Marine Layer"
    assert a["product_url"] == "https://store.test/products/x" and a["listing_title"] == "Camp Shirt"
    assert a["primary_color"] == "green"  # Gemini's color stays
    b = listing_attributes(L, "u", {"price": 60, "price_source": "tag", "brand": "Other", "source": "fashion-clip-zero-shot"})
    assert b["price"] == 60 and b["price_source"] == "tag" and b["brand"] == "Other" and b["listing_price"] == 98.0
    assert b["primary_color"] == "green"  # 'sage' -> green from the listing for the offline detector


def test_listing_price_counts_as_a_real_price_for_the_verdict():
    from app.pricing import effective_price
    p = effective_price({"price": 98.0, "price_source": "listing", "category": "top", "subcategory": "shirt"})
    assert p["price"] == 98.0 and p["price_source"] != "estimated"


# ------------------------------------------------------------------ end to end (pipeline stubbed) + API
def _stub_pipeline(monkeypatch, n_items=2):
    """pipeline.detect stand-in: saves 'detected' items like the real one, without Gemini / segformer."""
    from app import config, db, pipeline
    db.init_db()
    seen = {}

    def fake_detect(data, purpose=None, source="upload"):
        seen["bytes"], seen["purpose"], seen["source"] = data, purpose, source
        pid = db.insert_photo("/tmp/x.jpg", purpose, 400, 500, source)
        specs = [("bottom", "trousers", [500, 300, 950, 700]), ("top", "shirt", [100, 200, 520, 800])][:n_items]
        items = []
        for cat, sub, bbox in specs:
            iid = db.insert_item(status="detected", category=cat, photo_id=pid, bbox=bbox, label=sub, source=source,
                                 attributes={"category": cat, "subcategory": sub, "primary_color": "green",
                                             "brand": None, "price": None, "source": "gemini",
                                             "estimated_price_usd": 60, "price_confidence": "low"})
            items.append(pipeline.item_to_api(db.get_item(iid)))
        return {"photo_id": pid, "image_url": "/media/originals/x.jpg", "detector": "gemini", "items": items,
                "skipped_accessories": 0, "message": None}
    monkeypatch.setattr(pipeline, "detect", fake_detect)
    return seen


SHOP_ROUTES = {
    "https://store.test/products/organic-linen-camp-shirt": (200, "text/html", html("shopify_page.html")),
    "https://store.test/products/organic-linen-camp-shirt.js": (200, "application/json", html("shopify_product.js")),
    "https://cdn.shopify.test/files/camp-shirt-flat.jpg": (200, "image/jpeg", WHITE),
}


def test_detect_from_url_runs_the_photo_pipeline_and_tags_the_listed_item(monkeypatch):
    from app import db, product_link
    seen = _stub_pipeline(monkeypatch)
    res = product_link.detect_from_url("https://store.test/products/organic-linen-camp-shirt", "candidate",
                                       client=client(SHOP_ROUTES))
    assert seen == {"bytes": WHITE, "purpose": "candidate", "source": "url"}
    assert res["source"] == "url" and len(res["items"]) == 2
    first = res["items"][0]
    assert res["suggested_item_id"] == first["id"] and first["category"] == "top"  # "Shirt" listing -> the top
    a = first["attributes"]
    assert a["price"] == 98.0 and a["price_source"] == "listing" and a["brand"] == "Marine Layer"
    assert a["product_url"] == "https://store.test/products/organic-linen-camp-shirt"
    assert db.get_item(first["id"])["attributes"]["listing_title"] == "Organic Linen Camp Shirt"
    other = res["items"][1]["attributes"]
    assert other["price"] is None and "product_url" not in other
    p = res["product"]
    assert p["title"] == "Organic Linen Camp Shirt" and p["price"] == 98.0 and p["photo_kind"] == "product_only"
    assert "images" not in p


def test_detect_url_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, product_link
    _stub_pipeline(monkeypatch, n_items=1)
    real = product_link.scrape
    monkeypatch.setattr(product_link, "scrape", lambda url, client=None: real(url, client=globals()["client"](SHOP_ROUTES)))
    tc = TestClient(main.app)  # no `with`: skip the startup warmup (model loading)
    r = tc.post("/api/detect-url", json={"url": "store.test/products/organic-linen-camp-shirt"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["items"][0]["attributes"]["price"] == 98.0 and d["product"]["brand"] == "Marine Layer"
    r = tc.post("/api/detect-url", json={"url": "http://169.254.169.254/latest/meta-data"})
    assert r.status_code == 400 and "private" in r.json()["detail"]
    r = tc.post("/api/detect-url", json={"url": "https://store.test/products/x", "purpose": "nope"})
    assert r.status_code == 422
