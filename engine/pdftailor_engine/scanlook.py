"""Editing a scanned line without leaving a seam.

Redrawing a whole scanned line in crisp vector type shows, however well the font was
guessed: the neighbours are soft, slightly heavy pixels, and every character of the
redrawn line lands a little off its old place. So a text edit on a scan is *spliced*:

* the characters the edit did not touch keep their **original pixels** — the common
  start and end of the old and new text are found in the scan (template matching of a
  few characters on each side of the change) and left alone, or, when the new wording
  is longer or shorter, the tail is moved sideways as it is;
* only the changed characters are drawn, as pixels at the scan's resolution, with the
  **look of the scan**: stroke weight, edge softness, ink colour and grain are measured
  on the line itself (:func:`fit_look`) and applied to the new glyphs;
* letter spacing follows the line: the space the old wording took is compared with what
  the font alone would give, and the difference is carried over to the new wording, so
  justified lines stay justified.

The result is one image patch for the writer to paint; the new text is also written
invisibly on top so searching and copying find it.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .fonts import FontFace, FontIndex
from .geometry import Rect
from .models import Edit

SS = 3  # supersampling of rendered glyphs
MIN_ANCHOR_SCORE = 0.42
ANCHOR_GLYPHS = 4


@dataclass
class _Char:
    ch: str
    face: FontFace
    synth_bold: bool
    synth_italic: bool


@dataclass
class Strip:
    """Rendered text as ink coverage (0..1) at scan resolution."""

    cov: np.ndarray
    x0: float  # column of the pen origin
    base: int  # row of the baseline
    ink: tuple[int, int, int, int] | None  # x0, y0, x1, y1 of the ink, in ``cov``

    @property
    def ink_left(self) -> float:  # relative to the pen origin
        return self.ink[0] - self.x0

    @property
    def ink_right(self) -> float:
        return self.ink[2] - self.x0


@dataclass
class Look:
    sigma: float = 0.8  # blur, px
    tau: float = 0.5  # coverage that comes out half dark: lower = heavier strokes
    width: float = 1.0  # softness of the edge
    ink: tuple[float, float, float] = (20.0, 20.0, 20.0)
    noise: float = 0.0


_FONT_CACHE: dict[tuple[str, int, int], object] = {}


def _pil_font(face: FontFace, size: int):
    from PIL import ImageFont

    key = (face.path, face.index, size)
    f = _FONT_CACHE.get(key)
    if f is None:
        if len(_FONT_CACHE) > 64:
            _FONT_CACHE.clear()
        f = ImageFont.truetype(face.path, size=size, index=face.index)
        _FONT_CACHE[key] = f
    return f


def chars_of(fonts: FontIndex, edit: Edit, text: str, serif: bool | None) -> list[_Char]:
    out: list[_Char] = []
    for seg in fonts.resolve(edit.style, text, serif).segments:
        out.extend(_Char(c, seg.face, seg.synth_bold, seg.synth_italic) for c in seg.text)
    return out


def pen_positions(chars: list[_Char], px: float, tracking: float = 0.0) -> list[float]:
    """Pen x before each character and after the last, px."""
    size = max(4, int(round(px * SS)))
    pos = [0.0]
    for c in chars:
        pos.append(pos[-1] + _pil_font(c.face, size).getlength(c.ch) / SS * (px * SS / size) + tracking)
    return pos


def render(chars: list[_Char], px: float, tracking: float = 0.0) -> Strip:
    import cv2
    from PIL import Image, ImageDraw

    size = max(4, int(round(px * SS)))
    fix = px * SS / size
    pos = pen_positions(chars, px, tracking)
    pad = int(px * 0.8) + 4
    w = (int(np.ceil(pos[-1])) + 2 * pad) * SS
    h = int(np.ceil(px * 2.6)) * SS
    base = int(round(px * 1.7)) * SS
    img = Image.new("L", (w, h), 0)
    plain = ImageDraw.Draw(img)
    bold_img = italic = None
    for c, x in zip(chars, pos):
        if c.ch.isspace():
            continue
        target = plain
        if c.synth_bold:
            if bold_img is None:
                bold_img = Image.new("L", (w, h), 0)
            target = ImageDraw.Draw(bold_img)
        target.text(((pad + x) * SS / fix, base), c.ch, fill=255, font=_pil_font(c.face, size), anchor="ls")
        italic = italic or c.synth_italic
    a = np.asarray(img).astype(np.float32) / 255.0
    if bold_img is not None:
        k = max(1, int(round(size * 0.035)))
        b = cv2.dilate(np.asarray(bold_img), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1)))
        a = np.maximum(a, b.astype(np.float32) / 255.0)
    if italic:
        sh = float(np.tan(np.radians(12)))
        m = np.float32([[1, -sh, sh * base], [0, 1, 0]])
        a = cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_LINEAR)
    cov = cv2.resize(a, (w // SS, h // SS), interpolation=cv2.INTER_AREA)
    ys, xs = np.nonzero(cov > 0.25)
    ink = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None
    return Strip(cov, float(pad), base // SS, ink)


# --------------------------------------------------------------------------------------
# the look of the scan
# --------------------------------------------------------------------------------------
def apply_look(cov: np.ndarray, look: Look) -> np.ndarray:
    import cv2

    a = cv2.GaussianBlur(cov, (0, 0), look.sigma) if look.sigma > 0.05 else cov
    tau = max(look.tau, 0.5 * look.width)
    return np.clip((a - tau) / look.width + 0.5, 0.0, 1.0)


def _signature(d: np.ndarray) -> tuple[float, np.ndarray]:
    """Mean darkness of the ink box and the histogram of its darkness values."""
    ys, xs = np.nonzero(d > 0.3)
    if len(xs) < 20:
        return 0.0, np.zeros(8, np.float32)
    box = d[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    v = box[box > 0.08]
    hist, _ = np.histogram(v, bins=8, range=(0.08, 1.0001))
    return float(box.mean()), hist.astype(np.float32) / max(1, v.size)


def fit_look(scan_dark: np.ndarray, cov: np.ndarray) -> Look:
    """Blur and stroke weight that make ``cov`` (the line's own text, rendered clean)
    look like ``scan_dark`` (the same line in the scan). Compared by statistics, not
    pixel by pixel, so it does not depend on the two being aligned."""
    import cv2

    mass_s, hist_s = _signature(scan_dark)
    best = Look()
    if mass_s <= 0:
        return best
    best_loss = 1e9
    for sigma in (0.3, 0.55, 0.8, 1.1, 1.4, 1.8, 2.3):
        a = cv2.GaussianBlur(cov, (0, 0), sigma)
        for width in (0.25, 0.45, 0.7, 1.0, 1.4):
            for tau in (0.2, 0.26, 0.32, 0.38, 0.44, 0.5, 0.56, 0.62, 0.7):
                if tau < 0.5 * width:
                    continue  # blank paper must stay blank: no tint where there is no glyph
                d = np.clip((a - tau) / width + 0.5, 0.0, 1.0)
                mass, hist = _signature(d)
                if mass <= 0:
                    continue
                loss = 2.0 * abs(float(np.log(mass / mass_s))) + float(np.abs(hist - hist_s).sum())
                if loss < best_loss:
                    best_loss, best = loss, Look(sigma, tau, width)
    return best


# --------------------------------------------------------------------------------------
# finding the untouched text in the scan
# --------------------------------------------------------------------------------------
def _locate(dark: np.ndarray, strip: Strip, ex: float, ey: float, dx: float, dy: float):
    """Where ``strip``'s ink sits in ``dark`` near the expected top-left (ex, ey).
    Returns (x, y, score) of the ink box's top-left, or None."""
    import cv2

    if strip.ink is None:
        return None
    x0, y0, x1, y1 = strip.ink
    tmpl = cv2.GaussianBlur(strip.cov[y0:y1, x0:x1], (0, 0), 0.8)
    th, tw = tmpl.shape
    H, W = dark.shape
    wx0, wy0 = int(max(0, np.floor(ex - dx))), int(max(0, np.floor(ey - dy)))
    wx1, wy1 = int(min(W, np.ceil(ex + tw + dx))), int(min(H, np.ceil(ey + th + dy)))
    win = dark[wy0:wy1, wx0:wx1]
    if win.shape[0] < th or win.shape[1] < tw or th < 4 or tw < 4:
        return None
    res = cv2.matchTemplate(win.astype(np.float32), tmpl.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    # prefer the expected place among near-equal peaks (repeated characters)
    yy, xx = np.mgrid[0 : res.shape[0], 0 : res.shape[1]]
    far = np.hypot((xx + wx0 - ex) / max(dx, 1.0), (yy + wy0 - ey) / max(dy, 1.0))
    _mn, mx, _mnl, loc = cv2.minMaxLoc(res - 0.06 * far.astype(np.float32))
    score = float(res[loc[1], loc[0]])
    return wx0 + loc[0], wy0 + loc[1], score


def _common(a: str, b: str) -> tuple[int, int]:
    """Lengths of the common start and end of ``a`` and ``b`` (not overlapping)."""
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1
    s = 0
    while s < n - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    return p, s


def _anchor_span(text: str, lo: int, hi: int, from_end: bool) -> tuple[int, int] | None:
    """Up to ANCHOR_GLYPHS visible characters of text[lo:hi] next to the change.

    The anchor stops where full-width text meets Latin letters or digits: OCR often drops
    the thin space printed there, and a template drawn without it would not fit the scan.
    """
    idx = [i for i in range(lo, hi) if not text[i].isspace()]
    if not idx:
        return None
    order = idx[::-1] if from_end else idx
    wide = unicodedata.east_asian_width(text[order[0]]) in "WF"
    pick = []
    for i in order:
        if (unicodedata.east_asian_width(text[i]) in "WF") != wide or len(pick) >= ANCHOR_GLYPHS:
            break
        pick.append(i)
    return min(pick), max(pick) + 1


@dataclass
class Splice:
    image: object  # PIL image
    rect: Rect  # where it goes, display pt
    kept: int  # characters whose original pixels were kept
    shift: float  # how far the tail moved, pt
    overflow: bool = False


def splice_line(
    page_rgb: np.ndarray,
    scale: float,
    edit: Edit,
    fonts: FontIndex,
    serif: bool | None,
    inpaint: Callable[[Rect], tuple[object, Rect | None]],
    right_limit: float,
) -> Splice | None:
    """The patch that turns ``edit.original_text`` into ``edit.text`` on a scanned line.

    ``inpaint(rect)`` returns the paper under ``rect`` with its ink removed, and the exact
    rect it covers. ``right_limit`` is how far right the line may reach, display pt.
    Returns None when the line cannot be read well enough; the caller then redraws it.
    """
    from .analysis.imaging import estimate_background

    old, new = edit.original_text or "", edit.text
    b = edit.original_bounds
    H, W = page_rgb.shape[:2]
    bx0, by0, bx1, by1 = b.x * scale, b.y * scale, b.right * scale, b.bottom * scale
    lh = by1 - by0
    if lh < 6 or bx1 - bx0 < 6 or not old.strip():
        return None

    c_old = chars_of(fonts, edit, old, serif)
    c_new = chars_of(fonts, edit, new, serif)
    if len(c_old) != len(old) or len(c_new) != len(new):
        return None

    # darkness of the strip of page that holds the line
    sy0, sy1 = int(max(0, by0 - 0.9 * lh)), int(min(H, by1 + 0.9 * lh))
    strip_rgb = page_rgb[sy0:sy1].astype(np.float32)
    lx0, lx1 = int(max(0, bx0 - 2)), int(min(W, bx1 + 2))
    ly0, ly1 = int(max(0, by0 - 2)) - sy0, int(min(H, by1 + 2)) - sy0
    line_rgb = strip_rgb[ly0:ly1, lx0:lx1]
    bg, _spread = estimate_background(np.ascontiguousarray(page_rgb[sy0:sy1, lx0:lx1]))
    bg_l = float(np.mean(bg))
    lum_line = line_rgb.mean(axis=2)
    ink_l = float(np.percentile(lum_line, 2))
    if bg_l - ink_l < 40:
        return None
    dark = np.clip((bg_l - strip_rgb.mean(axis=2)) / (bg_l - ink_l), 0.0, 1.0)
    line_dark = dark[ly0:ly1, lx0:lx1]

    # Size of the type as scanned. The rendered line's ink height against the scan's gives
    # a first value; a few sizes around it are then tried on the anchors — the unchanged
    # characters next to the change — and the size at which they fit the scan best wins.
    px0 = edit.style.size * scale
    first = render(c_old, px0)
    if first.ink is None:
        return None
    px0 *= float(np.clip(lh / max(1, first.ink[3] - first.ink[1]), 0.88, 1.12))

    p, s = _common(old, new)
    tail_span = _anchor_span(old, 0, p, True)
    head_span = _anchor_span(old, len(old) - s, len(old), False)

    def measure(px: float):
        whole = render(c_old, px)
        if whole.ink is None:
            return None
        pos_old = pen_positions(c_old, px)
        stretch = (bx1 - bx0) / max(1.0, whole.ink[2] - whole.ink[0])
        if not 0.6 <= stretch <= 1.7:
            stretch = 1.0
        base_row = by1 - sy0 - (whole.ink[3] - whole.base)  # in strip rows

        def find(span):
            if span is None:
                return None, 0.0
            st = render(c_old[span[0] : span[1]], px)
            if st.ink is None:
                return None, 0.0
            ex = bx0 + (pos_old[span[0]] + st.ink_left - whole.ink_left) * stretch
            ey = base_row - (st.base - st.ink[1])
            hit = _locate(dark, st, ex, ey, 1.1 * px, 0.3 * px)
            if hit is None:
                return None, 0.0
            x, y, score = hit
            n = sum(1 for c in c_old[span[0] : span[1]] if not c.ch.isspace())
            if score < (MIN_ANCHOR_SCORE if n >= 3 else 0.6):
                return None, score
            return (st, x, y + (st.base - st.ink[1])), score  # strip, ink left col, baseline row

        tail, s1 = find(tail_span)
        head, s2 = find(head_span)
        return whole, pos_old, base_row, tail, head, s1 + s2, stretch

    best = None
    for f in ((1.0, 0.97, 1.03, 0.94, 1.06) if (tail_span or head_span) else (1.0,)):
        m = measure(px0 * f)
        if m is not None and (best is None or m[5] > best[1][5] + 0.01):
            best = (px0 * f, m)
    if best is None:
        return None
    px, (whole, pos_old, base_row, tail, head, _score, stretch_of_line) = best
    pos_new = pen_positions(c_new, px)

    look = fit_look(line_dark, whole.cov)
    sel = line_dark > 0.85
    if sel.sum() >= 8:
        look.ink = tuple(float(np.median(line_rgb[..., i][sel])) for i in range(3))
    else:
        look.ink = tuple(float(v) for v in edit.style.color)
    paper = dark < 0.04
    look.noise = float(np.std(strip_rgb.mean(axis=2)[paper])) if paper.sum() > 200 else 0.0

    if tail is None:
        p, tail_span = 0, None
    if head is None:
        s, head_span = 0, None
    off = len(new) - len(old)  # index of a tail character in ``new`` minus in ``old``

    rows0, rows1 = int(max(0, by0 - sy0 - 0.15 * lh)), int(min(dark.shape[0], by1 - sy0 + 0.15 * lh))
    col_ink = dark[rows0:rows1].max(axis=0)
    reach = int(0.3 * px)

    def clear_col(start: int, step: int) -> int:
        x = start
        for _ in range(reach):
            if 0 <= x < W and col_ink[x] < 0.2:
                return x
            x += step
        return start

    pad = int(0.2 * px) + 2
    bases = []
    if tail is not None:
        ts, tx, tb = tail
        xe = tx + (ts.ink[2] - ts.ink[0])  # column just past the kept start
        cut_l = clear_col(int(xe), 1)
        i0, i1 = tail_span
        mid_from = i1  # first character to draw, same index in old and new
        pen_tail = xe - (pos_old[i0] + ts.ink_right)  # pen origin of the line at its start
        bases.append(tb)
    else:
        xe = None
        cut_l = int(max(0, bx0 - pad))
        mid_from = 0
    if head is not None:
        hs, hx, hb = head
        cut_r = clear_col(int(hx) - 1, -1) + 1
        j0 = head_span[0]
        bases.append(hb)
    else:
        hx = None
        cut_r = int(min(W, bx1 + pad))
        j0 = len(old)
    cut_r = max(cut_r, cut_l)
    base = int(round(float(np.mean(bases)))) if bases else int(round(base_row))

    mid_chars = c_new[mid_from : j0 + off]
    old_mid_chars = c_old[mid_from:j0]
    mid = render(mid_chars, px) if any(not c.ch.isspace() for c in mid_chars) else None
    if mid is not None and mid.ink is None:
        mid = None
    old_mid = render(old_mid_chars, px) if any(not c.ch.isspace() for c in old_mid_chars) else None
    if old_mid is not None and old_mid.ink is None:
        old_mid = None
    mid_adv = pen_positions(mid_chars, px)[-1]
    old_adv = pos_old[j0] - pos_old[mid_from]

    # Where the replaced wording's ink starts and ends in the scan. The gap the scan
    # leaves on each side beyond what the font gives (a space OCR dropped, justification)
    # is kept, unless the new wording brings that space itself.
    m_l = m_r = None
    lo, hi = cut_l, cut_r
    cols = np.nonzero(col_ink[max(0, lo) : max(lo, hi)] > 0.3)[0]
    if old_mid is not None and len(cols):
        m_l, m_r = max(0, lo) + int(cols[0]), max(0, lo) + int(cols[-1]) + 1

    # Letter spacing of the scan (justified lines are set wider than the font alone):
    # measured on the replaced wording itself when it is long enough, else on the whole
    # line when that is all full-width characters (spaces OCR dropped around Latin words
    # and digits would otherwise pass for letter spacing).
    track = 0.0
    n_old_mid = sum(1 for c in old_mid_chars if not c.ch.isspace())
    vis = [c for c in old if not c.isspace()]
    if old_mid is not None and m_l is not None and n_old_mid >= 3 and all(unicodedata.east_asian_width(c.ch) in "WF" for c in old_mid_chars):
        t = ((m_r - m_l) - (old_mid.ink[2] - old_mid.ink[0])) / (n_old_mid - 1)
    elif len(vis) >= 8 and all(unicodedata.east_asian_width(c) in "WF" for c in vis):
        t = ((bx1 - bx0) - (whole.ink[2] - whole.ink[0])) / (len(vis) - 1)
    else:
        t = 0.0
    if abs(t) >= 0.02 * px:
        track = float(np.clip(t, -0.03 * px, 0.25 * px))
        mid = render(mid_chars, px, track) if mid is not None else None
        mid_adv = pen_positions(mid_chars, px, track)[-1]

    def narrow(c: _Char | None) -> bool:
        return c is not None and not c.ch.isspace() and unicodedata.east_asian_width(c.ch) not in "WF"

    def keep_gap(scan_gap: float | None, nat_old: float, nat_new: float, kept: _Char, was: _Char | None, now: _Char | None) -> float:
        """Gap between a kept character and the new wording next to it. ``was``/``now``:
        the replaced and the new character on that side."""
        hidden = max(0.0, scan_gap - nat_old) if scan_gap is not None else 0.0
        mixed_was = was is not None and narrow(kept) != narrow(was)
        mixed_now = now is not None and narrow(kept) != narrow(now)
        if mixed_was and not mixed_now:
            hidden = 0.0  # the thin space Word puts between Chinese and Latin goes with the Latin
        elif mixed_now and not mixed_was and scan_gap is not None:
            hidden = max(hidden, auto_space)  # ...and comes with it
        return nat_new + max(0.0, hidden - max(0.0, nat_new - nat_old))

    vis_old = [c for c in old_mid_chars if not c.ch.isspace()]
    vis_new = [c for c in mid_chars if not c.ch.isspace()]
    # does this line space Chinese from Latin at all? (Word does by default; plain text does not)
    auto_space = 0.0
    for i in range(1, len(c_old)):
        a_, b_ = c_old[i - 1], c_old[i]
        if not a_.ch.isspace() and not b_.ch.isspace() and narrow(a_) != narrow(b_) and (a_.ch.isalnum() and b_.ch.isalnum()):
            auto_space = 0.25 * px if stretch_of_line > 1.01 else 0.0
            break

    # left side: from the kept start to the new characters
    if tail is not None:
        tail_end_pen = pos_old[i0] + ts.ink_right  # in line pen coordinates
        if mid is not None:
            nat_new = pos_old[i1] + mid.ink_left - tail_end_pen
            if old_mid is not None and m_l is not None:
                nat_old = pos_old[i1] + old_mid.ink_left - tail_end_pen
                gap = keep_gap(m_l - xe, nat_old, nat_new, c_old[i1 - 1], vis_old[0], vis_new[0])
            elif old_mid is None and head is not None:  # pure insertion
                nat_old = pos_old[j0] + hs.ink_left - tail_end_pen
                gap = keep_gap(hx - xe, nat_old, nat_new, c_old[i1 - 1], None, vis_new[0])
            else:
                gap = nat_new
            mid_pen = xe + gap - mid.ink_left
        else:
            mid_pen = xe - tail_end_pen + pos_old[i1]
    elif mid is not None:
        mid_pen = bx0 - mid.ink_left
    else:
        mid_pen = bx0

    # right side: from the new characters to the kept end
    shift = 0
    if head is not None:
        if mid is not None:
            nat_new = mid_adv - mid.ink_right + hs.ink_left
            if old_mid is not None and m_r is not None:
                nat_old = old_adv - old_mid.ink_right + hs.ink_left
                gap = keep_gap(hx - m_r, nat_old, nat_new, c_old[j0], vis_old[-1], vis_new[-1])
            else:
                gap = nat_new
            new_hx = mid_pen + mid.ink_right + gap
        elif tail is not None:  # pure deletion: the two kept parts close up
            nat_new = pos_new[j0 + off] + hs.ink_left - tail_end_pen
            scan_gaps = [g for g in ((m_l - xe) if m_l is not None else None, (hx - m_r) if m_r is not None else None) if g is not None]
            new_hx = xe + (max(nat_new, min(max(scan_gaps), nat_new + 0.6 * px)) if scan_gaps else nat_new)
        else:
            new_hx = bx0  # everything before the kept end was deleted
        shift = int(round(new_hx - hx))
        if abs(shift) <= 1:
            shift = 0
    line_end = int(min(W, bx1 + pad))
    new_end = line_end + shift if head is not None else int(mid_pen + (mid.ink_right if mid is not None else 0) + pad)
    overflow = new_end / scale > right_limit + 0.5

    # the patch: clean paper, the moved tail, the new characters
    c0 = cut_l
    c1 = cut_r if (head is not None and shift == 0) else max(line_end, new_end)
    c1 = int(min(W, max(c1, cut_r)))
    r0, r1 = int(max(0, by0 - 0.3 * lh)), int(min(H, by1 + 0.3 * lh))
    if c1 - c0 < 2:
        return None
    img, exact = inpaint(Rect(c0 / scale, r0 / scale, (c1 - c0) / scale, (r1 - r0) / scale))
    if img is None:
        return None
    out = np.asarray(img).astype(np.float32)
    ox, oy = int(round(exact.x * scale)), int(round(exact.y * scale))
    ph, pw = out.shape[:2]

    def blend(alpha: np.ndarray, colour, x: int, y: int) -> None:
        """Lays ``colour`` (an image or one colour) over the patch at page pixel (x, y)."""
        h, w = alpha.shape
        dx0, dy0 = x - ox, y - oy
        ax0, ay0 = max(0, -dx0), max(0, -dy0)
        ax1, ay1 = min(w, pw - dx0), min(h, ph - dy0)
        if ax1 <= ax0 or ay1 <= ay0:
            return
        a = alpha[ay0:ay1, ax0:ax1, None]
        dst = out[dy0 + ay0 : dy0 + ay1, dx0 + ax0 : dx0 + ax1]
        src = colour[ay0:ay1, ax0:ax1] if isinstance(colour, np.ndarray) and colour.ndim == 3 else np.asarray(colour, np.float32)
        dst[:] = dst * (1 - a) + src * a

    if head is not None and shift != 0:
        src = strip_rgb[r0 - sy0 : r1 - sy0, cut_r:line_end]
        d = dark[r0 - sy0 : r1 - sy0, cut_r:line_end]
        alpha = np.clip((d - 0.05) / 0.15, 0.0, 1.0)
        blend(alpha, src, cut_r + shift, r0)

    if mid is not None:
        d = apply_look(mid.cov, look)
        colour = np.empty(d.shape + (3,), np.float32)
        colour[:] = look.ink
        if look.noise > 0:
            rng = np.random.default_rng(edit.id * 7919 + len(new))
            colour += rng.normal(0, look.noise, d.shape)[..., None].astype(np.float32)
        blend(d, colour, int(round(mid_pen - mid.x0)), sy0 + base - mid.base)

    from PIL import Image

    kept = (p if tail is not None else 0) + (s if head is not None else 0)
    return Splice(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), exact, kept, shift / scale, overflow)
