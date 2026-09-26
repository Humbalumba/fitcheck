"""Canonical TEMPLATE renderer (app/render_template.py + app/garment_templates.py) and the attribute-driven
Gemini prompt (app/render_prompt.py). No Gemini calls: garment details are passed in explicitly."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import db, render
from app import garment_templates as gt
from app import render_template as rt
from app.render_prompt import build_render_prompt


def _item(label, subcategory, category="top", description="", pattern="solid", **extra):
    a = {"subcategory": subcategory, "category": category, "description": description or label, "pattern": pattern}
    a.update(extra)
    return {"id": "it_test", "label": label, "category": category, "attributes": a}


TEE = {"garment_type": "tshirt", "view": "front", "sleeve_length": "short", "neckline": "crew", "closure": "none",
       "has_hood": False, "has_front_pockets": False, "ribbed_hem_cuffs": False, "fit": "regular",
       "length": "regular", "graphics": []}


def _details(**kw):
    d = dict(TEE)
    d.update(kw)
    return d


# ------------------------------------------------------------------ template selection
@pytest.mark.parametrize("item,details,expected", [
    (_item("black graphic t-shirt", "t-shirt", pattern="graphic"), _details(view="back"), ("tshirt", "back")),
    (_item("brown hooded zip sweater", "hoodie"), _details(garment_type="zip_hoodie", has_hood=True,
                                                          closure="full_zip", has_drawstrings=False),
     ("zip_hoodie_nostrings", "front")),
    (_item("pink hoodie", "hoodie"), _details(garment_type="hoodie", has_hood=True), ("hoodie", "front")),
    (_item("grey sweatshirt", "sweater"), _details(garment_type="sweatshirt", has_hood=True, closure="full_zip"),
     ("zip_hoodie", "front")),
    (_item("white polo shirt", "polo"), _details(garment_type="polo", neckline="polo_collar"), ("polo", "front")),
    (_item("navy v-neck sweater", "sweater"), _details(garment_type="sweater", neckline="v_neck",
                                                        sleeve_length="long"), ("sweater_vneck", "front")),
    (_item("light blue jeans", "jeans", "bottom"), _details(garment_type="jeans", view="back"), ("jeans", "back")),
    # heuristics when no Gemini details exist
    (_item("red t-shirt", "t-shirt", description="A classic red crewneck t-shirt."), None, ("tshirt", "front")),
    (_item("khaki chinos", "chinos", "bottom"), None, ("trousers", "front")),
    (_item("black zip-up hoodie", "hoodie"), None, ("zip_hoodie", "front")),
])
def test_select_template(item, details, expected):
    sel = rt.select_template(item, details)
    assert sel is not None and sel[:2] == expected


@pytest.mark.parametrize("item,details", [
    (_item("white sneakers", "sneakers", "shoes"), None),
    (_item("leather belt", "belt", "accessory"), None),
    (_item("red plaid flannel shirt", "shirt", pattern="plaid"), None),
    (_item("striped tee", "t-shirt", pattern="striped"), _details()),
    (_item("grey and black raglan t-shirt", "t-shirt"), _details()),
    (_item("pink slogan hoodie", "hoodie", pattern="graphic print"), None),  # logo but no locations -> cleanup
    (_item("sage satin maxi dress", "maxi dress", "dress"), None),
    (_item("mystery item", "thing", "top"), None),
    (_item("CK Jeans Men's Omero High Top Trainers", "sneakers", "shoes"), None),
    (_item("destroyed denim shorts", "shorts", "bottom"), None),
])
def test_unsupported_or_risky_items_fall_back(item, details):
    assert rt.select_template(item, details) is None


def test_all_templates_build_and_have_anchors():
    for name in gt.TEMPLATE_NAMES:
        for view in ("front", "back"):
            t = gt.get_template(name, view)
            assert t.mask.shape == (gt.S, gt.S) and 0.12 < float((t.mask > 0.5).mean()) < 0.7, name
            m = t.mask > 0.5
            # left/right symmetric silhouette (canonical pose)
            assert (m ^ m[:, ::-1]).mean() < 0.01, name
            # typical fabric pixel renders at the sampled colour (shade normalised to ~1)
            assert 0.93 < float(np.median(t.shade[m & (t.inner < 0.5)])) < 1.07, name
            assert t.anchors, name


# ------------------------------------------------------------------ recolour
@pytest.mark.parametrize("lab", [(50.0, 60.0, 35.0), (15.0, 0.5, 0.0), (94.0, 0.0, 1.0), (75.0, -3.0, -16.0)])
def test_recolour_matches_requested_colour(lab):
    t = gt.get_template("tshirt", "front")
    img = rt.composite(t, np.array(lab), "jersey", [])
    m = (t.mask > 0.99) & (t.inner < 0.01) & (t.rib < 0.01)
    got = np.median(render.rgb_to_lab(img[m]), axis=0)
    assert render.color_distance(got, lab) < 5.0, (got, lab)
    assert np.array_equal(img, rt.composite(t, np.array(lab), "jersey", []))  # deterministic


def test_denim_looks_like_denim():
    t = gt.get_template("jeans", "front")
    img = rt.composite(t, np.array([55.0, -2.0, -25.0]), "denim", [])
    m = (t.mask > 0.99)
    L = render.rgb_to_lab(img[m])[:, 0]
    assert L.std() > 2.0  # twill / wash texture, not a flat fill
    lab = np.median(render.rgb_to_lab(img[m]), axis=0)
    assert lab[2] < -12  # still blue


def test_sample_fabric_ignores_logo_and_shadows():
    rng = np.random.default_rng(0)
    arr = np.zeros((300, 240, 3), np.uint8)
    arr[:] = (200, 40, 40)
    arr = np.clip(arr + rng.normal(0, 4, arr.shape), 0, 255).astype(np.uint8)
    arr[40:80, 60:180] = (245, 245, 245)       # white logo
    arr[250:300] = (70, 15, 15)                # deep shadow fold
    fg = np.ones(arr.shape[:2], bool)
    excl = np.zeros_like(fg)
    excl[30:90, 50:190] = True
    lab, _ = rt.sample_fabric(arr, fg, excl)
    assert render.color_distance(lab, render.rgb_to_lab(np.array([200, 40, 40]))) < 4


# ------------------------------------------------------------------ logo transplant
def _garment_photo(angle=20.0):
    """A red garment with a white horizontal bar 'logo' rotated `angle` degrees clockwise, on a grey floor."""
    im = Image.new("RGB", (400, 400), (90, 90, 90))
    d = ImageDraw.Draw(im)
    d.rectangle([60, 40, 340, 380], fill=(190, 30, 40))
    logo = Image.new("RGBA", (140, 40), (0, 0, 0, 0))
    ImageDraw.Draw(logo).rectangle([0, 0, 139, 39], fill=(250, 250, 250, 255))
    ImageDraw.Draw(logo).rectangle([10, 12, 40, 28], fill=(20, 20, 20, 255))  # a dark mark inside
    logo = logo.rotate(-angle, expand=True, resample=Image.BICUBIC)
    im.paste(logo, (200 - logo.width // 2, 150 - logo.height // 2), logo)
    fg = np.zeros((400, 400), bool)
    fg[40:381, 60:341] = True
    box = (200 - logo.width // 2 - 4, 150 - logo.height // 2 - 4, 200 + logo.width // 2 + 4, 150 + logo.height // 2 + 4)
    return im, fg, box


def test_logo_deskewed_upright_and_placed_deterministically():
    im, fg, box = _garment_photo(20.0)
    arr = np.asarray(im)
    lab = render.rgb_to_lab(arr).astype(np.float32)
    fab, _ = rt.sample_fabric(arr, fg)
    # Gemini's rough angle is off (and could be wrong) -- the stroke analysis fixes the fine angle
    gr = rt.extract_graphic(lab, fg, box, fab, rotate_cw=-5)
    assert gr is not None and not gr["tonal"]
    assert abs(gr["angle_cw"] - (-20.0)) <= 2.5, gr["angle_cw"]
    h, w = gr["alpha"].shape
    assert w > 2.5 * h  # upright: the bar is horizontal again
    t = gt.get_template("tshirt", "front")
    g = {"position": "chest_center", "kind": "logo", "text": ""}
    ys, xs = np.nonzero(fg)
    p1 = rt.place_graphic(t, g, gr, box, fg, (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    p2 = rt.place_graphic(t, g, rt.extract_graphic(lab, fg, box, fab, rotate_cw=-5), box, fg,
                          (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    assert np.array_equal(p1["alpha"], p2["alpha"]) and p1["box"] == p2["box"]
    cx, cy, max_w = t.anchors["chest_center"]
    x0, y0, x1, y1 = p1["box"]
    assert abs((x0 + x1) / 2 - cx) < 3 and abs((y0 + y1) / 2 - cy) < 3
    assert (x1 - x0) <= max_w + 1
    # the logo keeps its real colours on the template (white bar with a dark mark)
    img = rt.composite(t, fab, "jersey", [p1])
    core = img[p1["alpha"] > 0.9]
    assert (core.mean(axis=1) > 200).mean() > 0.5 and (core.mean(axis=1) < 90).any()


def test_sideways_text_uses_gemini_quarter_turns():
    im, fg, box = _garment_photo(90.0)
    arr = np.asarray(im)
    lab = render.rgb_to_lab(arr).astype(np.float32)
    fab, _ = rt.sample_fabric(arr, fg)
    gr = rt.extract_graphic(lab, fg, box, fab, rotate_cw=-90)
    h, w = gr["alpha"].shape
    assert w > 2.5 * h and abs(abs(gr["angle_cw"]) - 90) <= 2.5


def test_stroke_orientation_sign():
    img = np.zeros((200, 200), np.float32)
    img[80:120, 40:160] = 50
    for deg in (0, 20, -30, 40):
        r = rt._rotate_float(img, deg)
        ang, _ = rt.stroke_orientation(r, np.ones_like(r))
        assert abs(ang + deg) <= 2.0, (deg, ang)


# ------------------------------------------------------------------ end to end + fallback
def test_render_template_end_to_end_and_fallbacks():
    im, fg, box = _garment_photo(0.0)
    W, H = im.size
    b = [int(box[1] / H * 1000), int(box[0] / W * 1000), int(box[3] / H * 1000), int(box[2] / W * 1000)]
    details = _details(graphics=[{"box_2d": b, "kind": "logo", "text": "", "position": "chest_center",
                                  "rotate_cw_deg": 0, "description": "white bar"}])
    item = _item("red graphic t-shirt", "t-shirt", pattern="graphic")
    out, info = rt.render_template(item, im, fg, im, details=details, frame="crop")
    assert out is not None, info
    assert out.size == (render.RENDER_SIZE, render.RENDER_SIZE) and info["template"] == "tshirt"
    a = np.asarray(out)
    for y, x in [(0, 0), (0, -1), (-1, 0), (-1, -1)]:
        assert tuple(a[y, x]) == (255, 255, 255)
    assert info["color_check"]["passed"] and info["graphics"][0]["position"] == "chest_center"
    out2, _ = rt.render_template(item, im, fg, im, details=details, frame="crop")
    assert np.array_equal(np.asarray(out), np.asarray(out2))  # deterministic
    # unsupported type -> None (caller uses the cleanup)
    none, info = rt.render_template(_item("white sneakers", "sneakers", "shoes"), im, fg, im, details=None)
    assert none is None and info["skipped"]
    # colour sanity check: photo colour far from what the template can show -> None
    det_bad = _details()
    item2 = _item("red t-shirt", "t-shirt")
    fake = np.asarray(im).copy()
    fake[fg] = (190, 30, 40)
    orig = rt.normalize_fabric

    def broken(lab, item):
        return np.array([60.0, -40.0, 40.0]), {"exposure": "broken"}
    rt.normalize_fabric = broken
    try:
        none, info = rt.render_template(item2, Image.fromarray(fake), fg, None, details=det_bad)
        assert none is None and "colour check" in info["skipped"]
    finally:
        rt.normalize_fabric = orig


def test_white_garment_snaps_to_clean_white():
    lab, info = rt.normalize_fabric(np.array([79.7, 1.8, -6.7]), _item("white polo shirt", "polo"))
    assert lab[0] >= 92 and abs(lab[1]) < 2 and abs(lab[2]) < 3 and info["exposure"] == "white_snap"
    lab, info = rt.normalize_fabric(np.array([90.6, -0.4, 2.8]), _item("light blue jeans", "jeans", "bottom"))
    assert lab[0] <= 81 and lab[2] < -5  # over-exposed, warm-cast light-wash denim reads light blue
    lab, _ = rt.normalize_fabric(np.array([48.0, 60.0, 35.0]), _item("red t-shirt", "t-shirt"))
    assert abs(lab[1] - 60) < 0.01 and abs(lab[2] - 35) < 0.01  # saturated colours are left alone


def test_render_item_priority_template_then_cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER_STATE_PATH", str(tmp_path / "render_state.json"))
    monkeypatch.setattr(render, "_state", None)
    monkeypatch.setattr(render, "RENDER_GEMINI", "off")
    tee = next((it for it in db.list_items(status="closet") if rt.select_template(it, None)), None)
    if tee is None:
        pytest.skip("no template-able item in the test closet")
    tname = rt.select_template(tee, None)[0]
    try:
        rec = render.render_item(tee["id"], "auto")
        assert rec["status"] == "done"
        assert rec["method"] in ("template", "cleanup")
        assert rec["checks"]["template"]["template"] == tname
        if rec["method"] == "template":
            assert rec["embed_source"] == "cutout"  # display only: embeddings keep using the original cutout
            assert render.embedding_image_path(db.get_item(tee["id"])) != rec["clean_path"]
        rec2 = render.render_item(tee["id"], "cleanup")
        assert rec2["method"] == "cleanup" and "template" not in rec2["checks"]
        monkeypatch.setattr(render, "RENDER_TEMPLATE", False)
        assert render.render_item(tee["id"], "auto")["method"] == "cleanup"
    finally:
        render.forget(tee["id"])


# ------------------------------------------------------------------ Gemini prompt (option 1)
def test_attribute_driven_canonical_prompt():
    zip_item = _item("brown hooded zip sweater", "hoodie", description="Brown full-zip hoodie",
                     primary_color="brown")
    p = build_render_prompt(zip_item, _details(garment_type="zip_hoodie", closure="full_zip", has_hood=True,
                                               lining_color="white", hardware_color="silver"))
    assert "FULLY ZIPPED" in p and "hood" in p and "BOTH sleeves" in p
    assert "hood lining colour: white" in p and "main colour: brown" in p and "PURE WHITE" in p
    assert "NO logos or text" in p
    tee = _item("black graphic t-shirt", "t-shirt", pattern="graphic")
    p = build_render_prompt(tee, _details(view="back", graphics=[{
        "box_2d": [1, 2, 3, 4], "kind": "graphic", "text": "", "position": "back_center", "rotate_cw_deg": -30,
        "description": "white circular cross emblem"}]))
    assert "BACK view" in p and "FRONT view" not in p
    assert "white circular cross emblem" in p and "back center" in p and "collar up" in p and "do NOT redraw" in p
    jeans = build_render_prompt(_item("light blue jeans", "jeans", "bottom"), None)
    assert "BOTH legs straight" in jeans and "waistband" in jeans
    shoes = build_render_prompt(_item("white sneakers", "sneakers", "shoes"), None)
    assert "3/4 side view" in shoes


def test_prompt_follows_construction_details():
    hood = _item("pink hoodie", "hoodie")
    p = build_render_prompt(hood, _details(garment_type="hoodie", has_hood=True, has_drawstrings=False))
    assert "NO drawstrings" in p and "drawstrings hanging evenly" not in p
    jeans_back = build_render_prompt(_item("light blue jeans", "jeans", "bottom"),
                                     _details(garment_type="jeans", view="back", has_front_pockets=False))
    assert "BACK view of jeans" in jeans_back and "fly facing" not in jeans_back
    assert "no front pockets" not in jeans_back
    p = build_render_prompt(_item("navy sweater", "sweater"), _details(garment_type="sweater", neckline="v_neck"))
    assert "V neckline" in p
    p = build_render_prompt(_item("black tee", "t-shirt"), _details(), pose_ref=True)
    assert "crew neckline" in p and "SCHEMATIC" in p and "Image 3" in p


def test_render_closet_script_dry_run(capsys, monkeypatch):
    import sys
    from scripts import render_closet
    monkeypatch.setattr(sys, "argv", ["render_closet.py", "--dry-run", "--mode", "template"])
    before = {r["item_id"]: r.get("clean_path") for r in render._all_records().values()} \
        if hasattr(render, "_all_records") else {}
    assert render_closet.main() == 0
    out = capsys.readouterr().out
    assert "plan:" in out and "Gemini image key" in out
    after = {r["item_id"]: r.get("clean_path") for r in render._all_records().values()}
    assert before == after  # dry run changes nothing
