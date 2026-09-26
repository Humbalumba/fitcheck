"""Clean product images (app/render.py): option 2 cleanup, verification, quota fallback, API, add-item robustness.
Gemini is mocked everywhere (conftest sets FITCHECK_GEMINI_OFF=1; we monkeypatch the two Gemini call sites)."""
import time

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import db, render


# ------------------------------------------------------------------ helpers
def _garment(angle: float = 0.0, color=(30, 60, 160)) -> Image.Image:
    """Synthetic 'garment' cutout: a tall textured rectangle with a white logo square, rotated, on transparency."""
    w, h = 220, 420
    g = Image.new("RGBA", (w, h), color + (255,))
    d = ImageDraw.Draw(g)
    for y in range(0, h, 12):  # faint 'wrinkle' texture
        d.line([(0, y), (w, y + 6)], fill=(color[0] + 12, color[1] + 12, color[2] + 12, 255), width=2)
    d.rectangle([80, 90, 140, 150], fill=(250, 250, 250, 255))  # logo
    canvas = Image.new("RGBA", (600, 600), (120, 90, 60, 0))
    canvas.paste(g, (190, 90), g)
    return canvas.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(120, 90, 60, 0))


class FakeQuotaError(Exception):
    code = 429

    def __init__(self, model="gemini-3.1-flash-image"):
        super().__init__("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, "
                         "please check your plan and billing details. * Quota exceeded for metric: generativelanguage."
                         f"googleapis.com/generate_content_free_tier_requests, limit: 0, model: {model}'}}}}")


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Isolated Gemini-render state: temp state file, nothing blocked, Gemini 'configured', renders inline."""
    monkeypatch.setenv("RENDER_STATE_PATH", str(tmp_path / "render_state.json"))
    monkeypatch.setattr(render, "_state", None)
    monkeypatch.setattr(render, "_model_blocked", {})
    monkeypatch.setattr(render, "RENDER_GEMINI", "auto")
    monkeypatch.setattr(render, "_gemini_configured", lambda: True)
    monkeypatch.setattr(render, "SYNC", True)
    monkeypatch.setattr(render, "RENDER_ENABLED", True)
    yield tmp_path


@pytest.fixture(autouse=True, scope="module")
def _cleanup_clean_dir():
    yield
    d = render.clean_dir()
    for p in d.glob("*"):
        p.unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:
        pass


def _a_closet_item(category="top"):
    return next(it for it in db.list_items(status="closet") if it["category"] == category)


# ------------------------------------------------------------------ option 2: deterministic cleanup
def test_cleanup_size_white_background_and_deterministic():
    g = _garment(0)
    out1, info = render.cleanup_render(g)
    out2, _ = render.cleanup_render(g)
    assert out1.size == (render.RENDER_SIZE, render.RENDER_SIZE) and out1.mode == "RGB"
    assert np.array_equal(np.asarray(out1), np.asarray(out2)), "cleanup must be deterministic"
    a = np.asarray(out1)
    for y, x in [(0, 0), (0, -1), (-1, 0), (-1, -1), (10, 512), (512, 10)]:
        assert tuple(a[y, x]) == (255, 255, 255)
    assert info["mask"] == "segformer"
    # garment centred with padding
    fg = render.garment_mask_on_white(out1)
    ys, xs = np.nonzero(fg)
    assert abs((xs.min() + xs.max()) / 2 - 512) < 25 and abs((ys.min() + ys.max()) / 2 - 512) < 40
    assert ys.min() > 0.04 * 1024 and ys.max() < 0.96 * 1024


def test_cleanup_straightens_tilted_garment_and_keeps_logo():
    out, info = render.cleanup_render(_garment(15))
    assert abs(abs(info["tilt_deg"]) - 15) < 3, info["tilt_deg"]
    fg = render.garment_mask_on_white(out)
    assert render.estimate_tilt(fg) == 0.0  # now axis-aligned
    ys, xs = np.nonzero(fg)
    assert (ys.max() - ys.min()) > 1.6 * (xs.max() - xs.min())  # still upright (tall), not turned sideways
    a = np.asarray(out).astype(int)
    # the white 'logo' survives inside the blue garment and the garment colour is preserved
    inside = a[fg]
    assert (inside.min(axis=1) > 225).sum() > 1500
    blue = inside[(inside[:, 2] > inside[:, 0] + 60)]
    assert len(blue) > 0.5 * len(inside)
    ref = np.array([[30, 60, 160]])
    assert render.color_distance(render.rgb_to_lab(ref)[0], render.rgb_to_lab(blue.mean(0, keepdims=True))[0]) < 8


def test_cleanup_never_rotates_squarish_or_crumpled_shapes():
    m = np.zeros((300, 300), bool)
    m[60:240, 50:250] = True  # squarish
    assert render.estimate_tilt(m) == 0.0
    rng = np.random.default_rng(0)
    blob = np.zeros((300, 300), bool)
    for _ in range(12):  # irregular union of discs
        cy, cx, r = rng.integers(80, 220), rng.integers(80, 220), rng.integers(25, 60)
        yy, xx = np.ogrid[:300, :300]
        blob |= (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
    assert abs(render.estimate_tilt(blob)) <= render.RENDER_MAX_TILT


def test_cleanup_on_real_closet_cutout():
    it = _a_closet_item("bottom")
    out, info = render.cleanup_render(Image.open(it["cutout_path"]), Image.open(it["crop_path"]), category="bottom")
    assert out.size == (1024, 1024)
    assert tuple(np.asarray(out)[0, 0]) == (255, 255, 255)
    assert render.garment_mask_on_white(out).mean() > 0.05


# ------------------------------------------------------------------ verification (Gemini mocked)
def _verify(monkeypatch, *, clip=0.9, llm=None, llm_error=None, text=False, render_img=None):
    item = {"id": "x", "label": "navy tee", "attributes": {"brand": "Acme"} if text else {"pattern": "solid"}}
    ref, _ = render.cleanup_render(_garment(0))
    ref_px = np.asarray(_garment(0).convert("RGB"))[np.asarray(_garment(0))[..., 3] > 127]
    monkeypatch.setattr(render, "clip_similarity", lambda a, b: clip)

    def fake_llm(orig, ren, it):
        if llm_error:
            raise llm_error
        return {"same_garment": True, "has_text_or_logo": text, "text_matches": True, "color_matches": True,
                "added_elements": False, "issues": [], "model": "mock-lite", **(llm or {})}
    monkeypatch.setattr(render, "_gemini_verify_call", fake_llm)
    return render.verify_render(item, render_img or ref, ref, ref_px, None)


def test_verify_passes_for_faithful_render(monkeypatch):
    c = _verify(monkeypatch)
    assert c["passed"] and c["clip"]["passed"] and c["color"]["passed"] and c["gemini"]["same_garment"]


def test_verify_fails_on_text_mismatch_low_clip_or_colour(monkeypatch):
    c = _verify(monkeypatch, text=True, llm={"text_matches": False, "issues": ["logo text reads 'ACNE' not 'ACME'"]})
    assert not c["passed"] and "ACME" in " ".join(c["issues"])
    assert not _verify(monkeypatch, clip=0.5)["passed"]
    red, _ = render.cleanup_render(_garment(0, color=(200, 30, 30)))
    c = _verify(monkeypatch, render_img=red)
    assert not c["color"]["passed"] and not c["passed"]
    assert not _verify(monkeypatch, llm={"added_elements": True, "issues": ["added a hanger"]})["passed"]


def test_verify_without_vision_call(monkeypatch):
    err = render.RenderUnavailable("no verification model available")
    assert not _verify(monkeypatch, text=True, llm_error=err)["passed"]  # logo/text can't be verified -> reject
    c = _verify(monkeypatch, text=False, llm_error=err)                   # plain item: local checks suffice
    assert c["passed"] and c["gemini"]["skipped"]


def test_verify_template_reference_rescues_canonical_relayout(monkeypatch):
    """A catalog-pose re-layout scores low vs the crumpled cutout but high vs the template render; that passes only
    when the vision QA actually ran and approved it."""
    item = {"id": "x", "label": "navy tee", "attributes": {"pattern": "solid"}}
    ref, _ = render.cleanup_render(_garment(0))
    tmpl, _ = render.cleanup_render(_garment(1))
    ref_px = np.asarray(_garment(0).convert("RGB"))[np.asarray(_garment(0))[..., 3] > 127]
    monkeypatch.setattr(render, "clip_similarity", lambda a, b: 0.93 if b is tmpl else 0.60)
    ok = {"same_garment": True, "has_text_or_logo": False, "text_matches": True, "color_matches": True,
          "added_elements": False, "issues": [], "model": "mock"}
    monkeypatch.setattr(render, "_gemini_verify_call", lambda o, r, i: dict(ok))
    assert not render.verify_render(item, ref, ref, ref_px, None)["passed"]  # no template -> clip gate fails
    c = render.verify_render(item, ref, ref, ref_px, None, alt_reference=tmpl)
    assert c["passed"] and c["clip"]["via_template"] and c["clip"]["similarity_template"] == 0.93

    def down(o, r, i):
        raise render.RenderUnavailable("no verification model available")
    monkeypatch.setattr(render, "_gemini_verify_call", down)
    monkeypatch.setattr(render, "RENDER_VERIFY_GEMINI", "off")
    c = render.verify_render(item, ref, ref, ref_px, None, alt_reference=tmpl)
    assert not c["passed"]  # identity unverified -> don't trust a template-only CLIP pass
    monkeypatch.setattr(render, "_gemini_verify_call", lambda o, r, i: dict(ok, same_garment=False, issues=["other"]))
    assert not render.verify_render(item, ref, ref, ref_px, None, alt_reference=tmpl)["passed"]
    monkeypatch.setattr(render, "_gemini_verify_call", lambda o, r, i: dict(ok))
    monkeypatch.setattr(render, "clip_similarity", lambda a, b: 0.60)  # below both gates (cross-garment level)
    assert not render.verify_render(item, ref, ref, ref_px, None, alt_reference=tmpl)["passed"]


def test_classify_errors():
    assert render.classify_error(FakeQuotaError()) == "no_access"

    class E(Exception):
        code = 429
    assert render.classify_error(E("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier "
                                   "limit: 20")) == "daily"
    assert render.classify_error(E("429 ... PerMinute ... retryDelay 20s limit: 10")) == "minute"
    E.code = 404
    assert render.classify_error(E("404 NOT_FOUND model")) == "unavailable"
    E.code = 503
    assert render.classify_error(E("503 UNAVAILABLE overloaded")) == "transient"


# ------------------------------------------------------------------ orchestration: quota fallback / success / retry
def test_quota_error_falls_back_to_cleanup_and_auto_disables(fresh_state, monkeypatch):
    calls = []

    def boom(model, prompt, garment, context, pose_ref=None):
        calls.append(model)
        raise FakeQuotaError(model)
    monkeypatch.setattr(render, "_gemini_image_call", boom)
    it = _a_closet_item("top")
    try:
        rec = render.render_item(it["id"], "auto")
        assert rec["status"] == "done" and rec["method"] in ("template", "cleanup") and rec["embed_source"] == "cutout"
        assert len(calls) == render.RENDER_MAX_MODELS == 2  # tried the primary + ONE other model, then stopped
        att = rec["checks"]["gemini_render"]["attempts"]
        assert [a["error_kind"] for a in att] == ["no_access", "no_access"]
        st = render.gemini_status()
        assert st["disabled"] and "limit: 0" in st["last_error"]
        assert (fresh_state / "render_state.json").exists()  # persisted across restarts
        rec2 = render.render_item(it["id"], "auto")  # no further image calls once disabled
        assert len(calls) == 2 and rec2["method"] in ("template", "cleanup")
        assert rec2["checks"]["gemini_render"]["skipped"]
        render._state = None  # simulate a restart: persisted disable still honoured
        assert not render.gemini_render_available()
    finally:
        render.forget(it["id"])


def test_gemini_render_verified_switches_embeddings_and_retry(fresh_state, monkeypatch):
    from app.vectors import FCLIP_KIND
    it = _a_closet_item("top")
    before = db.get_embedding(it["id"], FCLIP_KIND)
    calls, verdicts = [], [False, True]  # first render rejected by QA, retry accepted

    def fake_image(model, prompt, garment, context, pose_ref=None):
        calls.append(prompt)
        out, _ = render.cleanup_render(Image.open(it["cutout_path"]))  # a 'render' that matches the garment
        return out
    monkeypatch.setattr(render, "_gemini_image_call", fake_image)

    def fake_verify(orig, ren, item):
        ok = verdicts.pop(0)
        return {"same_garment": ok, "has_text_or_logo": False, "text_matches": True, "color_matches": True,
                "added_elements": False, "issues": [] if ok else ["sleeves too short"], "model": "mock-lite"}
    monkeypatch.setattr(render, "_gemini_verify_call", fake_verify)
    try:
        rec = render.render_item(it["id"], "auto")
        assert rec["method"] == "gemini" and rec["model"] == render.RENDER_MODEL
        assert len(calls) == 2 and "sleeves too short" in calls[1]  # retry carried the QA issues
        assert rec["checks"]["verification"]["passed"] and rec["embed_source"] == "clean"
        after = db.get_embedding(it["id"], FCLIP_KIND)
        assert after is not None and not np.allclose(after, before)
        assert render.embedding_image_path(db.get_item(it["id"])) == rec["clean_path"]
        # re-render deterministically -> embeddings go back to the cutout
        rec2 = render.render_item(it["id"], "cleanup")
        assert rec2["method"] == "cleanup" and rec2["embed_source"] == "cutout"
        assert np.allclose(db.get_embedding(it["id"], FCLIP_KIND), before, atol=1e-5)
    finally:
        render.forget(it["id"])


def test_failed_verification_twice_falls_back(fresh_state, monkeypatch):
    it = _a_closet_item("bottom")
    monkeypatch.setattr(render, "_gemini_image_call", lambda *a: Image.new("RGB", (512, 512), (250, 20, 20)))
    monkeypatch.setattr(render, "_gemini_verify_call", lambda *a: {
        "same_garment": False, "has_text_or_logo": False, "text_matches": True, "color_matches": False,
        "added_elements": False, "issues": ["wrong garment"], "model": "mock"})
    try:
        rec = render.render_item(it["id"], "auto")
        assert rec["method"] in ("template", "cleanup") and rec["checks"]["fallback_reason"] == "verification failed"
        assert len(rec["checks"]["gemini_render"]["verifications"]) == 2
    finally:
        render.forget(it["id"])


# ------------------------------------------------------------------ API
def test_render_endpoint_sync_and_background(client, monkeypatch):
    monkeypatch.setattr(render, "RENDER_GEMINI", "off")
    monkeypatch.setattr(render, "RENDER_ENABLED", True)
    it = _a_closet_item("outerwear")
    try:
        r = client.post(f"/api/items/{it['id']}/render", json={"mode": "cleanup", "wait": True})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["render_status"] == "done" and j["clean_method"] == "cleanup"
        assert j["clean_image_url"].startswith("/media/clean/") and j["image_url"] == j["clean_image_url"]
        assert j["original_image_url"] and j["original_image_url"] != j["clean_image_url"]
        assert client.get(j["clean_image_url"]).status_code == 200
        listed = {x["id"]: x for x in client.get("/api/closet/items").json()["items"]}
        assert listed[it["id"]]["clean_image_url"] == j["clean_image_url"]
        # background mode: pending first, then done with a new file
        r = client.post(f"/api/items/{it['id']}/render")
        assert r.status_code == 200 and r.json()["render_status"] in ("pending", "done")
        for _ in range(120):
            j2 = client.get(f"/api/items/{it['id']}").json()
            if j2["render_status"] != "pending":
                break
            time.sleep(0.5)
        assert j2["render_status"] == "done" and j2["clean_image_url"] != j["clean_image_url"]
        assert client.post(f"/api/items/{it['id']}/render", json={"mode": "bogus"}).status_code == 422
        assert client.post("/api/items/it_nope/render").status_code == 404
        assert "render" in client.get("/api/health").json()
    finally:
        render.forget(it["id"])


def _detected_copy_of(it):
    return db.insert_item(status="detected", category=it["category"], attributes=dict(it["attributes"]),
                          label=it.get("label"), crop_path=it["crop_path"], cutout_path=it["cutout_path"],
                          white_path=it["white_path"], source="seed")  # 'seed' so delete keeps the shared files


def test_add_item_works_when_rendering_fails(client, monkeypatch):
    monkeypatch.setattr(render, "SYNC", True)
    monkeypatch.setattr(render, "RENDER_ENABLED", True)

    def explode(*a, **k):
        raise RuntimeError("render exploded")
    monkeypatch.setattr(render, "render_item", explode)
    iid = _detected_copy_of(_a_closet_item("top"))
    try:
        r = client.post("/api/closet/items", json={"item_ids": [iid]})
        assert r.status_code == 200, r.text
        assert db.get_item(iid)["status"] == "closet"
        j = client.get(f"/api/items/{iid}").json()
        assert j["render_status"] == "failed" and j["clean_image_url"] is None
        assert j["image_url"] == j["original_image_url"]  # UI keeps showing the cutout
    finally:
        client.delete(f"/api/closet/items/{iid}")
    # even if scheduling itself blows up, adding still succeeds
    monkeypatch.setattr(render, "_save", explode)
    iid = _detected_copy_of(_a_closet_item("top"))
    try:
        assert client.post("/api/closet/items", json={"item_ids": [iid]}).status_code == 200
        assert db.get_item(iid)["status"] == "closet"
    finally:
        monkeypatch.undo()
        client.delete(f"/api/closet/items/{iid}")


def test_add_item_renders_in_background(client, monkeypatch):
    monkeypatch.setattr(render, "SYNC", True)
    monkeypatch.setattr(render, "RENDER_ENABLED", True)
    monkeypatch.setattr(render, "RENDER_GEMINI", "off")
    iid = _detected_copy_of(_a_closet_item("bottom"))
    try:
        r = client.post("/api/closet/items", json={"item_ids": [iid]})
        assert r.status_code == 200
        j = client.get(f"/api/items/{iid}").json()
        assert j["render_status"] == "done" and j["clean_method"] in ("template", "cleanup") and j["clean_image_url"]
        clean_path = render.get_render(iid)["clean_path"]
    finally:
        client.delete(f"/api/closet/items/{iid}")
    assert render.get_render(iid) is None
    from pathlib import Path
    assert not Path(clean_path).exists()  # delete removes the clean image too


def test_image_key_change_resets_auto_disable(fresh_state, monkeypatch):
    """A separate GEMINI_IMAGE_API_KEY (e.g. billing-enabled) re-enables image generation automatically; only a hash
    of the key is persisted."""
    import json
    from app import gemini
    key = {"v": ("free-key-123", "main")}
    monkeypatch.setattr(gemini, "image_api_key", lambda: key["v"])
    assert render.gemini_render_available()
    render._disable("image models not available on this API key (free tier limit: 0)", "429 limit: 0",
                    time.time() + 3600)
    render._model_blocked["gemini-3.1-flash-image"] = time.time() + 3600
    assert not render.gemini_render_available()
    raw = (fresh_state / "render_state.json").read_text()
    assert "free-key-123" not in raw and json.loads(raw)["key_fp"] == gemini.key_fingerprint("free-key-123")
    render._state = None  # restart: still disabled for the same key
    assert not render.gemini_render_available()
    key["v"] = ("billing-key-456", "image")  # user adds GEMINI_IMAGE_API_KEY to backend/.env
    st = render.gemini_status()
    assert not st["disabled"] and st["key_source"] == "image" and st["reason"] is None
    assert render.gemini_render_available() and not render._model_blocked
    raw = (fresh_state / "render_state.json").read_text()
    assert "billing-key-456" not in raw and json.loads(raw)["previous_key"]["reason"]


def test_legacy_state_without_fingerprint_is_not_reset(fresh_state, monkeypatch):
    import json
    from app import gemini
    monkeypatch.setattr(gemini, "image_api_key", lambda: ("free-key-123", "main"))
    (fresh_state / "render_state.json").write_text(json.dumps(
        {"disabled_until": time.time() + 3600, "reason": "image models not available on this API key"}))
    render._state = None
    assert not render.gemini_render_available()  # adopts the current key; no wasted 429s
    assert json.loads((fresh_state / "render_state.json").read_text())["key_fp"]


def test_image_api_key_prefers_separate_key(monkeypatch, tmp_path):
    from app import gemini
    monkeypatch.delenv("FITCHECK_GEMINI_OFF", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "main-key")
    monkeypatch.delenv("GEMINI_IMAGE_API_KEY", raising=False)
    monkeypatch.setattr(gemini, "_key_from_dotenv", lambda names=None: None)
    assert gemini.image_api_key() == ("main-key", "main")
    monkeypatch.setenv("GEMINI_IMAGE_API_KEY", "img-key")
    assert gemini.image_api_key() == ("img-key", "image")
    monkeypatch.setenv("FITCHECK_GEMINI_OFF", "1")
    assert gemini.image_api_key() == (None, "none")


def test_gemini_gets_pose_reference_and_v2_prompt(fresh_state, monkeypatch):
    it = _a_closet_item("top")
    seen = {}

    def fake_image(model, prompt, garment, context, pose_ref=None):
        seen["prompt"], seen["pose_ref"] = prompt, pose_ref
        raise FakeQuotaError(model)
    monkeypatch.setattr(render, "_gemini_image_call", fake_image)
    try:
        rec = render.render_item(it["id"], "auto")
        assert "WHAT WE KNOW ABOUT THIS GARMENT" in seen["prompt"] and "PURE WHITE" in seen["prompt"]
        if rec["checks"].get("template", {}).get("template"):
            assert seen["pose_ref"] is not None and "SCHEMATIC" in seen["prompt"]
        else:
            assert seen["pose_ref"] is None and "SCHEMATIC" not in seen["prompt"]
    finally:
        render.forget(it["id"])
