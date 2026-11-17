"""Guessing which installed font a scanned line was set in.

A scan carries no font names, but the letters themselves are evidence. For each
installed candidate family the line's own OCR text is rendered, scaled onto the scanned
ink box and compared with the scanned ink (correlation of slightly blurred masks), and
the candidate's natural width-to-height ratio is compared with the scan's. The family
with the best combined score wins if it beats the runner-up clearly enough; otherwise
the block keeps ``family=None`` and the resolver falls back to the defaults.

Shape correlation separates 黑體/明體/楷體 and sans/serif Latin; the aspect term
separates wide faces (Verdana, DejaVu) from narrow ones (Arial, Calibri), which a
shape-only score after rescaling cannot see.

Once a family is chosen the size can be re-derived from the ink height: the font's
own ink extent for this text, in ems, divided into the measured height in points.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fonts import CJK_SANS, CJK_SERIF, FontIndex, is_cjk

LATIN_CANDIDATES = [
    "Calibri", "Arial", "Times New Roman", "Cambria", "Verdana", "Tahoma", "Segoe UI", "Georgia",
    "Century Gothic", "Garamond", "Book Antiqua", "Courier New", "Consolas",
    "Liberation Sans", "Liberation Serif", "Liberation Mono", "DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono",
]
CJK_CANDIDATES = CJK_SANS + CJK_SERIF + ["DFKai-SB", "BiauKai", "KaiTi"]

RENDER_H = 48  # px, height the comparison is done at
ASPECT_WEIGHT = 2.5
MIN_MARGIN = 0.015  # best must beat runner-up by this much


@dataclass
class FontGuess:
    family: str
    score: float
    margin: float
    size: float | None  # refined size in pt, or None
    ranking: list[tuple[str, float]]


def _ink_bbox(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def _blur(a: np.ndarray, k: int = 3) -> np.ndarray:
    import cv2

    return cv2.GaussianBlur(a.astype(np.float32), (2 * k + 1, 2 * k + 1), 0)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 0 else 0.0


def _render(face, text: str, px: int, synth_bold: bool = False):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(face.path, size=px, index=face.index)
    except Exception:
        return None
    w = int(px * (len(text) + 2) * 1.2) + 20
    img = Image.new("L", (w, int(px * 2.2)), 0)
    ImageDraw.Draw(img).text((10, int(px * 0.5)), text, fill=255, font=font)
    mask = np.asarray(img) > 100
    if synth_bold:
        # A family without a bold face (標楷體) is emboldened by stroking, as the writer
        # does; without this a bold line always "matches" whichever family ships a real
        # bold file, whatever its design.
        import cv2

        k = max(1, int(round(px * 0.035)))
        mask = cv2.dilate(mask.astype(np.uint8), np.ones((2 * k + 1, 2 * k + 1), np.uint8)) > 0
    return mask


@dataclass
class FontScores:
    """How well each candidate family fits one scanned line."""

    weight: float  # number of glyphs judged: longer lines say more
    ink_h: float  # height of the scanned ink, pt
    scores: dict[str, float]
    em: dict[str, float]  # the family's ink height for this text, in ems

    def ranking(self) -> list[tuple[str, float]]:
        return sorted(self.scores.items(), key=lambda t: -t[1])


def score_fonts(
    ink: np.ndarray,
    text: str,
    bold: bool,
    italic: bool,
    fonts: FontIndex,
    scale: float,
    candidates: list[str] | None = None,
    min_glyphs: int = 2,
) -> FontScores | None:
    """Scores every installed candidate family against the scanned line ``ink``.

    ``scale`` is pixels per point of ``ink``. A line with any CJK is judged against CJK
    families (they draw its Latin letters too, as in a 標楷體 document).
    """
    import cv2

    text = text.strip()
    glyphs = [c for c in text if not c.isspace()]
    if len(glyphs) < min_glyphs:
        return None
    cjk = sum(is_cjk(c) for c in glyphs)
    bb = _ink_bbox(ink)
    if bb is None:
        return None
    x0, y0, x1, y1 = bb
    scan = ink[y0:y1, x0:x1]
    sh, sw = scan.shape
    if sh < 6 or sw < 12:
        return None
    scan_aspect = sw / sh
    tw = max(8, int(round(RENDER_H * scan_aspect)))
    scan_n = cv2.resize(scan.astype(np.uint8) * 255, (tw, RENDER_H), interpolation=cv2.INTER_AREA) > 100
    scan_b = _blur(scan_n)

    pool = candidates or (CJK_CANDIDATES if cjk * 2 >= len(glyphs) else LATIN_CANDIDATES)
    seen: set[str] = set()
    scores: dict[str, float] = {}
    em: dict[str, float] = {}
    for fam in pool:
        face = fonts.pick_face(fam, bold, italic)
        if face is None or face.id in seen:
            continue
        seen.add(face.id)
        if sum(fonts.covers(face, c) for c in glyphs) < 0.95 * len(glyphs):
            continue
        px = 64
        r = _render(face, text, px, synth_bold=bold and not face.bold)
        if r is None:
            continue
        rb = _ink_bbox(r)
        if rb is None:
            continue
        rx0, ry0, rx1, ry1 = rb
        cand = r[ry0:ry1, rx0:rx1]
        ch, cw = cand.shape
        cand_aspect = cw / ch
        cand_n = cv2.resize(cand.astype(np.uint8) * 255, (tw, RENDER_H), interpolation=cv2.INTER_AREA) > 100
        shape = _corr(scan_b, _blur(cand_n))
        aspect_err = abs(np.log(cand_aspect / scan_aspect))
        scores[face.family] = float(shape - ASPECT_WEIGHT * aspect_err)
        em[face.family] = ch / px
    if not scores:
        return None
    # A Chinese line votes with its Chinese characters only: the Latin words in it may be
    # set in a different face, and would otherwise pull the choice towards whichever
    # Chinese font happens to have similar letters.
    weight = float(cjk) if cjk >= 2 else float(len(glyphs))
    return FontScores(weight, sh / scale, scores, em)


def score_latin(
    ink: np.ndarray,
    text: str,
    bold: bool,
    italic: bool,
    fonts: FontIndex,
    scale: float,
    cjk_family: str,
    pool: list[str],
) -> FontScores | None:
    """Scores Latin companion fonts for a line whose Chinese is set in ``cjk_family``.

    Each candidate draws the letters and digits while ``cjk_family`` draws the rest, the
    way Office splits a run; ``cjk_family`` itself is a candidate (its own letters, as in
    a document typed entirely in 標楷體). The weight is the number of letters and digits.
    """
    import cv2

    from .fonts import is_ambiguous
    from .scanlook import _Char, render

    text = text.strip()
    latin = sum(1 for c in text if c.isascii() and c.isalnum())
    if latin < 3:
        return None
    bb = _ink_bbox(ink)
    if bb is None:
        return None
    x0, y0, x1, y1 = bb
    scan = ink[y0:y1, x0:x1]
    sh, sw = scan.shape
    if sh < 6 or sw < 12:
        return None
    scan_aspect = sw / sh
    tw = max(8, int(round(RENDER_H * scan_aspect)))
    scan_b = _blur(cv2.resize(scan.astype(np.uint8) * 255, (tw, RENDER_H), interpolation=cv2.INTER_AREA) > 100)
    east_face = fonts.pick_face(cjk_family, bold, italic)
    if east_face is None:
        return None
    scores: dict[str, float] = {}
    em: dict[str, float] = {}
    px = 48.0
    for fam in dict.fromkeys([cjk_family] + pool):
        face = fonts.pick_face(fam, bold, italic)
        if face is None:
            continue
        chars = []
        for ch in text:
            east = is_cjk(ch) or is_ambiguous(ch) or not fonts.covers(face, ch)
            f = east_face if east else face
            chars.append(_Char(ch, f, bold and not f.bold, italic and not f.italic))
        try:
            strip = render(chars, px)
        except Exception:
            continue
        if strip.ink is None:
            continue
        rx0, ry0, rx1, ry1 = strip.ink
        cand = strip.cov[ry0:ry1, rx0:rx1] > 0.4
        ch_, cw = cand.shape
        cand_n = cv2.resize(cand.astype(np.uint8) * 255, (tw, RENDER_H), interpolation=cv2.INTER_AREA) > 100
        shape = _corr(scan_b, _blur(cand_n))
        scores[face.family] = float(shape - ASPECT_WEIGHT * abs(np.log((cw / ch_) / scan_aspect)))
        em[face.family] = ch_ / px
    if not scores:
        return None
    return FontScores(float(latin), sh / scale, scores, em)


def match_font(
    ink: np.ndarray,
    text: str,
    bold: bool,
    italic: bool,
    fonts: FontIndex,
    scale: float,
    candidates: list[str] | None = None,
) -> FontGuess | None:
    """Best family for one line on its own. Returns None when the text is too short to
    judge or no candidate is installed."""
    glyphs = [c for c in text.strip() if not c.isspace()]
    cjk = sum(is_cjk(c) for c in glyphs)
    if len(glyphs) < (4 if cjk * 2 >= len(glyphs) else 6):
        return None
    fs = score_fonts(ink, text, bold, italic, fonts, scale, candidates)
    if fs is None:
        return None
    rank = fs.ranking()
    best = rank[0]
    margin = best[1] - rank[1][1] if len(rank) > 1 else 1.0
    e = fs.em[best[0]]
    size = fs.ink_h / e if e > 0 else None
    return FontGuess(best[0], best[1], margin, size, [(f, round(s, 4)) for f, s in rank])


PRIOR_WEIGHT = 80.0  # glyphs: how much the whole page counts next to one group of lines


def _mean_scores(items: list[FontScores]) -> dict[str, float]:
    """Glyph-weighted mean score per family, over the lines where the family could be judged.

    A family that could draw less than half of the text is left out.
    """
    total = sum(i.weight for i in items)
    acc: dict[str, list[float]] = {}
    for i in items:
        for fam, s in i.scores.items():
            a = acc.setdefault(fam, [0.0, 0.0])
            a[0] += s * i.weight
            a[1] += i.weight
    return {f: a[0] / a[1] for f, a in acc.items() if a[1] >= 0.5 * total}


def settle_families(groups: dict[object, list[FontScores]], prior: float = PRIOR_WEIGHT) -> dict[object, str]:
    """One family per group of lines, decided by all of the group's lines together.

    Judged alone, the lines of a single paragraph scatter over look-alike families, and
    the paragraph comes out as a patchwork. Here each group (lines of one script, weight
    and size) pools its evidence, and is pulled towards what the whole page says by
    ``prior`` glyphs' worth — so a two-line group needs strong evidence to differ from
    the page, while a long passage in another typeface still wins its own family.
    """
    everything = [i for items in groups.values() for i in items]
    if not everything:
        return {}
    page = _mean_scores(everything)
    out: dict[object, str] = {}
    for key, items in groups.items():
        if not items:
            continue
        own = _mean_scores(items)
        w = sum(i.weight for i in items)
        cands = set(own) | set(page)
        best, best_s = None, -1e9
        for fam in sorted(cands):
            if fam in own and fam in page:
                s = (w * own[fam] + prior * page[fam]) / (w + prior)
            elif fam in own:
                s = own[fam]
            else:
                continue  # cannot draw this group's text
            if s > best_s:
                best, best_s = fam, s
        if best is not None:
            out[key] = best
    return out
