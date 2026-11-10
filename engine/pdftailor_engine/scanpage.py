"""Turning a scanned (pixel-only) page into editable blocks.

Pipeline, per page:

1. the OCR engine finds and reads lines; a line that runs across a ruled table's cell
   borders is cut at them, by the position of each word the engine reported;
2. each line is measured from its pixels (em, baseline, ink colour, shapes);
3. a line is cut apart at geometric symbols (■ □ ● …) and at wide gaps (table cells) — the
   symbols are classified by shape, the pieces between them are read again on their own,
   so a symbol can no longer corrupt the text beside it (Tesseract read "● 項目三" as
   "@WA=");
4. weight and slant are judged against the rest of the page.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from .analysis.imaging import Component, find_components, ink_mask
from .analysis.line import LineScan, scan_line
from .analysis.style import StyleEstimate, estimate_styles, regularize_sizes
from .analysis.symbols import SymbolMatch
from .geometry import Rect
from .models import Block, Run, TextStyle
from .ocr.base import OcrEngine, OcrLine, OcrWord
from .ocr.tesseract import join_chars
from .tables import Grid, erase_rules, find_grids

_BLOCK_GAP_EM = 2.2  # a gap this wide starts a new block (table cells, columns)
_SPACE_EM = 0.15  # a gap this wide between pieces reads as a space

# What an OCR engine tends to make of the shapes we detect by geometry. When a symbol was
# found at a position and the text there is one of these, the engine's guess is replaced.
CONFUSABLE = set("圖回口囗O0oOo[]()｛｝{}@Q日目田□■◻◼▪▫●○◯〇◎•・.,:;|!lI1/\\-_=+<>^~`'\"")


@dataclass
class _Item:
    kind: str  # "sym" | "txt"
    comps: list[Component]
    match: SymbolMatch | None = None
    text: str = ""
    conf: float = -1.0
    keep: bool = False  # the page-pass reading stands; do not read this piece again

    @property
    def x0(self) -> int:
        return min(c.x for c in self.comps)

    @property
    def x1(self) -> int:
        return max(c.right for c in self.comps)

    @property
    def y0(self) -> int:
        return min(c.y for c in self.comps)

    @property
    def y1(self) -> int:
        return max(c.bottom for c in self.comps)


# Characters an engine makes of a table's vertical rule, or of a cell edge it touches.
_RULE_CHARS = set("|¦│┃｜[]_")


def _vertical_rules(grids: list[Grid], scale: float) -> list[tuple[float, float, float]]:
    """The vertical rules actually drawn in each table row, as (x, y0, y1) in page pixels.

    Built from the separators, not from cells: where a rule is missing (a merged cell, or
    a header band whose one rule only runs down part of it) nothing is cut.
    """
    out = []
    for g in grids:
        for r in range(g.rows):
            for c in range(g.cols + 1):
                if c == 0 or c == g.cols or g.vsep[r][c - 1]:
                    out.append((g.xs[c] * scale, g.ys[r] * scale, g.ys[r + 1] * scale))
    return out


def _char_boxes(lines: list[OcrLine]) -> list[tuple[float, float, float, float]]:
    """Boxes of the characters the engine read, for telling letter strokes from rules.
    Rule characters are left out (they *are* the rules), and so are boxes far wider than
    tall, which the engine sometimes stretches across a gap."""
    out = []
    for l in lines:
        for w in l.words:
            t = w.text.strip()
            if not t or set(t) <= _RULE_CHARS or w.w > 1.6 * max(w.h, 1) * max(1, len(t)):
                continue
            out.append((w.x, w.y, w.x + w.w, w.y + w.h))
    return out


def _line_from_words(words: list[OcrWord], parent: OcrLine) -> OcrLine:
    x0 = min(w.x for w in words)
    x1 = max(w.x + w.w for w in words)
    y0 = min(w.y for w in words)
    y1 = max(w.y + w.h for w in words)
    confs = [w.confidence for w in words if w.confidence >= 0]
    return OcrLine(join_chars(words), x0, y0, x1 - x0, y1 - y0,
                   sum(confs) / len(confs) if confs else -1.0, list(words))


def _split_at_cells(lines: list[OcrLine], rules: list[tuple[float, float, float]]) -> list[OcrLine]:
    """Cuts lines that run across table rules; drops the rules the engine read as text.

    Page segmentation reads a table row as one line ("報到|110年11月15日|報到中國人壽…"),
    so an edit, and the Word table, would mix cells. A line is cut between two neighbouring
    characters wherever a drawn vertical rule passes between them; a "character" made only
    of rule marks (| [ ] _) is the border itself.
    """
    out: list[OcrLine] = []
    for l in lines:
        if not l.words:
            out.append(l)
            continue
        # reading order as the engine gave it: its character boxes overlap, so sorting them
        # by position would swap neighbours (身分證 → 身證分)
        words = [w for w in l.words if w.text.strip() and not set(w.text.strip()) <= _RULE_CHARS]
        if not words:
            continue
        cy = l.y + l.h / 2
        xs = sorted(x for x, y0, y1 in rules if y0 <= cy <= y1)
        groups: list[list[OcrWord]] = [[words[0]]]
        for a, b in zip(words, words[1:]):
            ca, cb = a.x + a.w / 2, b.x + b.w / 2
            if any(ca < x < cb for x in xs):
                groups.append([b])
            else:
                groups[-1].append(b)
        if len(groups) == 1 and len(words) == len(l.words):
            out.append(l)
        else:
            out.extend(_line_from_words(g, l) for g in groups)
    return out


def _assign_words(ol: OcrLine, blocks: list[list[_Item]], scan: LineScan, x0: float) -> None:
    """Gives every character the engine read in the page pass to the piece it overlaps most.

    A text piece takes those characters as its reading (it may still be read again on its
    own, see the caller). A symbol piece keeps its shape-detected character only where the
    engine saw nothing better there: when the engine read a confident, ordinary character
    at that spot, the "symbol" was the box-like part of a character (國, 回) and the
    character stays.
    """
    k = scan.scale
    items = [it for blk in blocks for it in blk]
    spans = [(x0 + it.x0 / k, x0 + (it.x1 + 1) / k) for it in items]
    got: list[list[OcrWord]] = [[] for _ in items]
    for w in ol.words:
        t = w.text.strip()
        if not t or set(t) <= _RULE_CHARS:
            continue
        best, best_ov = -1, 0.0
        for n, (a, b) in enumerate(spans):
            ov = min(b, w.x + w.w) - max(a, w.x)
            if items[n].kind == "sym":
                ov *= 1.5  # a text piece's box can reach over the symbol next to it
            if ov > best_ov:
                best, best_ov = n, ov
        if best < 0:  # between pieces: the nearest one
            cx = w.x + w.w / 2
            best = min(range(len(spans)), key=lambda n: min(abs(cx - spans[n][0]), abs(cx - spans[n][1])))
        got[best].append(w)
    for it, ws in zip(items, got):
        if it.kind == "sym":
            text = "".join(w.text.strip() for w in ws)
            if ws and not any(ch in CONFUSABLE for ch in text) and min(w.confidence for w in ws) >= 70:
                it.kind, it.match = "txt", None
                it.text = join_chars(ws)
                it.conf = sum(w.confidence for w in ws) / len(ws)
                it.keep = True
            continue
        if ws:
            it.text = join_chars(ws)
            confs = [w.confidence for w in ws if w.confidence >= 0]
            it.conf = sum(confs) / len(confs) if confs else -1.0


def _width_misfit(text: str, width_em: float) -> float:
    """How far a reading's expected width (CJK 1 em, others about half) is from the ink's."""
    est = sum(1.0 if _is_cjk(ch) else 0.55 for ch in text if not ch.isspace())
    return abs(est - width_em) / max(width_em, 0.5)


def _similar(a: str, b: str) -> float:
    """0..1, share of characters two readings have in common (order-aware)."""
    a = "".join(a.split())
    b = "".join(b.split())
    if not a or not b:
        return 0.0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return 2 * prev[-1] / (len(a) + len(b))


@dataclass
class ScanResult:
    blocks: list[Block]
    notes: list[str] = field(default_factory=list)


def _split_into_blocks(scan: LineScan) -> list[list[_Item]]:
    syms = {c.label: m for c, m in scan.symbols}
    blocks: list[list[_Item]] = []
    items: list[_Item] = []
    right = -1e9
    for c in scan.comps:  # sorted left to right
        if c.w >= 3 * scan.em:
            # no letter is 3 em wide: an underline or a rule (with whatever strokes touch it);
            # the text it carries is still text, so it does not open a gap either
            right = max(right, c.right)
            continue
        if items and c.x - right >= _BLOCK_GAP_EM * scan.em:
            blocks.append(items)
            items = []
        if c.label in syms:
            items.append(_Item("sym", [c], syms[c.label]))
        elif items and items[-1].kind == "txt":
            items[-1].comps.append(c)
        else:
            items.append(_Item("txt", [c]))
        right = max(right, c.right)
    if items:
        blocks.append(items)

    # specks of noise between symbols are not text
    cleaned: list[list[_Item]] = []
    for blk in blocks:
        kept = [
            it
            for it in blk
            if it.kind == "sym" or (it.x1 - it.x0) >= 0.25 * scan.em or sum(c.area for c in it.comps) >= 0.05 * scan.em**2
        ]
        if kept:
            cleaned.append(kept)
    return cleaned


def _text_crop(scan: LineScan, item: _Item) -> np.ndarray:
    """The pixels of one text piece, ready for the OCR engine.

    Cut from the line's own enlarged copy, with every *other* blob inside the window —
    the neighbouring ■ or 」 — painted over. Left in, a bracket's foot reads as a full stop
    ("引用文字」" came back as "引用文字.").
    """
    h, w = scan.mask.shape
    pad = int(0.2 * scan.em)
    cx0, cx1 = max(0, item.x0 - pad), min(w, item.x1 + pad + 1)
    cy0, cy1 = max(0, item.y0 - pad), min(h, item.y1 + pad + 1)
    crop = scan.rgb[cy0:cy1, cx0:cx1].copy()
    own = {c.label for c in item.comps}
    foreign = np.zeros(crop.shape[:2], dtype=bool)
    for c in scan.comps:
        if c.label in own or c.right < cx0 or c.x > cx1 or c.bottom < cy0 or c.y > cy1:
            continue
        sub = scan.labels[cy0:cy1, cx0:cx1] == c.label
        foreign |= sub
    if foreign.any():
        foreign = cv2.dilate(foreign.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        crop[foreign] = scan.bg
    return _prepare_crop(crop, scan.em, scan.bg)


def _prepare_crop(rgb: np.ndarray, em_px: float, bg) -> np.ndarray:
    """Brings text toward ~48 px em (small text enlarged, very large text reduced —
    Tesseract misreads both extremes) and frames it with background colour."""
    if em_px > 80:
        f = 48.0 / em_px
        rgb = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    else:
        f = 1 if em_px >= 40 else (2 if em_px >= 24 else 3)
        if f > 1:
            rgb = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    pad = 14
    return cv2.copyMakeBorder(rgb, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=[int(v) for v in bg])


_BULLET_KINDS = {"square", "circle", "triangle", "diamond"}


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF


def _spacing(prev: "_Item | None", cur: _Item, last: str, first: str, gap_em: float) -> str:
    """Whitespace to put between two pieces of a cut line, from the gap between them.

    A bracket glyph has empty space inside its em cell, so a gap next to 「」 is not a
    space; a bullet (■ □ ●) is followed by a real one; Latin words are separated by one;
    and a gap of nearly a full em is a typed full-width space.
    """
    if gap_em >= 0.9:
        return "\u3000" if (_is_cjk(last) and _is_cjk(first)) else " "
    after_bullet = prev is not None and prev.kind == "sym" and prev.match and prev.match.kind in _BULLET_KINDS
    if after_bullet and gap_em >= 0.3:
        return " "
    latin_side = (last.isascii() and last.isalnum()) or (first.isascii() and first.isalnum())
    if latin_side and not (prev is not None and prev.kind == "sym") and cur.kind != "sym" and gap_em >= _SPACE_EM:
        return " "
    if cur.kind == "sym" and cur.match and cur.match.kind in _BULLET_KINDS and gap_em >= 0.3:
        return " "
    return ""


def _estimate_merge(text: str, line_x0: float, line_x1: float, syms: list[tuple[float, SymbolMatch]]) -> tuple[str, list[int]]:
    """Fallback when an engine cannot re-read crops: put each detected symbol into the
    OCR text at a position estimated from where it sits along the line. Approximate, and
    flagged as such, but better than losing the symbol."""
    chars = list(text)
    inserted: list[int] = []
    span = max(line_x1 - line_x0, 1.0)
    for cx, m in sorted(syms, key=lambda s: s[0]):
        idx = int(round((cx - line_x0) / span * len(chars)))
        idx = max(0, min(idx, len(chars)))
        replaced = False
        for j in (idx, idx - 1, idx + 1):
            if 0 <= j < len(chars) and chars[j] in CONFUSABLE:
                chars[j] = m.char
                inserted.append(j)
                replaced = True
                break
        if not replaced:
            chars.insert(idx, m.char)
            inserted.append(idx)
    return "".join(chars), inserted


def _dedupe(lines: list[OcrLine]) -> list[OcrLine]:
    """Drops a line that mostly lies inside another, keeping the more confident one."""
    keep: list[OcrLine] = []
    for l in sorted(lines, key=lambda l: -max(l.confidence, 0) * max(1, len(l.text))):
        dup = False
        for k in keep:
            ix = min(l.x + l.w, k.x + k.w) - max(l.x, k.x)
            iy = min(l.y + l.h, k.y + k.h) - max(l.y, k.y)
            # the same ink read twice: they overlap in area *and* share most of their
            # height (a title's tall box, logo included, can reach over the small line under it)
            if ix > 0 and iy > 0 and ix * iy >= 0.5 * min(l.w * l.h, k.w * k.h) and iy >= 0.6 * max(l.h, k.h):
                dup = True
                break
        if not dup:
            keep.append(l)
    return sorted(keep, key=lambda l: (l.y, l.x))


def _plausible(text: str) -> bool:
    t = text.strip()
    good = sum(1 for c in t if c.isalnum() or _is_cjk(c))
    return bool(t) and good >= max(1, 0.6 * len(t.replace(" ", "")))


def _orphan_lines(rgb: np.ndarray, lines: list[OcrLine], engine: OcrEngine) -> list[OcrLine]:
    """Text the engine's page layout skipped.

    Tesseract's segmentation drops short, isolated words — table headers like 品名 or a
    lone number in a cell. Ink that looks like text but lies outside every recognised
    line is grouped into lines and read one crop at a time.
    """
    if not lines or not engine.can_recrop:
        return []
    H, W = rgb.shape[:2]
    em = float(np.median([l.h for l in lines])) / 1.15
    if em < 6:
        return []
    mask, bg, _thr = ink_mask(rgb)
    claimed = np.zeros(mask.shape, dtype=bool)
    for l in lines:
        p = int(0.25 * l.h)
        claimed[max(0, int(l.y) - p) : int(l.y + l.h) + p, max(0, int(l.x) - p) : int(l.x + l.w) + p] = True
    comps, _labels = find_components(mask.astype(np.uint8), min_area=max(4, int(0.004 * em * em)))
    cand = []
    for c in comps:
        if claimed[c.y : c.bottom, c.x : c.right].mean() > 0.3:
            continue
        if c.h > 2.2 * em or c.w > 2.5 * em or c.h < 0.12 * em:
            continue  # figures, rules, chart bars and specks are not letters
        if c.area > 0.85 * c.w * c.h and c.w * c.h > 0.5 * em * em:
            continue  # solid blocks (bars, filled cells)
        cand.append(c)
    # Group into lines. Parts of one character can be stacked (品 is three 口), so blobs
    # join when they sit side by side on a row or one above the other in a column.
    n = len(cand)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a = cand[i]
        for j in range(i + 1, n):
            b = cand[j]
            v_ov = min(a.bottom, b.bottom) - max(a.y, b.y)
            h_ov = min(a.right, b.right) - max(a.x, b.x)
            h_gap = max(a.x, b.x) - min(a.right, b.right)
            v_gap = max(a.y, b.y) - min(a.bottom, b.bottom)
            row = v_ov >= 0.3 * min(a.h, b.h) and h_gap <= 1.2 * em
            column = h_ov >= 0.3 * min(a.w, b.w) and v_gap <= 0.35 * em
            if row or column:
                parent[find(i)] = find(j)
    by_root: dict[int, list] = {}
    for i, c in enumerate(cand):
        by_root.setdefault(find(i), []).append(c)
    groups = [g for g in by_root.values() if max(c.bottom for c in g) - min(c.y for c in g) <= 2.0 * em]
    found: list[OcrLine] = []
    for g in groups:
        x0, y0 = min(c.x for c in g), min(c.y for c in g)
        x1, y1 = max(c.right for c in g), max(c.bottom for c in g)
        if (y1 - y0) < 0.45 * em or (x1 - x0) < 0.45 * em:
            continue
        pad = int(0.3 * em)
        crop = rgb[max(0, y0 - pad) : min(H, y1 + pad), max(0, x0 - pad) : min(W, x1 + pad)]
        try:
            text, conf = engine.recognize_crop(_prepare_crop(np.ascontiguousarray(crop), max(y1 - y0, em) / 1.0, bg))
        except Exception:
            continue
        if conf >= 55 and _plausible(text):
            found.append(OcrLine(text.strip(), float(x0), float(y0), float(x1 - x0), float(y1 - y0), conf))
    return found


def analyze_scanned_page(
    rgb: np.ndarray,
    scale: float,
    engine: OcrEngine,
    page_number: int,
    workers: int | None = None,
    grids: list[Grid] | None = None,
) -> ScanResult:
    """``rgb`` is the page render, ``scale`` its pixels per point."""
    H, W = rgb.shape[:2]
    notes: list[str] = []
    engine.dpi = scale * 72.0
    page_lines = [l for l in engine.recognize_page(rgb) if l.text.strip()]
    if grids is None:
        grids = find_grids(rgb, scale, _char_boxes(page_lines))
    ocr_lines = _split_at_cells(page_lines, _vertical_rules(grids, scale))
    rgb = erase_rules(rgb, grids, scale)  # lines are measured and re-read without the table's rules
    ocr_lines = _dedupe([l for l in ocr_lines if l.text.strip()])
    # A short, unsure line is often a fragment of a word the layout pass cut up (half of
    # 品名 read as 於). It must not hide that ink from the orphan search; it survives only
    # if nothing better is found where it is.
    weak = [l for l in ocr_lines if 0 <= l.confidence < 70 and len(l.text.strip()) <= 3]
    strong = [l for l in ocr_lines if l not in weak]
    orphans = _orphan_lines(rgb, strong, engine)

    def covered(l: OcrLine) -> bool:
        for o in orphans:
            ix = min(l.x + l.w, o.x + o.w) - max(l.x, o.x)
            iy = min(l.y + l.h, o.y + o.h) - max(l.y, o.y)
            if ix > 0 and iy > 0:
                return True
        return False

    ocr_lines = _dedupe(strong + orphans + [l for l in weak if not covered(l)])

    prepared: list[tuple[OcrLine, LineScan, int, int]] = []
    for ol in ocr_lines:
        pad = max(4, int(0.3 * ol.h))
        x0, y0 = max(0, int(ol.x) - pad), max(0, int(ol.y) - pad)
        x1, y1 = min(W, int(ol.x + ol.w) + pad), min(H, int(ol.y + ol.h) + pad)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        scan = scan_line(rgb[y0:y1, x0:x1], text_hint=ol.text, em_hint=ol.h / 1.15)
        if not scan.comps:
            continue
        # engine noise: a faint one- or two-character "line" with almost no ink
        if 0 <= ol.confidence < 25 and len(ol.text.strip()) <= 2 and len(scan.comps) <= 2 and not scan.symbols:
            continue
        prepared.append((ol, scan, x0, y0))

    styles = estimate_styles([p[1] for p in prepared])
    sizes_px = regularize_sizes(
        [e.size_px for e in styles], [len(p[1].text_comps) >= 6 for p in prepared]
    )

    # ---- re-read the pieces of lines that were cut apart, in parallel ---------------------
    jobs: list[tuple[int, _Item, np.ndarray]] = []
    reread: list[tuple[int, OcrLine, np.ndarray]] = []
    plans: list[list[list[_Item]] | None] = []
    for li, (ol, scan, x0, y0) in enumerate(prepared):
        blocks = _split_into_blocks(scan)
        needs_cut = len(blocks) > 1 or any(it.kind == "sym" for b in blocks for it in b)
        if not needs_cut or not engine.can_recrop:
            plans.append(None)
            if engine.can_recrop and (0 <= ol.confidence < 80 or len(ol.text.strip()) <= 4):
                reread.append((li, ol, _prepare_crop(np.ascontiguousarray(rgb[y0 : y0 + int(scan.rgb.shape[0] / scan.scale) + 1, x0 : x0 + int(scan.rgb.shape[1] / scan.scale) + 1]), scan.em / scan.scale, scan.bg)))
            continue
        plans.append(blocks)
        if ol.words:
            _assign_words(ol, blocks, scan, x0)
        for blk in blocks:
            for it in blk:
                if it.kind != "txt" or it.keep:
                    continue
                jobs.append((li, it, _text_crop(scan, it)))

    if jobs:
        def run(job):
            li, it, crop = job
            had = it.text if it.text else None
            sc = prepared[li][1]
            width_em = (it.x1 - it.x0) / max(sc.em, 1e-6)
            try:
                text, conf = engine.recognize_crop(crop)
            except Exception:  # one bad crop must not sink the page
                text, conf = "", -1.0
            if had is None:
                it.text, it.conf = text, conf
                return
            if not (text.strip() and _plausible(text)):
                return
            # Take the separate reading when the engine is clearly surer of it and it is a
            # correction of the same text — or, when the two disagree outright, when only it
            # fits the width of the ink (Tesseract read two bold 數量 as "Ae", sure of it).
            if _similar(text, had) >= 0.5:
                if conf >= it.conf + 10:
                    it.text, it.conf = text, conf
            elif conf >= 60 and _width_misfit(text, width_em) < 0.5 * _width_misfit(had, width_em):
                it.text, it.conf = text, conf

        with ThreadPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 2)) as pool:
            list(pool.map(run, jobs))

    if reread:
        def run_line(job):
            li, ol, crop = job
            try:
                text, conf = engine.recognize_crop(crop)
            except Exception:
                return
            # The line on its own, at a comfortable size, usually reads better than in the
            # page layout; take it only when the engine is clearly surer of it.
            if (text.strip() and conf > ol.confidence + (1 if len(ol.text.strip()) <= 4 else 5) and _plausible(text)
                    and (len(ol.text.strip()) <= 4 or _similar(text, ol.text) >= 0.5)):
                prepared[li] = (OcrLine(text.strip(), ol.x, ol.y, ol.w, ol.h, conf),) + prepared[li][1:]

        with ThreadPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 2)) as pool:
            list(pool.map(run_line, reread))

    # ---- assemble blocks --------------------------------------------------------------------
    out: list[Block] = []
    for li, ((ol, scan, x0, y0), est, plan, size_px) in enumerate(zip(prepared, styles, plans, sizes_px)):
        k = scan.scale

        def to_page(cx: float, cy: float) -> tuple[float, float]:
            return x0 + cx / k, y0 + cy / k

        def block_rect(items: list[_Item], ol=ol, k=k, y0=y0) -> Rect:
            # The crop has a margin, so it can hold the bottom of the line above or the top
            # of the one below; only ink centred in this line's band sets its height (a
            # block 2.5 lines tall made Word merge it with its neighbours).
            if ol.words:  # character boxes can be reported too tall; the typical one is not
                top = float(np.median([w.y for w in ol.words]))
                bot = float(np.median([w.y + w.h for w in ol.words]))
            else:
                top, bot = ol.y, ol.y + ol.h
            hh = max(bot - top, 1.0)
            lo = (top - 0.3 * hh - y0) * k
            hi = (bot + 0.3 * hh - y0) * k
            comps = [c for it in items for c in it.comps if lo <= (c.y + c.bottom) / 2 <= hi] or [c for it in items for c in it.comps]
            px0, py0 = to_page(min(it.x0 for it in items), min(c.y for c in comps))
            px1, py1 = to_page(max(it.x1 for it in items), max(c.bottom for c in comps))
            return Rect.from_bounds(px0 / scale, py0 / scale, px1 / scale, py1 / scale)

        em_pt = size_px / scale
        style = TextStyle(
            family=None,
            size=em_pt,
            bold=est.bold,
            italic=est.italic,
            color=scan.ink,
            source="measured",
            bold_confidence=est.bold_confidence,
            italic_confidence=est.italic_confidence,
        )

        if plan is None:
            # whole line as the engine read it; symbols are merged in by position when the
            # engine cannot re-read crops
            text, detected = ol.text.strip(), []
            if scan.symbols and not engine.can_recrop:
                syms = [(c.cx / k + x0, m) for c, m in scan.symbols]
                text, detected = _estimate_merge(text, min(c.x for c in scan.comps) / k + x0, max(c.right for c in scan.comps) / k + x0, syms)
                blk_notes = ["符號位置由版面推估，請確認。"]
            else:
                blk_notes = []
            all_items = [_Item("txt", scan.comps)]
            rect = block_rect(all_items)
            out.append(_make_block(len(out), page_number, text, rect, style, scan, ol.confidence, detected, blk_notes, run_text=text))
            continue

        for items in plan:
            parts: list[str] = []
            detected: list[int] = []
            confs: list[float] = []
            prev_x1: float | None = None
            prev_it: _Item | None = None
            for it in items:
                if prev_x1 is not None:
                    gap_em = (it.x0 - prev_x1) / scan.em
                    first = it.match.char if it.kind == "sym" else it.text.strip()[:1]
                    last = "".join(parts)[-1:]
                    parts.append(_spacing(prev_it, it, last, first, gap_em))
                if it.kind == "sym":
                    pos = len("".join(parts))
                    parts.append(it.match.char)
                    detected.append(pos)
                else:
                    t = it.text.strip()
                    parts.append(t)
                    if it.conf >= 0:
                        confs.append(it.conf)
                prev_x1 = it.x1
                prev_it = it
            text = "".join(parts).strip()
            if not text:
                continue
            lead = len("".join(parts)) - len("".join(parts).lstrip())
            detected = [d - lead for d in detected if d - lead >= 0]
            out.append(
                _make_block(
                    len(out), page_number, text, block_rect(items), style, scan,
                    (sum(confs) / len(confs)) if confs else ol.confidence, detected, [], run_text=text,
                )
            )

    if any(b.detected_symbols for b in out):
        n = sum(len(b.detected_symbols) for b in out)
        notes.append(f"以形狀偵測補回 {n} 個符號（■□●「」等），OCR 引擎本身常讀錯這類字元。")
    out = _harmonize_markers(out)
    out = [_with_text(b, cjk_punctuation(b.text)) for b in out]
    return ScanResult(out, notes)


_FULLWIDTH = {",": "，", ":": "：", ";": "；", "!": "！", "?": "？"}


def cjk_punctuation(text: str) -> str:
    """Comma, colon, semicolon, ! and ? beside Chinese are the full-width forms.

    Tesseract returns the ASCII forms with a space ("台端至本公司服務, 錄用"), which changes
    how the line looks once written back. A mark between two digits ("2,400") stays.
    """
    chars = list(text)
    out: list[str] = []
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        if ch in _FULLWIDTH:
            j = len(out) - 1
            while j >= 0 and out[j] == " ":
                j -= 1
            prev = out[j] if j >= 0 else ""
            k = i + 1
            while k < n and chars[k] == " ":
                k += 1
            nxt = chars[k] if k < n else ""
            digits = prev.isdigit() and nxt.isdigit()
            if not digits and (_is_cjk(prev) or _is_cjk(nxt)) and not (prev in "）)" and nxt.isascii() and nxt.isalnum()):
                del out[j + 1:]
                out.append(_FULLWIDTH[ch])
                i = k
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# What the engine makes of a ※ at the start of a list line.
_MARKER_MISREADS = ("HK", "PK", "K", "*", "了", "米", "X", "x", "%", "際", "“", "”", '"')


def _with_text(b: Block, text: str) -> Block:
    if text == b.text:
        return b
    runs = [Run(text, b.runs[0].style if b.runs else b.style, b.bounds.x, b.bounds.right)]
    return replace(b, text=text, runs=runs)


def _harmonize_markers(blocks: list[Block]) -> list[Block]:
    """List lines in one column share their marker. When most lines starting at the same
    x begin with ※, a line there that begins with something ※ is often misread as (K, HK,
    *, 了) gets the ※ back, and a lone one-character block sitting in the marker column
    right before a line's text is that line's marker."""
    if len(blocks) < 3:
        return blocks
    out = list(blocks)
    starts = [b for b in out if b.text.startswith("※")]
    if len(starts) < 3:
        return out
    col_x = sorted(b.bounds.x for b in starts)[len(starts) // 2]
    em = sorted(b.style.size for b in starts)[len(starts) // 2]
    in_col = lambda b: abs(b.bounds.x - col_x) <= 0.6 * em  # noqa: E731
    drop: set[int] = set()
    for i, b in enumerate(out):
        if not in_col(b) or b.text.startswith("※"):
            continue
        t = b.text
        for bad in _MARKER_MISREADS:
            if t.startswith(bad) and len(t) > len(bad) and (_is_cjk(t[len(bad)]) or t[len(bad)] == "※"):
                rest = t[len(bad):].lstrip()
                out[i] = _with_text(b, rest if rest.startswith("※") else "※" + rest)
                break
        else:
            if len(t.strip()) == 1:
                # a lone mark in the column: the marker of the text right of it on the same row
                for j, o in enumerate(out):
                    if j == i or j in drop:
                        continue
                    ov = min(b.bounds.bottom, o.bounds.bottom) - max(b.bounds.y, o.bounds.y)
                    gap = o.bounds.x - b.bounds.right
                    if ov > 0.3 * min(b.bounds.h, o.bounds.h) and 0 <= gap <= 1.5 * em and not o.text.startswith("※"):
                        out[j] = replace(_with_text(o, "※" + o.text),
                                         bounds=Rect.from_bounds(b.bounds.x, min(b.bounds.y, o.bounds.y), o.bounds.right, max(b.bounds.bottom, o.bounds.bottom)))
                        drop.add(i)
                        break
    return [b for i, b in enumerate(out) if i not in drop]


def _make_block(
    bid: int,
    page: int,
    text: str,
    rect: Rect,
    style: TextStyle,
    scan: LineScan,
    conf: float,
    detected: list[int],
    notes: list[str],
    run_text: str,
) -> Block:
    return Block(
        id=bid,
        page=page,
        text=text,
        bounds=rect,
        source="ocr",
        style=style,
        runs=[Run(run_text, style, rect.x, rect.right)],
        background=scan.bg,
        background_reliable=scan.bg_reliable,
        confidence=None if conf < 0 else conf / 100.0,
        detected_symbols=detected,
        notes=notes,
    )
