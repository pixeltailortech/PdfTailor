"""Ruled-table detection from a rendered page.

Works on pixels, so one code path serves both real PDFs (whose borders are vector lines
that render to pixels) and scans (whose borders are already pixels). A table is a group of
long horizontal and vertical lines that touch each other; its grid is the set of distinct
line positions, and a *separator* between two neighbouring cells exists only where the
line is actually drawn — a missing separator is how merged cells show up.

Coordinates returned are display points (pixel / scale), top-left origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .analysis.imaging import distance_map, ink_mask
from .geometry import Rect

MIN_H_LINE_PT = 16.0  # shortest horizontal stroke taken for a table line
MIN_V_LINE_PT = 10.0  # shortest vertical stroke
MAX_LINE_PT = 3.5  # thicker strokes are filled bands, not rules
SEP_COVERAGE = 0.5  # share of a cell edge that must be inked for a separator to count


@dataclass
class Grid:
    bounds: Rect
    xs: list[float]  # column boundaries (cols + 1 values)
    ys: list[float]  # row boundaries (rows + 1 values)
    vsep: list[list[bool]]  # vsep[r][c]: rule between column c and c+1 on row r
    hsep: list[list[bool]]  # hsep[r][c]: rule between row r and r+1 in column c
    line_pt: float  # typical rule thickness
    line_color: tuple[int, int, int] = (0, 0, 0)
    # the strokes the grid was built from, (x0, y0, x1, y1) in points (see erase_rules)
    strokes: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.ys) - 1

    @property
    def cols(self) -> int:
        return len(self.xs) - 1


@dataclass
class _Seg:
    pos: float  # y for horizontal, x for vertical (px, centre of the stroke)
    a: float  # start along the stroke
    b: float  # end along the stroke
    thick: float


def _segments(mask: np.ndarray, horizontal: bool, max_thick: float, min_len: float = 0.0) -> list[_Seg]:
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: list[_Seg] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if max(w, h) < min_len:
            continue
        # Thickness is ink per unit of length, not the box's height: a rule on a page scanned
        # a fraction of a degree askew has a tall box (2000 px long, 0.4° → 14 px) while the
        # stroke itself is still thin.
        if horizontal:
            thick = min(h, area / max(w, 1))
            if thick > max_thick or h > max_thick + 0.02 * w:
                continue
            out.append(_Seg(y + h / 2.0, x, x + w, thick))
        else:
            thick = min(w, area / max(h, 1))
            if thick > max_thick or w > max_thick + 0.02 * h:
                continue
            out.append(_Seg(x + w / 2.0, y, y + h, thick))
    return out


def _isolated(mask: np.ndarray, seg: _Seg, horizontal: bool, scale: float) -> bool:
    """A rule has blank paper along both sides; a letter's stroke has the rest of the letter.

    On a scan, the long vertical strokes of bold 12–14 pt characters pass the length test
    and turned one table into 35 columns. The ink just beside the stroke tells them apart:
    a short stroke must be almost alone, a long one may have text close to it here and there.
    """
    H, W = mask.shape
    half = int(np.ceil(seg.thick / 2.0)) + 1
    width = max(3, int(round(1.2 * scale)))
    p, a, b = int(round(seg.pos)), max(0, int(seg.a)), int(seg.b)
    if b - a < 2:
        return False
    sides = []
    for sgn in (-1, 1):
        lo, hi = sorted((p + sgn * (half + 1), p + sgn * (half + width)))
        lo, hi = max(0, lo), min((H if horizontal else W) - 1, hi)
        if hi < lo:
            sides.append(0.0)
            continue
        band = mask[lo:hi + 1, a:b] if horizontal else mask[a:b, lo:hi + 1]
        sides.append(float(band.mean()) if band.size else 0.0)
    length_pt = (b - a) / scale
    limit = 0.05 + 0.15 * min(1.0, max(0.0, (length_pt - 10.0) / 40.0))
    return (sides[0] + sides[1]) / 2.0 <= limit


def _inside_text(seg: _Seg, horizontal: bool, boxes: list[tuple[float, float, float, float]]) -> bool:
    """True when most of the stroke runs through boxes known to hold text."""
    spans = []
    for x0, y0, x1, y1 in boxes:
        if horizontal:
            if y0 <= seg.pos <= y1:
                spans.append((max(seg.a, x0), min(seg.b, x1)))
        elif x0 <= seg.pos <= x1:
            spans.append((max(seg.a, y0), min(seg.b, y1)))
    covered, end = 0.0, seg.a
    for lo, hi in sorted(s for s in spans if s[1] > s[0]):
        lo = max(lo, end)
        if hi > lo:
            covered += hi - lo
            end = hi
    return covered >= 0.6 * max(1.0, seg.b - seg.a)


def _box_text_share(r: tuple[int, int, int, int], boxes: list[tuple[float, float, float, float]]) -> float:
    x0, y0, x1, y1 = r
    area = max(1.0, (x1 - x0) * (y1 - y0))
    m = np.zeros((max(1, y1 - y0), max(1, x1 - x0)), bool)
    for bx0, by0, bx1, by1 in boxes:
        a0, a1 = int(max(bx0, x0) - x0), int(min(bx1, x1) - x0)
        b0, b1 = int(max(by0, y0) - y0), int(min(by1, y1) - y0)
        if a1 > a0 and b1 > b0:
            m[b0:b1, a0:a1] = True
    return float(m.sum()) / area


def _cluster(segs: list[_Seg], tol: float) -> list[float]:
    """Positions of distinct rules: strokes closer than ``tol`` are one rule."""
    if not segs:
        return []
    items = sorted(segs, key=lambda s: s.pos)
    groups: list[list[_Seg]] = [[items[0]]]
    for s in items[1:]:
        if s.pos - groups[-1][-1].pos <= tol:
            groups[-1].append(s)
        else:
            groups.append([s])
    res = []
    for g in groups:
        wsum = sum(max(1.0, s.b - s.a) for s in g)
        res.append(sum(s.pos * max(1.0, s.b - s.a) for s in g) / wsum)
    return res


def _near(v: float, positions: list[float], tol: float) -> bool:
    return any(abs(v - p) <= tol for p in positions)


def find_grids(rgb: np.ndarray, scale: float, text_boxes: list[tuple[float, float, float, float]] | None = None) -> list[Grid]:
    """Detects ruled tables on a page rendered at ``scale`` pixels per point.

    ``text_boxes`` (pixels, x0 y0 x1 y1) are where text is known to be — characters the OCR
    engine read, or text blocks. A stroke lying inside text is part of a letter (a heavy
    title's 書 has strokes as long and as straight as a rule), not a table border.
    """
    H, W = rgb.shape[:2]
    mask, _bg, _thr = ink_mask(rgb)
    u8 = (mask.astype(np.uint8)) * 255

    lh = max(12, int(round(MIN_H_LINE_PT * scale)))
    lv = max(8, int(round(MIN_V_LINE_PT * scale)))
    max_thick = MAX_LINE_PT * scale + 3
    # one pixel of thickening across the stroke tolerates blur and a slightly crooked scan
    hm = cv2.morphologyEx(cv2.dilate(u8, np.ones((3, 1), np.uint8)), cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (lh, 1)))
    vm = cv2.morphologyEx(cv2.dilate(u8, np.ones((1, 3), np.uint8)), cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (1, lv)))

    fh, fv, eh, ev, rects = _fill_edges(rgb, _bg, scale, mask, text_boxes)
    for x, y, w, h in rects:
        # A fill dark enough to count as ink is itself the stroke detector's prey, and its lettering
        # leaves slivers between the glyphs; a light fill is not ink, so its rules are real rules.
        if mask[y:y + h, x:x + w].mean() < 0.5:
            continue
        hm[max(0, y - 2) + 3:y + h - 1, max(0, x - 2):x + w + 2] = 0
        vm[max(0, y - 3):y + h + 3, x + 3:x + w - 3] = 0
    hsegs = [s for s in _segments(hm, True, max_thick, lh) if _isolated(mask, s, True, scale)]
    vsegs = [s for s in _segments(vm, False, max_thick, lv) if _isolated(mask, s, False, scale)]
    if text_boxes:
        hsegs = [s for s in hsegs if not _inside_text(s, True, text_boxes)]
        vsegs = [s for s in vsegs if not _inside_text(s, False, text_boxes)]
    hsegs += fh
    vsegs += fv
    hm = hm | eh
    vm = vm | ev
    if len(hsegs) < 2 or len(vsegs) < 2:
        return []

    # thin rules only, redrawn, then grouped by touching
    thin = np.zeros((H, W), np.uint8)
    for s in hsegs:
        cv2.line(thin, (int(s.a), int(round(s.pos))), (int(s.b), int(round(s.pos))), 255, 1)
    for s in vsegs:
        cv2.line(thin, (int(round(s.pos)), int(s.a)), (int(round(s.pos)), int(s.b)), 255, 1)
    joined = cv2.dilate(thin, np.ones((5, 5), np.uint8))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)

    tol = max(3.0, 1.8 * scale)
    grids: list[Grid] = []
    for i in range(1, n):
        rx, ry, rw, rh, _ = (int(v) for v in stats[i])
        if rw < 20 * scale or rh < 8 * scale:
            continue
        inside = lambda s, horiz: (  # noqa: E731
            (rx - tol <= s.a and s.b <= rx + rw + tol and ry - tol <= s.pos <= ry + rh + tol)
            if horiz
            else (ry - tol <= s.a and s.b <= ry + rh + tol and rx - tol <= s.pos <= rx + rw + tol)
        )
        hs = [s for s in hsegs if inside(s, True)]
        vs = [s for s in vsegs if inside(s, False)]
        if len(hs) < 1 or len(vs) < 1:
            continue
        ys = _cluster(hs, tol)
        xs = _cluster(vs, tol)
        # A stroke only belongs to the grid when it ends on a crossing rule; an underline
        # or a stray stroke inside a cell does not.
        hs = [s for s in hs if _near(s.a, xs, 3 * tol) and _near(s.b, xs, 3 * tol)]
        vs = [s for s in vs if _near(s.a, ys, 3 * tol) and _near(s.b, ys, 3 * tol)]
        if not hs or not vs:
            continue
        ys = _cluster(hs, tol)
        xs = _cluster(vs, tol)
        if len(ys) < 2 or len(xs) < 2 or (len(ys) - 1) * (len(xs) - 1) < 2:
            continue
        if xs[-1] - xs[0] < 20 * scale or ys[-1] - ys[0] < 8 * scale:
            continue

        # Rules narrower than a letter are table noise: drop columns/rows thinner than 3pt
        xs = _drop_thin(xs, 3.0 * scale)
        ys = _drop_thin(ys, 3.0 * scale)
        if len(ys) < 2 or len(xs) < 2 or (len(ys) - 1) * (len(xs) - 1) < 2:
            continue

        vsep, hsep = _separators(hm, vm, xs, ys, scale)
        thick = float(np.median([s.thick for s in hs + vs])) / scale
        color = _rule_color(rgb, hm, vm, xs, ys)
        grids.append(Grid(
            bounds=Rect.from_bounds(xs[0] / scale, ys[0] / scale, xs[-1] / scale, ys[-1] / scale),
            xs=[x / scale for x in xs],
            ys=[y / scale for y in ys],
            vsep=vsep,
            hsep=hsep,
            line_pt=max(0.5, thick - 2.0 / scale),
            line_color=color,
            strokes=[(s.a / scale, s.pos / scale - s.thick / scale, s.b / scale, s.pos / scale + s.thick / scale) for s in hs]
                    + [(s.pos / scale - s.thick / scale, s.a / scale, s.pos / scale + s.thick / scale, s.b / scale) for s in vs],
        ))
    grids.sort(key=lambda g: (g.bounds.y, g.bounds.x))
    return grids


def _drop_thin(pos: list[float], min_gap: float) -> list[float]:
    out = [pos[0]]
    for p in pos[1:]:
        if p - out[-1] >= min_gap:
            out.append(p)
        elif p == pos[-1]:
            out[-1] = p  # keep the outer edge
    return out


def _separators(hm: np.ndarray, vm: np.ndarray, xs: list[float], ys: list[float], scale: float):
    H, W = hm.shape
    pad = 2
    rows, cols = len(ys) - 1, len(xs) - 1
    vsep = [[True] * max(0, cols - 1) for _ in range(rows)]
    hsep = [[True] * cols for _ in range(max(0, rows - 1))]
    for r in range(rows):
        y0, y1 = int(ys[r]) + pad, int(ys[r + 1]) - pad
        if y1 <= y0:
            continue
        for c in range(1, cols):
            x = int(round(xs[c]))
            band = vm[max(0, y0):min(H, y1), max(0, x - 2):min(W, x + 3)]
            vsep[r][c - 1] = band.size > 0 and float((band.max(axis=1) > 0).mean()) >= SEP_COVERAGE
    for c in range(cols):
        x0, x1 = int(xs[c]) + pad, int(xs[c + 1]) - pad
        if x1 <= x0:
            continue
        for r in range(1, rows):
            y = int(round(ys[r]))
            band = hm[max(0, y - 2):min(H, y + 3), max(0, x0):min(W, x1)]
            hsep[r - 1][c] = band.size > 0 and float((band.max(axis=0) > 0).mean()) >= SEP_COVERAGE
    return vsep, hsep


def _rule_color(rgb, hm, vm, xs, ys) -> tuple[int, int, int]:
    """Colour of the rules: the darkest-quartile median over the pixels the line masks cover."""
    x0, x1 = int(xs[0]), int(xs[-1]) + 1
    y0, y1 = int(ys[0]), int(ys[-1]) + 1
    m = (hm[y0:y1, x0:x1] > 0) | (vm[y0:y1, x0:x1] > 0)
    px = rgb[y0:y1, x0:x1][m]
    if px.size == 0:
        return (0, 0, 0)
    lum = px.astype(np.float32).sum(axis=1)
    keep = px[lum <= np.percentile(lum, 25)]
    c = np.median(keep, axis=0)
    return (int(c[0]), int(c[1]), int(c[2]))


def _fill_edges(rgb: np.ndarray, bg, scale: float, ink: np.ndarray | None = None,
                text_boxes: list[tuple[float, float, float, float]] | None = None):
    """Shaded cells (a coloured header band, grey cells) have no rules of their own, yet their
    edges are the table's grid. Returns each solid rectangle's four edges as segments plus the
    same edges drawn as masks (for the separator test).

    A morphological opening with a block larger than any rule or letter stroke leaves only the
    solid areas, so a fill that touches a table's lines is still found on its own."""
    H, W = rgb.shape[:2]
    fill = (distance_map(rgb, bg) > 10).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(6 * scale)), max(3, int(4 * scale))))
    solid = cv2.morphologyEx(fill, cv2.MORPH_OPEN, kernel)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    min_w, min_h = 14.0 * scale, 7.0 * scale
    hs: list[_Seg] = []
    vs: list[_Seg] = []
    rects: list[tuple[int, int, int, int]] = []
    eh = np.zeros((H, W), np.uint8)
    ev = np.zeros((H, W), np.uint8)
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if w < min_w or h < min_h or w * h > 0.6 * W * H or area < 0.7 * w * h:
            continue
        # A heavy glyph or a logo is a small, solid, ink-dark blob: not a shaded cell.
        if ink is not None and w < 40 * scale and float(ink[y:y + h, x:x + w].mean()) > 0.5:
            continue
        if text_boxes and _box_text_share((x, y, x + w, y + h), text_boxes) > 0.5:
            continue
        rects.append((x, y, w, h))
        for yy in (y, y + h - 1):
            hs.append(_Seg(float(yy), float(x), float(x + w), 3.0))
            eh[max(0, yy - 1):yy + 2, x:x + w] = 255
        for xx in (x, x + w - 1):
            vs.append(_Seg(float(xx), float(y), float(y + h), 3.0))
            ev[y:y + h, max(0, xx - 1):xx + 2] = 255
    return hs, vs, eh, ev, rects


