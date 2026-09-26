"""Procedural, brand-catalogue style garment templates (front / back views) for the template renderer.

Every template is drawn from hand-designed, left/right-symmetric Bezier outlines in a 1000 x 1000 unit space
(no downloaded mock-ups), rasterised with 2x supersampling, and comes with:
  mask      garment coverage (0..1)          inner   inside surfaces (neck inside, hood lining, pocket mouths)
  rib       ribbed knit areas                 shade   multiply map (seams, folds, edge occlusion, ribbing, light)
  light     highlight/sheen map (screen)      stitches / hardware overlays (buttons, zip, rivets, aglets)
  anchors   logo positions -> (cx, cy, max_w) in px
All pure numpy/PIL/scipy, deterministic, cached per (name, view).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

S = 1024          # template raster size (px)
SS = 2            # supersampling for antialiased shapes
K = S / 1000.0    # px per unit


# ------------------------------------------------------------------ geometry helpers
def _cubic(p0, p1, p2, p3, n=28):
    t = np.linspace(0, 1, n)[1:, None]
    p0, p1, p2, p3 = map(np.asarray, (p0, p1, p2, p3))
    pts = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3
    return [tuple(p) for p in pts]


class Pen:
    def __init__(self, x, y):
        self.pts = [(float(x), float(y))]

    def L(self, x, y):
        self.pts.append((float(x), float(y)))
        return self

    def C(self, x1, y1, x2, y2, x, y, n=28):
        self.pts += _cubic(self.pts[-1], (x1, y1), (x2, y2), (x, y), n)
        return self

    def Q(self, x1, y1, x, y, n=20):
        p0 = self.pts[-1]
        c1 = (p0[0] + 2 / 3 * (x1 - p0[0]), p0[1] + 2 / 3 * (y1 - p0[1]))
        c2 = (x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y))
        return self.C(*c1, *c2, x, y, n)


def mirror(pts):
    return [(1000 - x, y) for x, y in reversed(pts)]


def mirror_same(pts):
    return [(1000 - x, y) for x, y in pts]


def sym(right):
    """Right half outline from top-centre, clockwise, to bottom-centre -> closed symmetric polygon."""
    return list(right) + mirror(right)


def _canvas():
    return Image.new("L", (S * SS, S * SS), 0)


def _px(pts):
    return [(x * K * SS, y * K * SS) for x, y in pts]


def _down(img):
    return np.asarray(img.resize((S, S), Image.BOX), dtype=np.float32) / 255.0


def fill(*polys):
    im = _canvas()
    d = ImageDraw.Draw(im)
    for p in polys:
        d.polygon(_px(p), fill=255)
    return _down(im)


def stroke(pts, width, closed=False):
    im = _canvas()
    d = ImageDraw.Draw(im)
    p = _px(pts + ([pts[0]] if closed else []))
    w = max(1, int(round(width * K * SS)))
    d.line(p, fill=255, width=w, joint="curve")
    r = w / 2
    for x, y in (p[0], p[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return _down(im)


def dashed(pts, width, dash=9.0, gap=6.0):
    """Stitch line: dashes along a polyline (units)."""
    im = _canvas()
    d = ImageDraw.Draw(im)
    w = max(1, int(round(width * K * SS)))
    seg_on, pos = True, 0.0
    run = dash
    cur = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a, b)
        t = 0.0
        while L - t > 1e-6:
            step = min(run - pos, L - t)
            t += step
            pos += step
            p = (a[0] + (b[0] - a[0]) * t / L, a[1] + (b[1] - a[1]) * t / L)
            if seg_on:
                cur.append(p)
            if pos >= run - 1e-6:
                if seg_on and len(cur) > 1:
                    d.line(_px(cur), fill=255, width=w)
                seg_on = not seg_on
                pos, run = 0.0, (dash if seg_on else gap)
                cur = [p]
    if seg_on and len(cur) > 1:
        d.line(_px(cur), fill=255, width=w)
    return _down(im)


def ellipse(cx, cy, rx, ry):
    im = _canvas()
    ImageDraw.Draw(im).ellipse(_px([(cx - rx, cy - ry), (cx + rx, cy + ry)]), fill=255)
    return _down(im)


def offset_curve(pts, dx=0.0, dy=0.0, towards=None, k=0.0):
    """Shift a curve (optionally also pull it towards a point by fraction k)."""
    out = []
    for x, y in pts:
        if towards is not None:
            x, y = x + (towards[0] - x) * k, y + (towards[1] - y) * k
        out.append((x + dx, y + dy))
    return out


def blur(a, sigma_units):
    return ndimage.gaussian_filter(a, sigma_units * K)


# ------------------------------------------------------------------ template object
@dataclass
class Template:
    name: str
    view: str
    mask: np.ndarray
    inner: np.ndarray
    rib: np.ndarray
    shade: np.ndarray
    light: np.ndarray
    stitches: list = field(default_factory=list)   # (alpha, kind) kind: 'tonal' | 'contrast'
    hardware: list = field(default_factory=list)   # (alpha, kind, local_shade) kind: 'metal' | 'button' | 'aglet' | 'tonal' | 'string'
    anchors: dict = field(default_factory=dict)    # position -> (cx, cy, max_w) px
    denim: bool = False
    fade: np.ndarray | None = None                 # denim wash map (0..1 lighter)

    @property
    def bbox(self):
        ys, xs = np.nonzero(self.mask > 0.5)
        return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class _B:
    """Template builder: collects parts and produces shade/light maps."""

    def __init__(self, name, view):
        self.name, self.view = name, view
        self.parts: list[np.ndarray] = []
        self.inner = np.zeros((S, S), np.float32)
        self.rib = np.zeros((S, S), np.float32)
        self.rib_dir = np.zeros((S, S), np.float32)  # 0 = vertical ribs (horizontal band), 1 = horizontal ribs
        self.seams: list[np.ndarray] = []
        self.dark: list[tuple] = []
        self.hi: list[tuple] = []
        self.stitches: list = []
        self.hardware: list = []
        self.anchors: dict = {}
        self.denim = False
        self.fade = None
        self.cut = np.zeros((S, S), np.float32)  # holes (e.g. under the hood opening we keep fabric) unused

    def part(self, poly_or_mask):
        m = fill(poly_or_mask) if isinstance(poly_or_mask, list) else poly_or_mask
        self.parts.append(m)
        return m

    def seam(self, pts, closed=False, w=2.2):
        self.seams.append(stroke(pts, w, closed))

    def fold(self, pts, width=14, strength=0.10, hi=0.0, hi_offset=(8, 0)):
        self.dark.append((pts, width, strength))
        if hi:
            self.hi.append((offset_curve(pts, *hi_offset), width * 1.2, hi))

    def sheen(self, pts, width=40, strength=0.2):
        self.hi.append((pts, width, strength))

    def stitch(self, pts, kind="tonal", w=1.6, dash=8.0, gap=5.0):
        self.stitches.append((dashed(pts, w, dash, gap), kind))

    def ribband(self, poly, horizontal_ribs=False):
        m = fill(poly)
        self.rib = np.maximum(self.rib, m)
        if horizontal_ribs:
            self.rib_dir = np.maximum(self.rib_dir, m)
        return m

    def build(self) -> Template:
        mask = np.zeros((S, S), np.float32)
        for p in self.parts:
            mask = np.maximum(mask, p)
        hard = mask > 0.5
        shade = np.ones((S, S), np.float32)
        # soft edge occlusion (garment curves away from the camera at the silhouette)
        d = ndimage.distance_transform_edt(hard).astype(np.float32)
        shade *= 1 - 0.20 * np.exp(-d / (16 * K))
        # part boundaries read as seams: a small occlusion on both sides of every part edge
        for p in self.parts:
            pd = ndimage.distance_transform_edt(p > 0.5).astype(np.float32)
            pdo = ndimage.distance_transform_edt(p <= 0.5).astype(np.float32)
            edge = np.exp(-(pd + pdo) / (3.5 * K)) * hard   # one of pd/pdo is 0 -> distance to the part edge
            shade *= 1 - 0.10 * edge
        for s in self.seams:
            shade *= 1 - 0.28 * blur(s, 0.8)
        for pts, w, st in self.dark:
            shade *= 1 - st * np.clip(blur(stroke(pts, w * 0.5), w * 0.45) * 1.6, 0, 1)
        # ribbing
        yy, xx = np.mgrid[:S, :S].astype(np.float32)
        period = 7.0 * K
        rib_v = 0.5 + 0.5 * np.sin(2 * np.pi * xx / period)
        rib_h = 0.5 + 0.5 * np.sin(2 * np.pi * yy / period)
        rib_pat = rib_v * (1 - self.rib_dir) + rib_h * self.rib_dir
        shade *= 1 - 0.09 * self.rib * rib_pat
        # inside surfaces are in shadow, darker towards the top
        inner_grad = 0.66 + 0.20 * np.clip((yy / S - 0.05) * 2.2, 0, 1)
        shade = shade * (1 - self.inner) + shade * inner_grad * self.inner
        # studio light: from the top-left
        shade *= 1.035 - 0.06 * (yy / S) - 0.02 * (xx / S)
        # normalise: the typical fabric pixel renders at exactly the sampled colour (shade 1.0)
        med = float(np.median(shade[hard & (self.inner < 0.5)])) if hard.any() else 1.0
        shade = shade / max(med, 1e-3)
        light = np.zeros((S, S), np.float32)
        for pts, w, st in self.hi:
            light += st * np.clip(blur(stroke(pts, w * 0.5), w * 0.5) * 1.5, 0, 1)
        light = np.clip(light, 0, 1) * mask
        return Template(self.name, self.view, mask, np.clip(self.inner, 0, 1), np.clip(self.rib, 0, 1),
                        shade.astype(np.float32), light.astype(np.float32), self.stitches, self.hardware,
                        self.anchors, self.denim, self.fade)


def _anchor(cx, cy, w):
    return (cx * K, cy * K, w * K)


# ------------------------------------------------------------------ tops
def _tee_body(b: _B, view: str, long_sleeves=False, sweater=False, polo=False, y_hem=820, vneck=False):
    """Crew-neck tee / long-sleeve / sweater / polo body. Returns neckline pts."""
    # right half outline pieces
    neck_side = (588, 128)
    shoulder = (722, 152)
    armpit = (724, 300)
    front_neck = ((500, 292) if vneck else (500, 186)) if view == "front" else (500, 146)
    # body (without sleeves)
    body = Pen(500, 140 if view == "front" else 146)
    if view == "front" and vneck:
        body = Pen(*front_neck).C(530, 240, 570, 170, *neck_side)
    elif view == "front":
        body = Pen(*front_neck).C(548, 186, 580, 160, *neck_side)
    else:
        body = Pen(*front_neck).C(545, 146, 572, 138, *neck_side)
    body.L(*shoulder).C(712, 200, 716, 260, *armpit).C(716, 420, 716, 600, 728, y_hem - 10)
    body.C(640, y_hem - 2, 560, y_hem + 4, 500, y_hem + 4)
    body_poly = sym(body.pts)
    b.part(body_poly)
    # sleeves
    if not long_sleeves:
        sl = Pen(*shoulder).C(790, 200, 845, 250, 884, 296).L(818, 364).C(790, 334, 760, 312, *armpit)
        sl.C(716, 260, 712, 200, *shoulder)
        hem_band = [(884, 296), (818, 364)]
        cuff_poly = None
    else:
        sl = Pen(*shoulder).C(790, 172, 836, 226, 850, 320).C(862, 460, 870, 600, 874, 700)
        sl.L(878, 772).L(802, 778).L(798, 706).C(784, 560, 768, 420, 748, 330).C(740, 318, 732, 306, *armpit)
        sl.C(716, 260, 712, 200, *shoulder)
        cuff_poly = [(874, 704), (879, 776), (801, 781), (797, 709)]
        hem_band = None
    sleeve_r = sl.pts
    b.part(sleeve_r)
    b.part(mirror_same(sleeve_r))
    b.seam(Pen(*shoulder).C(712, 200, 716, 260, *armpit).pts)
    b.seam(mirror_same(Pen(*shoulder).C(712, 200, 716, 260, *armpit).pts))
    b.seam([neck_side, shoulder])
    b.seam(mirror_same([neck_side, shoulder]))
    # neckline rib band
    if view == "front" and vneck:
        nl = Pen(1000 - neck_side[0], neck_side[1]).C(430, 170, 470, 240, 500, 292).C(530, 240, 570, 170, *neck_side).pts
        inner_v = Pen(1000 - neck_side[0] + 16, neck_side[1] + 4).C(446, 174, 482, 232, 500, 262)
        inner_v.C(518, 232, 554, 174, neck_side[0] - 16, neck_side[1] + 4)
        back_nl = Pen(1000 - neck_side[0], neck_side[1]).C(440, 142, 470, 146, 500, 146).C(530, 146, 560, 142, *neck_side).pts
        b.ribband(nl + list(reversed(inner_v.pts)))
        insd = np.clip(fill(back_nl + list(reversed(nl))) - fill(nl + list(reversed(inner_v.pts))), 0, 1)
        b.inner = np.maximum(b.inner, insd)
        b.part(back_nl + list(reversed(nl)))
        b.ribband(back_nl + list(reversed(offset_curve(back_nl, dy=12, towards=(500, 146), k=0.02))))
        b.seam([(500, 262), (500, 292)], w=1.4)
        b.stitch(offset_curve(inner_v.pts, dy=5), "tonal")
    elif view == "front":
        nl = Pen(1000 - neck_side[0], neck_side[1]).C(420, 160, 452, 186, 500, 186).C(548, 186, 580, 160, *neck_side).pts
        inner_nl = Pen(1000 - neck_side[0] + 14, neck_side[1] + 6).C(425, 178, 455, 207, 500, 207)
        inner_nl.C(545, 207, 575, 178, neck_side[0] - 14, neck_side[1] + 6)
        back_nl = Pen(1000 - neck_side[0], neck_side[1]).C(440, 142, 470, 146, 500, 146).C(530, 146, 560, 142, *neck_side).pts
        if not polo:
            b.ribband(nl + list(reversed(inner_nl.pts)))
            # inside of the back neck + back rib + a small neck label
            b.inner = np.maximum(b.inner, np.clip(fill(back_nl + list(reversed(nl))) - fill(nl + list(reversed(inner_nl.pts))), 0, 1))
            b.part(back_nl + list(reversed(nl)))
            b.ribband(back_nl + list(reversed(offset_curve(back_nl, dy=12, towards=(500, 146), k=0.02))))
        b.stitch(offset_curve(inner_nl.pts, dy=5), "tonal")
    else:
        nl = Pen(1000 - neck_side[0], neck_side[1]).C(428, 138, 455, 146, 500, 146).C(545, 146, 572, 138, *neck_side).pts
        inner_nl = offset_curve(nl, dy=18, towards=(500, 150), k=0.04)
        if not polo:
            b.ribband(nl + list(reversed(inner_nl)))
        b.stitch(offset_curve(inner_nl, dy=4), "tonal")
    # hems / cuffs
    hem_line = Pen(272, y_hem - 10).C(360, y_hem + 4, 440, y_hem + 4, 500, y_hem + 4).C(560, y_hem + 4, 640, y_hem + 4, 728, y_hem - 10).pts
    if sweater:
        band = hem_line + list(reversed(offset_curve(hem_line, dy=-62)))
        b.ribband(band)
        b.seam(offset_curve(hem_line, dy=-62))
    else:
        b.stitch(offset_curve(hem_line, dy=-18), "tonal")
    if hem_band:
        a, c = hem_band
        b.stitch([(a[0] - 12, a[1] + 12), (c[0] - 12, c[1] + 12)], "tonal")
        b.stitch(mirror_same([(a[0] - 12, a[1] + 12), (c[0] - 12, c[1] + 12)]), "tonal")
        if polo:  # ribbed sleeve bands
            band = [a, c, (c[0] - 16, c[1] - 16), (a[0] - 16, a[1] - 16)]
            b.ribband(band, horizontal_ribs=False)
            b.ribband(mirror_same(band))
    if cuff_poly:
        b.ribband(cuff_poly)
        b.ribband(mirror_same(cuff_poly))
        b.seam([cuff_poly[0], cuff_poly[3]])
        b.seam(mirror_same([cuff_poly[0], cuff_poly[3]]))
    # drape / folds
    b.fold([(640, 175), (628, 330), (636, 480)], 18, 0.06, hi=0.10, hi_offset=(-14, 0))
    b.fold(mirror_same([(640, 175), (628, 330), (636, 480)]), 18, 0.06)
    b.fold([(724, 312), (680, 360), (660, 420)], 16, 0.07)
    b.fold(mirror_same([(724, 312), (680, 360), (660, 420)]), 16, 0.07)
    b.fold([(560, 600), (575, 700), (568, 800)], 22, 0.045, hi=0.06, hi_offset=(-16, 0))
    b.fold(mirror_same([(540, 650), (552, 800)]), 20, 0.04)
    b.sheen([(420, 250), (440, 420)], 90, 0.10)
    b.sheen([(800, 210), (850, 280)] if not long_sleeves else [(810, 230), (845, 600)], 40, 0.10)
    b.sheen(mirror_same([(800, 210), (850, 280)] if not long_sleeves else [(810, 230), (845, 600)]), 40, 0.07)
    if long_sleeves:
        b.fold([(830, 420), (800, 470)], 12, 0.08)
        b.fold([(846, 560), (812, 600)], 12, 0.08)
        b.fold(mirror_same([(830, 440), (800, 490)]), 12, 0.08)
        b.fold(mirror_same([(846, 590), (812, 630)]), 12, 0.08)
    # logo anchors (px). left/right = wearer's (left chest = viewer's right on a front view)
    if view == "front":
        b.anchors = {"chest_center": _anchor(500, 330, 280), "full_front": _anchor(500, 430, 360),
                     "left_chest": _anchor(612, 300, 90), "right_chest": _anchor(388, 300, 90),
                     "hem": _anchor(640, 760, 80), "sleeve": _anchor(195 if not long_sleeves else 175, 250, 60)}
    else:
        b.anchors = {"upper_back": _anchor(500, 300, 300), "back_center": _anchor(500, 380, 340),
                     "chest_center": _anchor(500, 330, 280), "full_front": _anchor(500, 420, 360),
                     "sleeve": _anchor(805, 250, 60)}
    return nl


def tshirt(view="front", vneck=False):
    b = _B("tshirt_vneck" if vneck else "tshirt", view)
    _tee_body(b, view, vneck=vneck)
    return b.build()


def longsleeve(view="front", sweater=False, vneck=False):
    b = _B(("sweater" if sweater else "longsleeve_tee") + ("_vneck" if vneck else ""), view)
    _tee_body(b, view, long_sleeves=True, sweater=sweater, y_hem=830, vneck=vneck)
    return b.build()


def polo(view="front"):
    b = _B("polo", view)
    _tee_body(b, view, polo=True)
    if view == "front":
        # placket
        pl = [(478, 176), (522, 176), (522, 352), (478, 352)]
        b.part(pl)
        b.seam(pl, closed=True, w=1.6)
        b.stitch([(484, 182), (484, 346), (516, 346), (516, 182)], "tonal")
        for y in (218, 270, 322):
            b.hardware.append((ellipse(500, y, 8.5, 8.5), "button", None))
        # collar: two flat knit wings + the collar band at the back
        wing = Pen(506, 170).C(540, 150, 575, 118, 596, 106).C(612, 112, 630, 124, 640, 140)
        wing.C(626, 190, 600, 238, 584, 266).C(560, 250, 530, 228, 510, 200).L(506, 170)
        band = Pen(404, 106).C(440, 96, 560, 96, 596, 106).L(588, 124).C(550, 116, 450, 116, 412, 124).pts
        # inside of the collar band, visible between the wings above the top button
        gap = Pen(412, 122).C(450, 114, 550, 114, 588, 122).L(510, 196).L(490, 196).L(412, 122).pts
        b.part(gap)
        b.inner = np.maximum(b.inner, fill(gap))
        cw = b.part(wing.pts)
        cw2 = b.part(mirror_same(wing.pts))
        b.part(band)
        b.ribband(band)
        b.seam(wing.pts, closed=True, w=1.8)
        b.seam(mirror_same(wing.pts), closed=True, w=1.8)
        b.stitch(offset_curve(wing.pts[28:60], dx=-6, dy=0), "tonal")
        b.fold([(640, 140), (660, 168)], 16, 0.12)
        b.fold(mirror_same([(640, 140), (660, 168)]), 16, 0.12)
        # the collar casts a soft shadow on the chest
        b.dark.append((Pen(586, 272).C(610, 240, 636, 190, 648, 146).pts, 18, 0.10))
        b.dark.append((mirror_same(Pen(586, 272).C(610, 240, 636, 190, 648, 146).pts), 18, 0.10))
        b.anchors["left_chest"] = _anchor(622, 318, 95)
        b.anchors["right_chest"] = _anchor(378, 318, 95)
    else:
        band = Pen(404, 112).C(440, 100, 560, 100, 596, 112).L(592, 150).C(550, 142, 450, 142, 408, 150).pts
        b.part(band)
        b.ribband(band, horizontal_ribs=False)
        b.seam(band, closed=True, w=1.6)
    return b.build()


def _hood_front(b: _B, zip_=False):
    outer = Pen(500, 36).C(560, 34, 640, 44, 668, 110).C(690, 160, 680, 200, 652, 232)
    outer.L(500, 290 if not zip_ else 300)
    hood_poly = sym(outer.pts)
    b.part(hood_poly)
    # the opening: lining visible
    op = Pen(500, 70).C(548, 70, 590, 90, 604, 140).C(612, 190, 570, 250, 505, 300 if not zip_ else 306)
    op.L(500, 300 if not zip_ else 306)
    opening = sym(op.pts)
    lin = fill(opening)
    b.inner = np.maximum(b.inner, lin)
    b.seam(Pen(500, 40).C(500, 60, 500, 70, 500, 72).pts, w=1.6)
    b.stitch(offset_curve(op.pts, dx=10, dy=-4, towards=(500, 180), k=-0.05), "tonal")
    b.stitch(mirror_same(offset_curve(op.pts, dx=10, dy=-4, towards=(500, 180), k=-0.05)), "tonal")
    # hood fabric edge (the rim) casts shadow into the opening
    b.dark.append((op.pts, 20, 0.18))
    b.dark.append((mirror_same(op.pts), 20, 0.18))
    b.hi.append((offset_curve(op.pts, dx=14, dy=-6), 22, 0.12))
    b.hi.append((mirror_same(offset_curve(op.pts, dx=14, dy=-6)), 22, 0.08))
    return lin


def _hoodie_body(b: _B, view: str, zip_=False, hood=True, collar=False, strings=True):
    y_hem = 880
    shoulder = (744, 244)
    armpit = (752, 352)
    neck = (640, 214)
    body = Pen(500, 212 if view == "front" else 200).C(560, 212, 610, 214, *neck).L(*shoulder)
    body.C(750, 280, 752, 320, *armpit).C(752, 500, 752, 700, 752, 806).L(742, y_hem).C(650, y_hem + 4, 560, y_hem + 6, 500, y_hem + 6)
    b.part(sym(body.pts))
    sl = Pen(*shoulder).C(820, 262, 852, 316, 864, 410).C(876, 560, 884, 700, 888, 796).L(894, 874).L(804, 880)
    sl.L(800, 800).C(792, 660, 782, 520, 772, 440).C(768, 400, 762, 370, *armpit).C(752, 320, 750, 280, *shoulder)
    b.part(sl.pts)
    b.part(mirror_same(sl.pts))
    b.seam(Pen(*shoulder).C(750, 280, 752, 320, *armpit).pts)
    b.seam(mirror_same(Pen(*shoulder).C(750, 280, 752, 320, *armpit).pts))
    cuff = [(888, 800), (894, 874), (804, 880), (800, 804)]
    b.ribband(cuff)
    b.ribband(mirror_same(cuff))
    b.seam([cuff[0], cuff[3]])
    b.seam(mirror_same([cuff[0], cuff[3]]))
    hem = Pen(258, 806).C(360, 810, 440, 812, 500, 812).C(560, 812, 640, 810, 742, 806).pts
    band = [(258, 806)] + hem[1:] + [(742, y_hem), (500, y_hem + 6), (258, y_hem)]
    b.ribband(band)
    b.seam(hem)
    # sleeve folds + body drape
    for y in (470, 580, 690):
        b.fold([(870, y), (826, y + 34)], 12, 0.09, hi=0.08, hi_offset=(0, -12))
        b.fold(mirror_same([(870, y + 20), (826, y + 54)]), 12, 0.09)
    b.fold([(752, 370), (700, 430), (680, 500)], 20, 0.07)
    b.fold(mirror_same([(752, 370), (700, 430), (680, 500)]), 20, 0.07)
    b.fold([(600, 260), (620, 420), (610, 520)], 22, 0.05, hi=0.08, hi_offset=(-18, 0))
    b.sheen([(410, 300), (430, 480)], 100, 0.10)
    b.sheen([(820, 290), (860, 700)], 36, 0.10)
    b.sheen(mirror_same([(820, 290), (860, 700)]), 36, 0.06)
    if view == "front":
        if hood:
            _hood_front(b, zip_)
        elif collar:
            st = Pen(500, 170).C(560, 168, 610, 178, 640, 214).L(636, 250).C(600, 226, 560, 222, 500, 222)
            b.part(sym(st.pts))
            b.ribband(sym(st.pts))
            b.seam(sym(st.pts), closed=True)
        # pockets
        if zip_:
            pk = Pen(514, 600).L(596, 600).C(620, 640, 640, 670, 660, 700).L(660, 806).L(514, 806)
            b.part(pk.pts + [(514, 600)])
            b.seam(pk.pts + [(514, 600)], closed=True, w=1.6)
            b.part(mirror_same(pk.pts + [(514, 600)]))
            b.seam(mirror_same(pk.pts + [(514, 600)]), closed=True, w=1.6)
            mouth = [(596, 600), (660, 700), (668, 700), (604, 598)]
            b.dark.append(([(600, 602), (662, 700)], 14, 0.28))
            b.dark.append((mirror_same([(600, 602), (662, 700)]), 14, 0.28))
        else:
            pk = Pen(500, 560).L(606, 560).C(630, 610, 660, 650, 678, 676).L(678, 806).L(500, 806)
            poly = sym(pk.pts)
            b.part(poly)
            b.seam(poly, closed=True, w=1.6)
            b.stitch(offset_curve(pk.pts[:2], dy=8), "tonal")
            b.stitch(mirror_same(offset_curve(pk.pts[:2], dy=8)), "tonal")
            b.dark.append(([(608, 564), (676, 676)], 16, 0.30))
            b.dark.append((mirror_same([(608, 564), (676, 676)]), 16, 0.30))
            b.hi.append(([(620, 560), (690, 672)], 14, 0.10))
        if zip_:
            tape = fill([(491, 300), (509, 300), (509, y_hem + 4), (491, y_hem + 4)])
            teeth = np.zeros((S, S), np.float32)
            for y in np.arange(306, y_hem, 7.0):
                teeth = np.maximum(teeth, fill([(493, y), (507, y), (507, y + 4), (493, y + 4)]))
            b.hardware.append((tape * 0.9, "zip_tape", None))
            b.hardware.append((teeth, "metal", None))
            pull = fill([(494, 302), (506, 302), (508, 350), (500, 358), (492, 350)])
            b.hardware.append((pull, "metal", None))
            b.stitch([(486, 310), (486, y_hem - 4)], "tonal")
            b.stitch([(514, 310), (514, y_hem - 4)], "tonal")
        if hood and strings:  # drawstrings with aglets
            for x0, x1 in ((474, 466), (526, 534)) if not zip_ else ((470, 462), (530, 538)):
                s = stroke([(x0, 290), (x0 - 2 if x0 < 500 else x0 + 2, 360), (x1, 470)], 9)
                b.hardware.append((s, "string", None))
                ag = fill([(x1 - 5, 468), (x1 + 5, 468), (x1 + 5, 510), (x1 - 5, 510)])
                b.hardware.append((ag, "aglet", None))
                b.hardware.append((ellipse(x0, 288, 7, 7), "eyelet", None))
        b.anchors = {"chest_center": _anchor(500, 430, 300), "full_front": _anchor(500, 470, 360),
                     "left_chest": _anchor(612, 380, 90), "right_chest": _anchor(388, 380, 90),
                     "sleeve": _anchor(175, 450, 60), "hem": _anchor(640, 760, 80)}
    else:
        if hood:
            hb = Pen(500, 60).C(580, 60, 650, 80, 660, 160).C(664, 200, 650, 222, 640, 232).L(500, 250)
            b.part(sym(hb.pts))
            b.seam([(500, 64), (500, 248)], w=1.6)
            b.dark.append((Pen(360, 230).C(420, 262, 580, 262, 640, 230).pts, 24, 0.14))
        b.anchors = {"upper_back": _anchor(500, 360, 320), "back_center": _anchor(500, 460, 360),
                     "chest_center": _anchor(500, 430, 300), "sleeve": _anchor(825, 450, 60)}


def hoodie(view="front", strings=True):
    b = _B("hoodie", view)
    _hoodie_body(b, view, strings=strings)
    return b.build()


def zip_hoodie(view="front", strings=True):
    b = _B("zip_hoodie", view)
    _hoodie_body(b, view, zip_=True, strings=strings)
    return b.build()


def jacket(view="front"):
    b = _B("jacket", view)
    _hoodie_body(b, view, zip_=True, hood=False, collar=True)
    return b.build()


# ------------------------------------------------------------------ bottoms
def _pants(b: _B, view: str, hem_y=940, denim=True, shorts=False):
    top = 108
    wb_bot = 160
    right = Pen(500, top).L(702, top - 4).C(712, 200, 724, 300, 726, 420)
    if shorts:
        right.C(728, 460, 732, 500, 736, hem_y).L(530, hem_y + 6).C(522, 420, 512, 400, 500, 396)
    else:
        right.C(726, 560, 712, 760, 700, hem_y).L(544, hem_y + 4).C(536, 700, 520, 520, 508, 448).C(506, 444, 503, 442, 500, 440)
    poly = sym(right.pts)
    b.part(poly)
    wb = [(298, top - 4), (500, top), (702, top - 4), (704, wb_bot - 2), (500, wb_bot), (296, wb_bot - 2)]
    b.part(wb)
    b.seam([(296, wb_bot - 2), (500, wb_bot), (704, wb_bot - 2)])
    kind = "contrast" if denim else "tonal"
    b.stitch([(300, top + 8), (500, top + 12), (700, top + 8)], kind)
    b.stitch([(300, wb_bot - 10), (500, wb_bot - 8), (700, wb_bot - 10)], kind)
    # belt loops
    loops = [320, 420, 580, 680] + ([500] if view == "back" else [])
    for x in loops:
        lp = [(x - 9, top - 6), (x + 9, top - 6), (x + 9, wb_bot + 10), (x - 9, wb_bot + 10)]
        b.part(lp)
        b.seam(lp, closed=True, w=1.4)
        b.dark.append(([(x - 11, top), (x - 11, wb_bot + 8)], 6, 0.12))
    # side seams / inseams
    outer = Pen(702, wb_bot).C(712, 220, 724, 300, 726, 420)
    outer = outer.C(726, 560, 712, 760, 700, hem_y) if not shorts else outer.C(728, 460, 732, 500, 736, hem_y)
    b.stitch(offset_curve(outer.pts, dx=-8), kind)
    b.stitch(mirror_same(offset_curve(outer.pts, dx=-8)), kind)
    if not shorts:
        ins = Pen(544, hem_y + 4).C(536, 700, 520, 520, 508, 448).pts
        b.stitch(offset_curve(ins, dx=8), kind)
        b.stitch(mirror_same(offset_curve(ins, dx=8)), kind)
    # hems
    hy = hem_y
    b.stitch([(706 if not shorts else 730, hy - 22), (548 if not shorts else 532, hy - 18)], kind)
    b.stitch(mirror_same([(706 if not shorts else 730, hy - 22), (548 if not shorts else 532, hy - 18)]), kind)
    b.seam([(706 if not shorts else 732, hy - 30), (548 if not shorts else 532, hy - 26)], w=1.4)
    b.seam(mirror_same([(706 if not shorts else 732, hy - 30), (548 if not shorts else 532, hy - 26)]), w=1.4)
    if view == "front":
        # fly + button + rivets
        fly = Pen(528, wb_bot).L(528, 330).C(528, 356, 516, 372, 502, 380).pts
        b.stitch(fly, kind)
        b.seam([(500, wb_bot), (500, 400)], w=1.8)
        b.hardware.append((ellipse(500, 134, 13, 13), "metal", None))
        # front pockets
        pk = Pen(612, wb_bot).C(640, 230, 680, 262, 718, 272).pts
        b.seam(pk, w=2.0)
        b.dark.append((pk, 18, 0.16))
        b.stitch(offset_curve(pk, dx=-6, dy=8), kind)
        b.seam(mirror_same(pk), w=2.0)
        b.dark.append((mirror_same(pk), 18, 0.16))
        b.stitch(mirror_same(offset_curve(pk, dx=-6, dy=8)), kind)
        if denim:
            coin = [(636, wb_bot + 6), (684, wb_bot + 4), (690, 222), (642, 228)]
            b.seam(coin[1:3] + [coin[3]], w=1.4)
            b.stitch(offset_curve([coin[3], coin[0]], dy=-2), kind)
            for (x, y) in ((614, wb_bot + 6), (716, 268), (386, wb_bot + 6), (284, 268)):
                b.hardware.append((ellipse(x, y, 5, 5), "metal", None))
        b.anchors = {"front_waist": _anchor(640, 200, 60), "hem": _anchor(640, 880, 60)}
    else:
        # yoke, centre back seam, back pockets
        yoke = Pen(296, 220).C(400, 234, 470, 250, 500, 262).C(530, 250, 600, 234, 704, 220).pts
        b.seam(yoke)
        b.stitch(offset_curve(yoke, dy=10), kind)
        b.seam([(500, wb_bot), (500, 262), (500, 430)], w=1.8)
        b.stitch([(508, 262), (508, 430)], kind)
        pk = [(548, 272), (680, 258), (676, 392), (614, 418), (556, 394)]
        b.part(pk + [pk[0]])
        b.seam(pk, closed=True, w=1.8)
        b.stitch(offset_curve(pk + [pk[0]], towards=(614, 330), k=0.06), kind)
        b.dark.append((pk + [pk[0]], 10, 0.10))
        pkl = mirror_same(pk)
        b.part(pkl + [pkl[0]])
        b.seam(pkl, closed=True, w=1.8)
        b.stitch(mirror_same(offset_curve(pk + [pk[0]], towards=(614, 330), k=0.06)), kind)
        b.dark.append((pkl + [pkl[0]], 10, 0.10))
        if denim:  # arcuate stitching
            arc = Pen(566, 300).C(590, 350, 610, 350, 614, 322).C(620, 350, 644, 350, 662, 290).pts
            b.stitch(arc, kind)
            b.stitch(mirror_same(arc), kind)
        b.anchors = {"back_waistband_right": _anchor(620, 134, 92), "back_waistband_left": _anchor(380, 134, 92),
                     "back_pocket": _anchor(614, 330, 90), "front_waist": _anchor(620, 134, 92)}
    # leg drape + denim whiskers
    if not shorts:
        for x0 in (600, 640):
            b.fold([(x0, 480), (x0 - 10, 700), (x0 + 6, 900)], 26, 0.05, hi=0.10, hi_offset=(-26, 0))
        b.fold(mirror_same([(620, 480), (612, 700), (626, 900)]), 26, 0.05, hi=0.08, hi_offset=(26, 0))
        for y in (640, 660):  # knee creases
            b.fold([(560, y), (690, y + 16)], 8, 0.04)
            b.fold(mirror_same([(560, y + 10), (690, y + 26)]), 8, 0.04)
        for y in (880, 905):  # stacking at the hem
            b.fold([(560, y), (700, y - 6)], 8, 0.05)
            b.fold(mirror_same([(560, y + 4), (700, y - 2)]), 8, 0.05)
    if view == "front":
        for i, (a, c) in enumerate((((520, 410), (600, 380)), ((518, 440), (590, 424)), ((516, 470), (580, 468)))):
            b.fold([a, c], 7, 0.06, hi=0.12, hi_offset=(0, -7))
            b.fold(mirror_same([a, c]), 7, 0.06, hi=0.12, hi_offset=(0, -7))
    b.denim = denim
    if denim:
        yy, xx = np.mgrid[:S, :S].astype(np.float32) / K
        f = np.zeros((S, S), np.float32)
        for cx in (612, 388):  # faded thigh/knee centre of each leg
            f += np.exp(-(((xx - cx) / 60) ** 2) - (((yy - 460) / 190) ** 2)) * 0.9
            f += np.exp(-(((xx - cx) / 55) ** 2) - (((yy - 700) / 70) ** 2)) * 0.5
        b.fade = np.clip(f, 0, 1)


def jeans(view="front"):
    b = _B("jeans", view)
    _pants(b, view, denim=True)
    return b.build()


def trousers(view="front"):
    b = _B("trousers", view)
    _pants(b, view, denim=False)
    t = b.build()
    return t


def shorts(view="front", denim=True):
    b = _B("shorts", view)
    _pants(b, view, hem_y=520, denim=denim, shorts=True)
    return b.build()


def skirt(view="front"):
    b = _B("skirt", view)
    wb = [(352, 150), (648, 150), (650, 200), (350, 200)]
    body = Pen(500, 196).L(650, 196).C(700, 380, 760, 560, 800, 760).C(700, 780, 600, 786, 500, 786)
    b.part(sym(body.pts))
    b.part(wb)
    b.seam([(350, 200), (650, 200)])
    b.stitch([(354, 190), (646, 190)], "tonal")
    b.stitch([(212, 752), (500, 772), (788, 752)], "tonal")
    for x in (560, 620, 690):
        b.fold([(x - 20, 260), (x, 520), (x + 30, 770)], 26, 0.06, hi=0.10, hi_offset=(-24, 0))
        b.fold(mirror_same([(x - 20, 260), (x, 520), (x + 30, 770)]), 26, 0.06)
    b.anchors = {"front_waist": _anchor(600, 240, 60), "chest_center": _anchor(500, 400, 200),
                 "hem": _anchor(640, 700, 70)}
    return b.build()


def dress(view="front", sleeves="sleeveless"):
    b = _B("dress", view)
    if sleeves == "short":
        _tee_body(b, view, y_hem=420)
        body = Pen(500, 380).L(718, 380).C(740, 560, 790, 760, 820, 950).C(700, 972, 600, 976, 500, 976)
        b.part(sym(body.pts))
        b.seam(Pen(282, 400).C(400, 420, 600, 420, 718, 400).pts)
    else:
        strap = [(566, 60), (582, 60), (604, 190), (588, 196)]
        b.part(strap)
        b.part(mirror_same(strap))
        body = Pen(500, 262).C(540, 250, 570, 214, 592, 188).L(612, 186).C(640, 210, 652, 240, 654, 262)
        body.C(640, 360, 620, 420, 624, 460).C(680, 640, 760, 820, 800, 950).C(700, 972, 600, 976, 500, 976)
        b.part(sym(body.pts))
        b.seam(Pen(346, 460).C(420, 470, 580, 470, 654, 460).pts, w=1.6)
        b.fold([(560, 300), (574, 400)], 16, 0.06)
    b.stitch([(206, 930), (500, 960), (794, 930)], "tonal")
    for x in (560, 640, 720):
        b.fold([(x - 30, 480), (x, 720), (x + 30, 960)], 30, 0.06, hi=0.10, hi_offset=(-26, 0))
        b.fold(mirror_same([(x - 30, 480), (x, 720), (x + 30, 960)]), 30, 0.06)
    b.anchors = {"chest_center": _anchor(500, 320, 200), "full_front": _anchor(500, 400, 260),
                 "left_chest": _anchor(600, 300, 70), "upper_back": _anchor(500, 320, 220),
                 "back_center": _anchor(500, 400, 260)}
    return b.build()


# ------------------------------------------------------------------ registry
_BUILDERS = {
    "tshirt": lambda v: tshirt(v),
    "tshirt_vneck": lambda v: tshirt(v, vneck=True),
    "longsleeve_vneck": lambda v: longsleeve(v, vneck=True),
    "sweater_vneck": lambda v: longsleeve(v, sweater=True, vneck=True),
    "longsleeve_tee": lambda v: longsleeve(v),
    "sweater": lambda v: longsleeve(v, sweater=True),
    "polo": lambda v: polo(v),
    "hoodie": lambda v: hoodie(v),
    "hoodie_nostrings": lambda v: hoodie(v, strings=False),
    "zip_hoodie": lambda v: zip_hoodie(v),
    "zip_hoodie_nostrings": lambda v: zip_hoodie(v, strings=False),
    "jacket": lambda v: jacket(v),
    "jeans": lambda v: jeans(v),
    "trousers": lambda v: trousers(v),
    "shorts": lambda v: shorts(v),
    "skirt": lambda v: skirt(v),
    "dress": lambda v: dress(v),
    "dress_short_sleeve": lambda v: dress(v, sleeves="short"),
}
TEMPLATE_NAMES = sorted(_BUILDERS)


def _affine(t: Template, sx: float, sy: float, cx: float = 500, cy: float = 0) -> Template:
    """Re-proportion a template (x scaled about cx, y about cy, in 1000-unit space) -- all maps + anchors."""
    import cv2
    M = np.array([[sx, 0, (cx * K) * (1 - sx)], [0, sy, (cy * K) * (1 - sy)]], np.float32)

    def w(a, border=0.0):
        if a is None:
            return None
        return cv2.warpAffine(a.astype(np.float32), M, (S, S), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=border)
    anchors = {k: (sx * x + M[0, 2], sy * y + M[1, 2], mw * min(sx, sy)) for k, (x, y, mw) in t.anchors.items()}
    return Template(t.name, t.view, w(t.mask), w(t.inner), w(t.rib), w(t.shade, 1.0), w(t.light),
                    [(w(a), k) for a, k in t.stitches], [(w(a), k, ls) for a, k, ls in t.hardware],
                    anchors, t.denim, w(t.fade))


# real flat-lay proportions: jeans/trousers are ~0.38 wide:long (the drawn outline is ~0.49)
# y' = sy*y + cy*(1-sy): waistband top 108 -> 60, hem 940 -> 971
_RESHAPE = {"jeans": (0.86, 1.095, 500, 613.3), "trousers": (0.86, 1.095, 500, 613.3)}


@lru_cache(maxsize=48)
def get_template(name: str, view: str = "front") -> Template:
    if name not in _BUILDERS:
        raise KeyError(name)
    t = _BUILDERS[name]("back" if view == "back" else "front")
    if name in _RESHAPE:
        sx, sy, cx, cy = _RESHAPE[name]
        t = _affine(t, sx, sy, cx, cy)
    return t