def erase_rules(rgb: np.ndarray, grids: list[Grid], scale: float) -> np.ndarray:
    """A copy of the page with the tables' rules painted over in the paper colour.

    Text is measured and read from this copy: a rule inside a line's crop counted as a
    giant "glyph" (a cell's 報到 came out 36 pt) and was read as | or [ .
    """
    if not grids:
        return rgb
    mask, bg, _thr = ink_mask(rgb)
    u8 = mask.astype(np.uint8) * 255
    lh = max(12, int(round(MIN_H_LINE_PT * scale)))
    lv = max(8, int(round(MIN_V_LINE_PT * scale)))
    hm = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (lh, 1)))
    vm = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, lv)))
    lines = cv2.dilate(hm | vm, np.ones((3, 3), np.uint8)) > 0
    H, W = mask.shape
    sel = np.zeros((H, W), bool)
    for g in grids:
        for x0, y0, x1, y1 in g.strokes:
            a0, a1 = max(0, int(x0 * scale) - 3), min(W, int(np.ceil(x1 * scale)) + 4)
            b0, b1 = max(0, int(y0 * scale) - 3), min(H, int(np.ceil(y1 * scale)) + 4)
            sel[b0:b1, a0:a1] = True
    out = rgb.copy()
    out[lines & sel & mask] = bg
    return out
