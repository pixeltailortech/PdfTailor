"""Turns the lines of PDF pages into a flowing document: paragraphs, headings, lists, tables.

Input is what the analysis already produces — one :class:`Block` per line (or per cell-sized
segment of a line) with a box in display points — plus the ruled grids found by
:mod:`tables`. Output is a list of :class:`PageDoc`, each an ordered list of
:class:`Para` and :class:`Table` that :mod:`docxout` writes as Word.

Nothing here knows about Word; everything is plain geometry and text rules, so it can be
tested without a document.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

import numpy as np

from .geometry import Rect
from .models import RGB, Block, Run, TextStyle
from .tables import Grid

# --------------------------------------------------------------------------- data

@dataclass
class Line:
    runs: list[Run]
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold_frac: float
    text: str
    parts: list[tuple[float, float]]  # x extent of each block that makes the line
    tab_before: list[int] = field(default_factory=list)  # indices into parts that follow a TAB
    leader: bool = False  # a table-of-contents line: "title ........ 12", dots now a TAB

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Para:
    runs: list[Run]
    text: str
    kind: str = "body"  # body | heading | list
    level: int = 0
    align: str = "left"
    left_indent: float = 0.0
    first_line: float = 0.0  # relative to left_indent; negative = hanging
    right_indent: float = 0.0
    space_before: float = 0.0
    space_after: float = 0.0
    line_pitch: float = 0.0  # > 0 for multi-line paragraphs
    size: float = 10.5
    bold_frac: float = 0.0
    tabs: list[tuple[float, str]] = field(default_factory=list)  # (pt from left margin, left|right)
    marker: str | None = None
    in_cell: bool = False
    # geometry kept for the later passes
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    n_lines: int = 1
    body_left: float = 0.0
    first_left: float = 0.0
    area_left: float = 0.0
    hang_exact: bool = False
    # list item whose wrapped lines return to the left of its text (Word's numbered list
    # with the number indented and the wrap flush with the paragraph): x of those lines
    wrap_left: float | None = None
    marker_sep: str = " "


@dataclass
class Cell:
    r: int
    c: int
    rs: int
    cs: int
    paras: list[Para] = field(default_factory=list)
    fill: RGB | None = None
    valign: str = "top"
    borders: tuple[bool, bool, bool, bool] = (False, False, False, False)  # top, left, bottom, right


@dataclass
class Table:
    bounds: Rect
    col_w: list[float]
    row_h: list[float]
    cells: list[Cell]
    ruled: bool
    line_pt: float = 0.5
    line_color: RGB = (0, 0, 0)
    left_indent: float = 0.0
    space_before: float = 0.0
    header_rows: int = 0  # leading rows repeated at the top of every page the table spans

    @property
    def top(self) -> float:
        return self.bounds.y

    @property
    def bottom(self) -> float:
        return self.bounds.bottom


@dataclass
class PageInput:
    page: int
    width: float
    height: float
    blocks: list[Block]
    grids: list[Grid] = field(default_factory=list)
    rgb: np.ndarray | None = None
    scale: float = 1.0


@dataclass
class HFLine:
    """One line of a running header or footer (written into Word's header/footer)."""
    runs: list[Run]
    text: str
    align: str  # left | center | right
    top: float
    bottom: float
    x0: float
    x1: float
    tabs: list[tuple[float, str]] = field(default_factory=list)  # pt from the left margin
    # a page number: (text before, Word number format, text after, value on this page)
    page_num: tuple[str, str, str, int] | None = None


@dataclass
class PageDoc:
    page: int
    width: float
    height: float
    margins: tuple[float, float, float, float]  # left, top, right, bottom
    elements: list = field(default_factory=list)  # Para | Table
    header: list[HFLine] = field(default_factory=list)
    footer: list[HFLine] = field(default_factory=list)


# --------------------------------------------------------------------------- text helpers

def _wide(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x2E80 and not (0xFE00 <= o <= 0xFE0F)


_END_PUNCT = "。！？；：.!?;:"
_BULLETS = "•·●○■□◆◇▪▫◦‣⁃▶►➢➤✓✔※★☆"
_BULLET_RE = re.compile(r"^([" + re.escape(_BULLETS) + r"]|[-–—*])\s+(?=\S)|^([" + re.escape(_BULLETS) + r"])(?=\S)")
_NUM_RE = re.compile(
    r"^(?:"
    r"[（(]\d{1,3}[)）]"  # (1) （1）
    r"|\d{1,3}(?:\.\d{1,3})+\.?(?=\s)"  # 1.1  1.2.3
    r"|\d{1,3}[\.\)）、](?!\d)"  # 1. 1) 1、
    r"|[（(][一二三四五六七八九十]{1,3}[)）]"  # （一）
    r"|[一二三四五六七八九十]{1,3}[、．]"  # 一、
    r"|[（(][A-Za-z][)）]"  # (a)
    r"|[A-Za-z][\.\)](?=\s)"  # a.  A)
    r"|[ivxIVX]{1,5}[\.\)](?=\s)"  # i. iv)
    r"|[①-⑳㈠-㈩⑴-⒇]"
    r")\s*"
)
_HEAD_NUM_RE = re.compile(r"^(?:第[一二三四五六七八九十百零\d]+[章節节條条款項项部篇]|\d{1,2}(?:\.\d{1,2})*[\.、\s]|[一二三四五六七八九十]{1,3}[、．]|[（(][一二三四五六七八九十]{1,3}[)）])")


def match_marker(text: str) -> tuple[str, str] | None:
    """Returns (marker as written, normalised marker) when ``text`` begins a list item."""
    m = _BULLET_RE.match(text)
    if m:
        raw = m.group(0).rstrip()
        # round bullets become Word's bullet; a ※ ■ □ ◆ ★ … is kept as written
        return raw, ("•" if raw[:1] in "•·●◦‣⁃-–—*" else raw[:1])
    m = _NUM_RE.match(text)
    if m:
        raw = m.group(0).rstrip()
        rest = text[len(m.group(0)):]
        if not rest.strip():
            return None
        return raw, raw
    return None


def _join_needs_space(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a[-1].isspace() or b[0].isspace():
        return False
    if _wide(a[-1]) or _wide(b[0]):
        return False
    if a[-1] == "-" and a[-2:-1].isalpha():
        return False
    return True


# --------------------------------------------------------------------------- lines

def group_rows(blocks: list[Block]) -> list[list[Block]]:
    items = sorted(blocks, key=lambda b: (b.bounds.cy, b.bounds.x))
    rows: list[list[Block]] = []
    for b in items:
        if rows:
            r = rows[-1]
            top = min(x.bounds.y for x in r)
            bot = max(x.bounds.bottom for x in r)
            ov = min(bot, b.bounds.bottom) - max(top, b.bounds.y)
            if ov >= 0.5 * min(bot - top, b.bounds.h):
                r.append(b)
                continue
        rows.append([b])
    for r in rows:
        r.sort(key=lambda b: b.bounds.x)
    return rows


def _bold_frac(runs: list[Run]) -> float:
    tot = sum(len(r.text.strip()) for r in runs)
    if not tot:
        return 0.0
    return sum(len(r.text.strip()) for r in runs if r.style.bold) / tot


def _dominant_size(runs: list[Run]) -> float:
    w: dict[float, int] = {}
    for r in runs:
        k = round(r.style.size * 2) / 2
        w[k] = w.get(k, 0) + len(r.text.strip())
    return max(w, key=w.get) if w else 10.5


def _copy_runs(b: Block) -> list[Run]:
    runs = b.runs or [Run(b.text, b.style, b.bounds.x, b.bounds.right)]
    return [Run(r.text, r.style, r.x0, r.x1) for r in runs]


# "第一章 緒論 ........ 1": four or more leader dots followed by a short page reference
_LEADER_RE = re.compile(r"\s*[.．·…‧・⋯]{4,}[.．·…‧・⋯\s]*")
_LEADER_TAIL_RE = re.compile(r"^\s*\S{1,8}\s*$")


def _leader_to_tab(runs: list[Run]) -> bool:
    """Replaces the dot leader of a contents line with a TAB; True when there was one."""
    full = "".join(r.text for r in runs)
    m = None
    for m_ in _LEADER_RE.finditer(full):
        m = m_
    if m is None or m.start() == 0 or not _LEADER_TAIL_RE.match(full[m.end():]):
        return False
    a, b = m.start(), m.end()
    pos = 0
    first = True
    for r in runs:
        lo, hi = pos, pos + len(r.text)
        pos = hi
        if hi <= a or lo >= b:
            continue
        keep_l = r.text[: max(0, a - lo)]
        keep_r = r.text[max(0, b - lo):]
        r.text = keep_l + ("\t" if first else "") + keep_r
        first = False
    return True


def make_line(row: list[Block], joiner: str = "\t") -> Line:
    runs: list[Run] = []
    parts: list[tuple[float, float]] = []
    tab_before: list[int] = []
    for i, b in enumerate(row):
        if i > 0:
            style = runs[-1].style if runs else b.style
            runs.append(Run(joiner, style, b.bounds.x, b.bounds.x))
            if joiner == "\t":
                tab_before.append(i)
        runs.extend(_copy_runs(b))
        parts.append((b.bounds.x, b.bounds.right))
    leader = _leader_to_tab(runs)
    text = "".join(r.text for r in runs)
    return Line(
        runs=runs,
        x0=min(b.bounds.x for b in row),
        x1=max(b.bounds.right for b in row),
        top=min(b.bounds.y for b in row),
        bottom=max(b.bounds.bottom for b in row),
        size=_dominant_size(runs),
        bold_frac=_bold_frac(runs),
        text=text,
        parts=parts,
        tab_before=tab_before,
        leader=leader,
    )


# --------------------------------------------------------------------------- paragraphs

@dataclass
class _Acc:
    lines: list[Line]
    marker: tuple[str, str] | None = None

    @property
    def body_left(self) -> float:
        return self.lines[1].x0 if len(self.lines) > 1 else self.lines[0].x0


def _first_token_width(line: Line) -> float:
    t = line.text.lstrip()
    if not t:
        return 0.0
    if _wide(t[0]):
        return line.size
    m = re.match(r"\S+", t)
    n = len(m.group()) if m else 1
    avg = (line.x1 - line.x0) / max(1, len(line.text))
    return n * avg + 0.3 * line.size


def _usual_pitch(lines: list[Line]) -> float | None:
    """The line spacing this run of text is set with, in pt, or None when it cannot be told.

    Taken from neighbouring lines of the same size; the lower-middle value, so the larger
    gaps between paragraphs do not count.
    """
    ps: list[float] = []
    for a, b in zip(lines, lines[1:]):
        size = max(a.size, b.size)
        p = b.cy - a.cy
        if abs(a.size - b.size) <= max(0.6, 0.06 * size) and 0.9 * size <= p <= 3.4 * size:
            ps.append(p / size)
    if len(ps) < 3:
        return None
    ps.sort()
    return ps[int(0.4 * (len(ps) - 1))]


def _starts_new(acc: _Acc, cur: Line, R: float, usual: float | None = None, in_cell: bool = False) -> bool:
    prev = acc.lines[-1]
    size = max(prev.size, cur.size)
    pitch = cur.cy - prev.cy
    # Single-spaced text sits about 1.2-1.5 sizes apart, so 1.75 separates paragraphs —
    # unless the document itself is set wider (1.5 or double line spacing), where every
    # line would otherwise become a paragraph of its own.
    limit = 1.75 * size + 1.0
    if usual is not None:
        limit = max(limit, 1.3 * usual * size)
    # in a table cell a line that ran to the cell's right edge has wrapped, however loosely
    # the rows are spaced (a 14 pt value on 28 pt rows)
    wrapped = in_cell and R - prev.x1 <= 0.6 * size and pitch <= 2.4 * size
    if (pitch > limit and not wrapped) or pitch < 0.55 * size:
        return True
    if abs(prev.size - cur.size) > max(0.6, 0.06 * size):
        return True
    if abs(prev.bold_frac - cur.bold_frac) > 0.8:
        return True
    if prev.tab_before or cur.tab_before:
        return True
    if prev.leader:  # a contents entry ends at its page number
        return True
    if match_marker(cur.text) is not None:
        return True
    # was the previous line really filled to the edge, i.e. would the next word have fit?
    slack = R - prev.x1
    if slack > _first_token_width(cur) + 0.5 * size:
        return True
    if prev.text.rstrip()[-1:] in "。" and slack > 0.5 * size and len(acc.lines) >= 1 and slack > 1.2 * size:
        return True
    ref = prev.x0 if len(acc.lines) == 1 else acc.body_left
    d = cur.x0 - ref
    tol = 1.3 * size
    if acc.marker is not None:
        if len(acc.lines) == 1:
            return d < -tol  # hanging wrap may be indented or flush, never outdented
        return abs(d) > tol
    if d > tol:
        return True
    if d < -tol:
        # first-line indent ("　　" in Chinese text): the 2nd line returns to the margin
        return len(acc.lines) >= 2
    return False


def build_paragraphs(lines: list[Line], L: float, R: float, in_cell: bool = False) -> list[Para]:
    if not lines:
        return []
    groups: list[_Acc] = []
    usual = _usual_pitch(lines)
    for ln in lines:
        mk = None if ln.leader else match_marker(ln.text)  # "2.1 Title …… 3" is a contents entry
        if groups and not _starts_new(groups[-1], ln, R, usual, in_cell):
            groups[-1].lines.append(ln)
        else:
            groups.append(_Acc([ln], mk))
    return [_finish(g, L, R, in_cell) for g in groups]


def _squeeze_cjk_spaces(runs: list[Run]) -> None:
    """Drops spaces between two wide characters (CJK text has none; the gap is a glyph-box artefact)."""
    chars = [(ri, ci) for ri, r in enumerate(runs) for ci in range(len(r.text))]
    flat = "".join(r.text for r in runs)
    drop: set[tuple[int, int]] = set()
    for i, ch in enumerate(flat):
        if ch == " " and 0 < i < len(flat) - 1 and _wide(flat[i - 1]) and _wide(flat[i + 1]):
            # a space read from a real text layer was typed ("第一章 緒論"); only spaces
            # guessed from gaps in a scan are artefacts
            if runs[chars[i][0]].style.source == "font":
                continue
            drop.add(chars[i])
    if not drop:
        return
    for ri, r in enumerate(runs):
        r.text = "".join(ch for ci, ch in enumerate(r.text) if (ri, ci) not in drop)


def _unify_scanned(runs: list[Run]) -> None:
    """One typeface and one size per paragraph for text measured from a scan.

    Family and size of scanned lines are estimates; small disagreements between the
    lines of one paragraph are measuring noise, and in Word they show as a patchwork.
    Runs of the same script take the paragraph's commonest family, and sizes within 15%
    of the commonest size take that size. Bold, italic and colour are left as measured;
    text read from a real text layer is never touched.
    """
    from collections import Counter

    def script(r: Run) -> str:
        return "cjk" if sum(_wide(c) for c in r.text) >= 2 else "latin"

    scanned = [r for r in runs if r.style.source == "measured" and r.text.strip()]
    if len(scanned) < 2:
        return
    fam: dict[str, Counter] = {"cjk": Counter(), "latin": Counter()}
    size: Counter = Counter()
    for r in scanned:
        n = len(r.text.strip())
        if r.style.family:
            fam[script(r)][r.style.family] += n
        size[round(r.style.size * 2) / 2] += n
    # the Latin companion font of the Chinese text: one per paragraph as well
    latin: Counter = Counter()
    for r in scanned:
        if script(r) == "cjk":
            latin[r.style.latin_family] += 1 + sum(1 for c in r.text if c.isascii() and c.isalnum())
    top_latin = latin.most_common(1)[0][0] if latin else None
    top_size = size.most_common(1)[0][0]
    top_fam = {k: (v.most_common(1)[0][0] if v else None) for k, v in fam.items()}
    for r in runs:
        if r.style.source != "measured":
            continue
        st = r.style
        f = top_fam[script(r)] if r.text.strip() else None
        if f and st.family != f:
            st = st.with_(family=f)
        if r.text.strip() and script(r) == "cjk" and st.latin_family != top_latin:
            st = st.with_(latin_family=top_latin)
        if st.size != top_size and 0.85 <= st.size / top_size <= 1.15:
            st = st.with_(size=top_size)
        r.style = st


def _finish(acc: _Acc, L: float, R: float, in_cell: bool) -> Para:
    lines = acc.lines
    runs: list[Run] = []
    for i, ln in enumerate(lines):
        if i > 0 and runs and ln.runs and _join_needs_space(runs[-1].text, ln.runs[0].text):
            runs.append(Run(" ", runs[-1].style, 0, 0))
        runs.extend(Run(r.text, r.style, r.x0, r.x1) for r in ln.runs)

    _squeeze_cjk_spaces(runs)
    _unify_scanned(runs)
    first = lines[0]
    size = _dominant_size(runs)
    para = Para(
        runs=runs, text="".join(r.text for r in runs), size=size, bold_frac=_bold_frac(runs),
        top=first.top, bottom=lines[-1].bottom, x0=min(l.x0 for l in lines), x1=max(l.x1 for l in lines),
        n_lines=len(lines), in_cell=in_cell,
    )
    if len(lines) > 1:
        ps = [lines[i + 1].cy - lines[i].cy for i in range(len(lines) - 1)]
        para.line_pitch = float(np.median(ps))

    # tab stops for multi-block lines (only single-line paragraphs get them)
    if len(lines) == 1 and first.tab_before:
        for i in first.tab_before:
            x0, x1 = first.parts[i]
            if x1 >= R - 0.5 * size and x0 - first.parts[i - 1][1] > size:
                para.tabs.append((x1 - L, "right"))
            else:
                para.tabs.append((x0 - L, "left"))

    if lines[-1].leader:  # contents entry: right-aligned page number after a dot leader
        pos = lines[-1].x1 - L
        para.tabs = [t for t in para.tabs if abs(t[0] - pos) > 2.0] + [(pos, "right-dot")]

    # list item: literal marker + TAB, hanging indent
    # a one-line cell ("11.75 小時", "1. 版面與對稱邊界") is text, not a list item
    if acc.marker is not None and not (in_cell and len(lines) == 1):
        raw, norm = acc.marker
        r0 = runs[0]
        if r0.text.startswith(raw):
            tail = r0.text[len(raw):].lstrip()
            para.marker_sep = " " if r0.text[len(raw):][:1].isspace() else ""
            r0.text = norm + "\t" + tail
            if not tail and len(runs) > 1:  # the marker is a run of its own: its space goes with the tab
                runs[1].text = runs[1].text.lstrip()
            para.marker = norm
            para.text = "".join(r.text for r in runs)
            wrap = None
            if len(lines) > 1 and lines[1].x0 > first.x0 + 0.5 * size:
                text_x = lines[1].x0
                para.hang_exact = True
            elif len(lines) > 1 and first.tab_before and first.parts[0][1] - first.parts[0][0] < 4.5 * size:
                text_x = first.parts[1][0]
                para.hang_exact = True
                para.tabs = [t for t in para.tabs if abs(t[0] - (text_x - L)) > 1]
                wrap = lines[1].x0
            elif first.tab_before and first.parts[0][1] - first.parts[0][0] < 4.5 * size:
                text_x = first.parts[1][0]
                para.hang_exact = True
                para.tabs = [t for t in para.tabs if abs(t[0] - (text_x - L)) > 1]
            else:
                width = sum(1.0 if _wide(ch) else 0.55 for ch in raw) * size
                text_x = first.x0 + max(1.2 * size, width + 0.4 * size)
                if len(lines) > 1:
                    wrap = lines[1].x0
            if wrap is not None and wrap < text_x - 0.5 * size:
                para.wrap_left = wrap
            para.kind = "list"
            para.first_left = first.x0
            para.body_left = text_x
            return para

    para.first_left = first.x0
    para.body_left = lines[1].x0 if len(lines) > 1 else first.x0
    return para


# --------------------------------------------------------------------------- borderless tables

def _row_extent(row: list[Block]) -> tuple[float, float]:
    return min(b.bounds.y for b in row), max(b.bounds.bottom for b in row)


def _row_size(row: list[Block]) -> float:
    return float(np.median([b.style.size for b in row]))


def _column_intervals(rows: list[list[Block]], n: int) -> list[tuple[float, float]] | None:
    iv = sorted((b.bounds.x, b.bounds.right) for r in rows if len(r) == n for b in r)
    merged: list[list[float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1] + 2.0:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if len(merged) != n:
        return None
    if any(merged[i + 1][0] - merged[i][1] < 4.0 for i in range(n - 1)):
        return None
    return [(a, b) for a, b in merged]


def _row_cy(row: list[Block]) -> float:
    t, b = _row_extent(row)
    return (t + b) / 2


def _wraps_from(rows: list[list[Block]], k: int, start: int) -> bool:
    """Is the single-block row ``k`` the wrapped second line of the value above it?

    It starts where the value above starts, that value ran to (nearly) the right edge of
    its column, and the lines are no further apart than a wrapped line plus paragraph gap.
    """
    cur, prev = rows[k], rows[k - 1]
    if len(cur) != 1 or not prev:
        return False
    pb = prev[-1]
    size = max(_row_size(prev), _row_size(cur))
    if abs(cur[0].bounds.x - pb.bounds.x) > 4.0 or _row_cy(cur) - _row_cy(prev) > 2.6 * size:
        return False
    reach = max(r[-1].bounds.right for r in rows[start:k] if r)
    return pb.bounds.right >= reach - 1.2 * size


def detect_text_tables(rows: list[list[Block]], max_right: float = 1e9) -> list[tuple[int, int, Table]]:
    """Borderless tables: consecutive rows whose blocks line up in columns."""
    found: list[tuple[int, int, Table]] = []
    i = 0
    n = len(rows)
    while i < n:
        if len(rows[i]) < 2:
            i += 1
            continue
        end = i
        k = i + 1
        while k < n:
            prev, cur = rows[k - 1], rows[k]
            size = max(_row_size(prev), _row_size(cur))
            ratio = _row_size(cur) / max(0.1, _row_size(prev))
            pitch = _row_cy(cur) - _row_cy(prev)
            if not 0.8 <= ratio <= 1.25:
                break
            if len(cur) >= 2:
                if pitch > 2.4 * size:
                    break
                end = k
            elif pitch > 1.7 * size:
                if not _wraps_from(rows, k, i):
                    break
                end = k
            k += 1
        tbl, used_end = None, end
        while used_end > i:
            run = rows[i:used_end + 1]
            multi = [r for r in run if len(r) >= 2]
            if len(multi) < 2:
                break
            mode = Counter(len(r) for r in multi).most_common(1)[0][0]
            cols = _column_intervals(multi, mode)
            if cols is None:
                break
            bad = _first_spanning_single(run, cols)
            if bad is None:
                tbl = _build_text_table(run, cols, max_right)
                break
            used_end = i + bad - 1
            while used_end > i and len(rows[used_end]) < 2:
                used_end -= 1
        if tbl is not None:
            found.append((i, used_end, tbl))
            i = used_end + 1
        else:
            i += 1
    return found


def _first_spanning_single(run: list[list[Block]], cols: list[tuple[float, float]]) -> int | None:
    """Index of the first single-block row that is not confined to one column."""
    bounds = [cols[0][0] - 2.0] + [(a[1] + b[0]) / 2 for a, b in zip(cols, cols[1:])] + [cols[-1][1] + 2.0]
    for idx, r in enumerate(run):
        if len(r) != 1:
            continue
        blk = r[0]
        c0 = next((c for c in range(len(cols)) if blk.bounds.x + 0.5 < bounds[c + 1]), len(cols) - 1)
        c1 = next((c for c in range(len(cols)) if blk.bounds.right - 0.5 < bounds[c + 1]), len(cols) - 1)
        if c0 != c1:
            return idx
    return None


def _build_text_table(rows: list[list[Block]], cols: list[tuple[float, float]], max_right: float = 1e9) -> Table | None:
    n = len(cols)
    bounds_x = [cols[0][0] - 2.0]
    for a, b in zip(cols, cols[1:]):
        # the left column keeps most of the gap: Word's fonts run wider than the PDF's
        bounds_x.append(b[0] - min(4.0, 0.5 * (b[0] - a[1])))
    bounds_x.append(max(cols[-1][1] + 2.0, min(cols[-1][1] + 16.0, max_right)))  # room for wider Word fonts

    def col_of(x: float) -> int:
        for c in range(n):
            if x < bounds_x[c + 1]:
                return c
        return n - 1

    # table rows, each: {(c0, c1): [blocks]}
    trows: list[dict[tuple[int, int], list[Block]]] = []
    row_top: list[float] = []
    row_bottom: list[float] = []
    prev_ext: tuple[float, float] | None = None
    col_reach: dict[int, float] = {}
    for r in rows:
        for blk in r:
            c0, c1 = col_of(blk.bounds.x + 0.5), col_of(blk.bounds.right - 0.5)
            if c0 == c1:
                col_reach[c0] = max(col_reach.get(c0, 0.0), blk.bounds.right)
    for r in rows:
        t, b = _row_extent(r)
        size = _row_size(r)
        spans = []
        for blk in r:
            c0 = col_of(blk.bounds.x + 0.5)
            c1 = col_of(blk.bounds.right - 0.5)
            spans.append(((c0, c1), blk))
        tight = prev_ext is not None and (t + b) / 2 - (prev_ext[0] + prev_ext[1]) / 2 <= 1.35 * size
        cont = tight and len(r) < n and all(s[0][0] == s[0][1] for s in spans) and trows
        if not cont and trows and len(r) == 1 and spans[0][0][0] == spans[0][0][1] and prev_ext is not None:
            # a value that wrapped onto a second line, with the paragraph gap of the rows
            key = spans[0][0]
            above = trows[-1].get(key)
            if above and (t + b) / 2 - (prev_ext[0] + prev_ext[1]) / 2 <= 2.6 * size \
                    and abs(spans[0][1].bounds.x - above[-1].bounds.x) <= 4.0 \
                    and above[-1].bounds.right >= col_reach[key[0]] - 1.2 * size:
                cont = True
        if cont:
            for key, blk in spans:
                trows[-1].setdefault(key, []).append(blk)
            row_bottom[-1] = max(row_bottom[-1], b)
        else:
            d: dict[tuple[int, int], list[Block]] = {}
            for key, blk in spans:
                d.setdefault(key, []).append(blk)
            trows.append(d)
            row_top.append(t)
            row_bottom.append(b)
        prev_ext = (t, b)

    blocks_all = [blk for d in trows for bl in d.values() for blk in bl]
    avg_chars = sum(len(b.text) for b in blocks_all) / max(1, len(blocks_all))
    tight_all = len(trows) >= 4 and all(
        row_top[i + 1] - row_top[i] <= 1.5 * max(b.style.size for b in blocks_all)
        for i in range(len(trows) - 1))
    if tight_all and avg_chars >= 25:
        # newspaper-style columns of running text, not a table: one row, a cell per column
        merged_row: dict[tuple[int, int], list[Block]] = {}
        for d in trows:
            for key, bl in d.items():
                merged_row.setdefault(key, []).extend(bl)
        trows = [merged_row]
        row_top, row_bottom = [row_top[0]], [row_bottom[-1]]

    cells: list[Cell] = []
    for ri, d in enumerate(trows):
        occupied = set()
        for (c0, c1), blks in d.items():
            cell = Cell(ri, c0, 1, c1 - c0 + 1)
            L, R = cols[c0][0], cols[c1][1]  # where the text really sits
            cell.paras = _cell_paragraphs(blks, L, R)
            for p in cell.paras:  # carry the gap between text and cell edge as indent
                if p.align == "left":
                    p.left_indent += max(0.0, L - (bounds_x[c0] + 2.5))
                elif p.align == "right":
                    p.right_indent = max(0.0, (bounds_x[c1 + 1] - 2.5) - R)
            cells.append(cell)
            occupied.update(range(c0, c1 + 1))
        for c in range(n):
            if c not in occupied:
                cells.append(Cell(ri, c, 1, 1))
    cells.sort(key=lambda c: (c.r, c.c))
    for ri in range(len(trows)):  # a wrapped value's line pitch also sets where its row-mates' first line sits
        pitch = max((p.line_pitch for c in cells if c.r == ri for p in c.paras), default=0.0)
        if pitch > 0:
            for c in cells:
                if c.r == ri:
                    for p in c.paras:
                        if p.line_pitch == 0:
                            p.line_pitch = pitch
    # heights: pitch-to-pitch
    row_h = []
    for ri in range(len(trows)):
        nxt = row_top[ri + 1] if ri + 1 < len(trows) else row_bottom[ri]
        row_h.append(max(0.0, nxt - row_top[ri]))
    bounds = Rect.from_bounds(bounds_x[0], row_top[0], bounds_x[-1], row_bottom[-1])
    return Table(bounds, [b - a for a, b in zip(bounds_x, bounds_x[1:])], row_h, cells, ruled=False)


def _cell_paragraphs(blocks: list[Block], L: float, R: float) -> list[Para]:
    rows = group_rows(blocks)
    lines = [make_line(r, joiner=" ") for r in rows]
    paras = build_paragraphs(lines, L, R, in_cell=True)
    _harmonize_lists(paras)
    _align(paras, L, R, cell=True)
    _spacing(paras)
    return paras


# --------------------------------------------------------------------------- ruled tables

def _cells_from_grid(g: Grid) -> list[tuple[int, int, int, int]]:
    """Merged-cell rectangles (r, c, rowspan, colspan) from missing separators."""
    R, C = g.rows, g.cols
    parent = list(range(R * C))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for r in range(R):
        for c in range(C - 1):
            if not g.vsep[r][c]:
                union(r * C + c, r * C + c + 1)
    for r in range(R - 1):
        for c in range(C):
            if not g.hsep[r][c]:
                union(r * C + c, (r + 1) * C + c)
    comps: dict[int, list[int]] = {}
    for i in range(R * C):
        comps.setdefault(find(i), []).append(i)
    out = []
    for members in comps.values():
        rs = [m // C for m in members]
        cs = [m % C for m in members]
        r0, r1, c0, c1 = min(rs), max(rs), min(cs), max(cs)
        if (r1 - r0 + 1) * (c1 - c0 + 1) == len(members):
            out.append((r0, c0, r1 - r0 + 1, c1 - c0 + 1))
        else:  # not a rectangle: keep the cells apart
            out.extend((m // C, m % C, 1, 1) for m in members)
    out.sort()
    return out


def _edge(vals: list[bool]) -> bool:
    return bool(vals) and sum(vals) * 2 >= len(vals)


_RULE_CHARS = " |¦│┃｜"


def _strip_rule_noise(b: Block) -> Block | None:
    """OCR reads a table's vertical rules as '|' at the edges of a cell's text; drop them."""
    if b.source != "ocr" or b.text == b.text.strip(_RULE_CHARS):
        return b
    runs = [Run(r.text, r.style, r.x0, r.x1) for r in b.runs] or [Run(b.text, b.style, b.bounds.x, b.bounds.right)]
    while runs and not runs[0].text.lstrip(_RULE_CHARS):
        runs.pop(0)
    while runs and not runs[-1].text.rstrip(_RULE_CHARS):
        runs.pop()
    if not runs:
        return None
    runs[0].text = runs[0].text.lstrip(_RULE_CHARS)
    runs[-1].text = runs[-1].text.rstrip(_RULE_CHARS)
    text = "".join(r.text for r in runs)
    return replace(b, text=text, runs=runs, char_x=None) if text.strip() else None


# --------------------------------------------------------------------------- cutting blocks

def split_block(b: Block, cuts: list[float]) -> list[Block]:
    """Splits a text-layer line at the given x positions (cell borders, a label's end).

    Each character goes to the side its centre is on; the line is cut where that side
    changes. Spaces next to a cut are dropped. Lines without per-character positions, or
    not crossed by any cut, come back unchanged.
    """
    cx = b.char_x
    if not cx or len(cx) != len(b.text) or not cuts:
        return [b]
    cuts = sorted(cuts)

    def side(i: int) -> int:
        mid = (cx[i][0] + cx[i][1]) / 2
        return sum(1 for c in cuts if mid > c)

    sides = [side(i) for i in range(len(b.text))]
    vis = [i for i, ch in enumerate(b.text) if not ch.isspace()]
    if not vis or len({sides[i] for i in vis}) < 2:
        return [b]
    groups: list[list[int]] = []
    side_cur = None
    for i in vis:
        if side_cur is None or sides[i] != side_cur:
            groups.append([])
            side_cur = sides[i]
        groups[-1].append(i)
    flat = "".join(r.text for r in b.runs)
    runs_ok = bool(b.runs) and flat == b.text
    out: list[Block] = []
    for g in groups:
        a0, a1 = g[0], g[-1] + 1
        text = b.text[a0:a1]
        runs: list[Run] = []
        if runs_ok:
            pos = 0
            for r in b.runs:
                lo, hi = max(pos, a0), min(pos + len(r.text), a1)
                if hi > lo:
                    seg_cx = cx[lo:hi]
                    runs.append(Run(r.text[lo - pos:hi - pos], r.style, min(c[0] for c in seg_cx), max(c[1] for c in seg_cx)))
                pos += len(r.text)
        style = max(runs, key=lambda r: len(r.text.strip())).style if runs else b.style
        part_cx = cx[a0:a1]
        x0 = min(c[0] for c in part_cx)
        x1 = max(c[1] for c in part_cx)
        out.append(replace(
            b, text=text, runs=runs or [Run(text, style, x0, x1)], style=style,
            bounds=Rect.from_bounds(x0, b.bounds.y, x1, b.bounds.bottom), char_x=list(part_cx),
            notes=list(b.notes), detected_symbols=[],
        ))
    return out


# a short label, its colon, then the value: "研 究 主 題： 台灣北部山城聚落…"
_COLON = "：:"


def _label_cut(b: Block) -> tuple[float, float] | None:
    """(x just after the label's colon, x where the value starts) for a label/value line."""
    cx = b.char_x
    if not cx or len(cx) != len(b.text):
        return None
    t = b.text
    k = next((i for i, ch in enumerate(t) if ch in _COLON), -1)
    if k < 1:
        return None
    label = t[:k]
    if not 1 <= len(re.sub(r"\s+", "", label)) <= 8 or re.sub(r"\s+", "", label).isdigit():
        return None
    j = k + 1
    while j < len(t) and t[j].isspace():
        j += 1
    if j >= len(t):
        return None
    return cx[k][1], cx[j][0]


_SPREAD_RE = re.compile(r"(?:\S[ \u3000])+\S[：:]?")


def _spread_label(b: Block) -> list[int] | None:
    """Indices of the characters of a letter-spaced label ("研 究 主 題："), else None."""
    t = b.text
    if not b.char_x or len(b.char_x) != len(t) or not b.runs or not _SPREAD_RE.fullmatch(t):
        return None
    vis = [i for i, ch in enumerate(t) if not ch.isspace()]
    body = [i for i in vis if t[i] not in _COLON]
    if len(body) < 2 or not all(_wide(t[i]) for i in body):
        return None
    return vis


def _tighten_label(b: Block, extra: float, x0: float) -> Block:
    """"研 究 主 題：" → "研究主題：" with the gaps as character spacing, starting at ``x0``.

    A typed space is as wide as the substitute font says (half an em or more in 標楷體),
    which made the label wider than its PDF column and wrapped it in Word; character
    spacing is the same in every font. The colon stays out of it, so no spacing trails.
    """
    vis = _spread_label(b)
    if vis is None:
        return b
    t, cx = b.text, b.char_x
    base = max(b.runs, key=lambda r: len(r.text.strip())).style
    body = [i for i in vis if t[i] not in _COLON]
    runs = [Run("".join(t[i] for i in body), base.with_(spacing=round(extra, 1)), cx[body[0]][0], cx[body[-1]][1])]
    if len(vis) > len(body):
        k = vis[-1]
        runs.append(Run(t[k], base, cx[k][0], cx[k][1]))
    text = "".join(r.text for r in runs)
    x1 = b.bounds.x + b.bounds.w
    return replace(b, text=text, runs=runs, style=base, char_x=None,
                   bounds=Rect.from_bounds(min(x0, b.bounds.x), b.bounds.y, x1, b.bounds.bottom))


def _split_label_values(blocks: list[Block]) -> list[Block]:
    """Cuts the label off every line of a column of "label：value" lines.

    Only when at least two lines of the same size line up — the same label start and the
    same value start; a single "註：…" line in running text stays whole.
    """
    cand: list[tuple[Block, float, float]] = []
    for b in blocks:
        cut = _label_cut(b)
        if cut is not None:
            k = next(i for i, ch in enumerate(b.text) if ch in _COLON)
            j = k + 1
            while b.text[j].isspace():
                j += 1
            cand.append((b, cut[0], b.char_x[j][0]))
    # labels that already stand alone ("作者姓名：" with its value in another block) vouch
    # for a line whose label and value ran together
    alone = [(m, m.bounds.x + m.bounds.w) for m in blocks
             if m.text.strip()[-1:] in _COLON and len(re.sub(r"\s+", "", m.text)) <= 9]
    if not cand or len(cand) + len(alone) < 2:
        return blocks
    chosen: set[int] = set()
    for b, end, vx in cand:
        # labels set to one width end their colons on one x; ragged labels start their
        # values on one x (glyph ink makes that wobble by a few points)
        mates = [m for m, me, mvx in cand if m is not b and (abs(me - end) <= 4.0 or abs(mvx - vx) <= 8.0)
                 and abs(m.bounds.x - b.bounds.x) <= 12.0 and abs(m.style.size - b.style.size) <= 0.5]
        mates += [m for m, me in alone if m is not b and abs(m.bounds.x - b.bounds.x) <= 12.0 and abs(m.style.size - b.style.size) <= 0.5]
        if mates:
            chosen.add(id(b))
    if not chosen:
        return blocks
    alone_ids = {id(m) for m, _ in alone}
    out: list[Block] = []
    labels: list[int] = []  # positions in ``out`` of the label blocks
    for b in blocks:
        if id(b) in chosen:
            end = _label_cut(b)
            parts = split_block(b, [(end[0] + end[1]) / 2])
            labels.append(len(out))
            out.extend(parts)
        else:
            if id(b) in alone_ids:
                labels.append(len(out))
            out.append(b)
    # letter-spaced labels share one pitch that fills the label column exactly
    spread = [(k, _spread_label(out[k])) for k in labels]
    spread = [(k, v) for k, v in spread if v is not None]
    if spread:
        vx = sorted(v for _, _, v in cand)[len(cand) // 2]
        x0 = min(out[k].bounds.x for k, _ in spread)
        size = out[spread[0][0]].style.size
        n = max(len(v) for _, v in spread)
        avail = vx - x0 - 12.0
        extra = min(max(0.0, (avail - n * size) / max(1, n - 1)), 1.2 * size)
        for k, _ in spread:
            out[k] = _tighten_label(out[k], extra, x0)
    return out


def build_ruled_table(g: Grid, blocks: list[Block], rgb: np.ndarray | None, scale: float) -> tuple[Table, list[Block]]:
    """Fills a grid with the blocks whose centres fall inside it. Returns (table, blocks used)."""
    rects = _cells_from_grid(g)
    cells: list[Cell] = []
    by_cell: dict[int, list[Block]] = {i: [] for i in range(len(rects))}
    inner = g.bounds.inflate(1.5)
    used: list[Block] = []

    def locate(px: float, py: float) -> int:
        best, best_d = 0, 1e18
        for i, (r, c, rs, cs) in enumerate(rects):
            x0, x1, y0, y1 = g.xs[c], g.xs[c + cs], g.ys[r], g.ys[r + rs]
            if x0 <= px <= x1 and y0 <= py <= y1:
                return i
            dx = max(x0 - px, 0, px - x1)
            dy = max(y0 - py, 0, py - y1)
            d = dx * dx + dy * dy
            if d < best_d:
                best, best_d = i, d
        return best

    def row_of(py: float) -> int:
        for r in range(g.rows):
            if g.ys[r] <= py <= g.ys[r + 1]:
                return r
        return min(range(g.rows), key=lambda r: min(abs(py - g.ys[r]), abs(py - g.ys[r + 1])))

    for b in blocks:
        if inner.contains_point(b.bounds.cx, b.bounds.cy):
            used.append(b)
            # A line the text layer ran across two cells ("時段區間 空間節點") is cut at every
            # rule that separates them in this row; cells merged across a missing rule stay whole.
            r = row_of(b.bounds.cy)
            cuts = [g.xs[c + 1] for c in range(g.cols - 1) if g.vsep[r][c]]
            for piece in split_block(b, cuts):
                clean = _strip_rule_noise(piece)
                if clean is not None:
                    by_cell[locate(clean.bounds.cx, clean.bounds.cy)].append(clean)

    for i, (r, c, rs, cs) in enumerate(rects):
        x0, x1, y0, y1 = g.xs[c], g.xs[c + cs], g.ys[r], g.ys[r + rs]
        pad = 2.5
        cell = Cell(r, c, rs, cs)
        blks = by_cell[i]
        if blks:
            cell.paras = _cell_paragraphs(blks, x0 + pad, x1 - pad)
            ct = min(b.bounds.y for b in blks)
            cb = max(b.bounds.bottom for b in blks)
            room_top, room_bot = ct - y0, y1 - cb
            if abs(room_top - room_bot) <= 0.25 * (y1 - y0) and (room_top + room_bot) > 6:
                cell.valign = "center"
            elif room_bot < 3 and room_top > 6:
                cell.valign = "bottom"
        cell.borders = (
            True if r == 0 else _edge([g.hsep[r - 1][cc] for cc in range(c, c + cs)]),
            True if c == 0 else _edge([g.vsep[rr][c - 1] for rr in range(r, r + rs)]),
            True if r + rs == g.rows else _edge([g.hsep[r + rs - 1][cc] for cc in range(c, c + cs)]),
            True if c + cs == g.cols else _edge([g.vsep[rr][c + cs - 1] for rr in range(r, r + rs)]),
        )
        if rgb is not None:
            cell.fill = _sample_fill(rgb, scale, x0, y0, x1, y1)
        cells.append(cell)

    table = Table(
        bounds=g.bounds,
        col_w=[b - a for a, b in zip(g.xs, g.xs[1:])],
        row_h=[b - a for a, b in zip(g.ys, g.ys[1:])],
        cells=cells,
        ruled=True,
        line_pt=g.line_pt,
        line_color=g.line_color,
    )
    return table, used


def _sample_fill(rgb: np.ndarray, scale: float, x0: float, y0: float, x1: float, y1: float) -> RGB | None:
    H, W = rgb.shape[:2]
    m = 2.5 * scale
    a, b = int(x0 * scale + m), int(y0 * scale + m)
    c, d = int(x1 * scale - m), int(y1 * scale - m)
    a, c = max(0, a), min(W, c)
    b, d = max(0, b), min(H, d)
    if c - a < 3 or d - b < 3:
        return None
    px = rgb[b:d, a:c].reshape(-1, 3)
    # the most common colour of the cell interior, which is the fill rather than the text
    q = (px // 8).astype(np.int32)
    key = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
    top = np.bincount(key).argmax()
    col = px[key == top].mean(axis=0)
    r, g_, b_ = (int(round(v)) for v in col)
    if min(r, g_, b_) >= 244:
        return None
    return (r, g_, b_)


# --------------------------------------------------------------------------- alignment & spacing

def _align(paras: list[Para], L: float, R: float, cell: bool = False, page_cx: float | None = None) -> None:
    w = max(1.0, R - L)
    for p in paras:
        p.area_left = L
        size = p.size
        slack_l = p.x0 - L
        slack_r = R - p.x1
        cx_off = abs((p.x0 + p.x1) / 2 - (L + R) / 2)
        if page_cx is not None:
            cx_off = min(cx_off, abs((p.x0 + p.x1) / 2 - page_cx))
        if cell:
            ctol = 0.06 * w + 1.5
            rtol = 0.04 * w + 2.0
        else:
            ctol = 0.02 * w + 2.0
            rtol = 0.015 * w + 2.0
        p.align = "left"
        if p.kind == "list" or p.tabs:
            pass
        elif slack_l > 1.5 * size and slack_r > 1.5 * size and cx_off <= ctol:
            p.align = "center"
        elif slack_r <= rtol and (slack_l > 0.9 * size if cell else (slack_l > 0.15 * w and slack_l > 2 * size)):
            p.align = "right"
        if p.align != "left":
            p.left_indent = 0.0
            p.right_indent = 0.0
            p.first_line = 0.0
            if p.align == "center":  # Word centres in the text area: shift if the text sits off-centre
                delta = (p.x0 + p.x1) / 2 - (L + R) / 2
                if delta > 1.0:
                    p.left_indent = 2 * delta
                elif delta < -1.0:
                    p.right_indent = -2 * delta
            continue
        if p.kind == "list" and p.wrap_left is not None:
            # marker indented, wrapped lines flush further left: a first-line indent plus a
            # tab stop where the item's text starts
            p.left_indent = max(0.0, p.wrap_left - L)
            p.first_line = p.first_left - max(p.wrap_left, L)
            p.tabs = [t for t in p.tabs if abs(t[0] - (p.body_left - L)) > 1] + [(p.body_left - L, "left")]
        elif p.kind == "list":
            p.left_indent = max(0.0, p.body_left - L)
            p.first_line = -(p.body_left - p.first_left)
        else:
            p.left_indent = max(0.0, p.body_left - L)
            p.first_line = p.first_left - p.body_left if p.n_lines > 1 else 0.0
        if p.left_indent < 0.6 * size and p.kind != "list":
            p.left_indent = 0.0
        if abs(p.first_line) < 0.6 * size and p.kind != "list":
            p.first_line = 0.0
        if cell:
            p.left_indent = 0.0 if p.left_indent < 4 else p.left_indent


def _harmonize_lists(paras: list[Para]) -> None:
    """Items of one list share one text start, whatever their individual estimates were."""
    i = 0
    while i < len(paras):
        if paras[i].kind != "list":
            i += 1
            continue
        j = i
        while j + 1 < len(paras) and paras[j + 1].kind == "list" and abs(paras[j + 1].first_left - paras[i].first_left) <= 3.0:
            j += 1
        grp = paras[i:j + 1]
        exact = [p.body_left for p in grp if p.hang_exact]
        src = exact or [p.body_left for p in grp]
        x = float(np.median(src))
        wraps = [p.wrap_left for p in grp if p.wrap_left is not None]
        for p in grp:
            p.body_left = x
            p.first_left = float(np.median([q.first_left for q in grp]))
            p.wrap_left = float(np.median(wraps)) if wraps else None
        i = j + 1


def _spacing(paras: list[Para]) -> None:
    for a, b in zip(paras, paras[1:]):
        pitch = (b.top + b.bottom) / 2 - (a.top + a.bottom) / 2
        extra = pitch - 1.25 * max(a.size, b.size) - (b.n_lines - 1) * 0
        # pitch measured between the centre of the previous paragraph's last line and of the
        # next paragraph's first line, so the previous paragraph's own height must be removed
        last_cy = a.bottom - a.size * 0.5
        first_cy = b.top + b.size * 0.5
        d = first_cy - last_cy
        size = max(a.size, b.size)
        # Word gives a line of a multi-line paragraph its full line spacing, so that much
        # of the distance is already there; a one-line paragraph between lines set at the
        # same spacing (a short list item) takes that spacing instead of a space before.
        if b.line_pitch <= 0 and a.line_pitch > 0 and abs(a.size - b.size) <= 0.06 * size \
                and 0.85 * a.line_pitch <= d <= 1.15 * a.line_pitch:
            b.line_pitch = a.line_pitch
        line_h = max(b.line_pitch, 1.25 * size) if b.line_pitch > 0 else 1.25 * size
        extra = d - line_h
        b.space_before = extra if extra >= 2.0 else 0.0
        b.space_before = min(b.space_before, 48.0)


# --------------------------------------------------------------------------- page assembly

def _region(lines: list[Line]) -> tuple[float, float]:
    """Left and right edge of the text column from where its lines start and end."""
    if not lines:
        return 0.0, 0.0
    body = [l for l in lines if len(l.text.strip()) >= 8] or lines
    weights: dict[int, float] = {}
    for l in body:
        weights[int(round(l.x0 / 3))] = weights.get(int(round(l.x0 / 3)), 0) + len(l.text)
    total = sum(weights.values())
    top = max(weights.values())
    lefts = [k for k, v in weights.items() if v >= 0.15 * total or v == top]
    L = min(lefts) * 3.0
    x1s = sorted(l.x1 for l in body)
    R = x1s[min(len(x1s) - 1, int(0.9 * len(x1s)))]
    return L, max(R, L + 1)


def _page_chunks(page: PageInput) -> tuple[list, tuple[float, float, float, float]]:
    """Ordered Para/Table elements of one page and the page's content box."""
    grids = _outer_grids(page.grids)
    tables: list[Table] = []
    taken: set[int] = set()
    for g in grids:
        t, used = build_ruled_table(g, page.blocks, page.rgb, page.scale)
        tables.append(t)
        taken.update(id(b) for b in used)
    free = [b for b in page.blocks if id(b) not in taken and b.text.strip()]
    free = _split_label_values(free)

    rows = group_rows(free)
    consumed = [False] * len(rows)
    for (s, e, tbl) in detect_text_tables(rows, page.width - 14.0):
        for k in range(s, e + 1):
            consumed[k] = True
        tables.append(tbl)

    # content box for margins and alignment
    xs0, xs1, ys0, ys1 = [], [], [], []
    for b in free:
        xs0.append(b.bounds.x); xs1.append(b.bounds.right); ys0.append(b.bounds.y); ys1.append(b.bounds.bottom)
    for t in tables:
        xs0.append(t.bounds.x); xs1.append(t.bounds.right); ys0.append(t.bounds.y); ys1.append(t.bounds.bottom)
    if not xs0:
        return [], (36.0, 36.0, 36.0, 36.0)
    ml = min(max(min(xs0), 14.0), page.width / 3)
    mr = min(max(page.width - max(xs1), 14.0), page.width / 3)
    mt = min(max(min(ys0), 14.0), page.height / 3)
    mb = min(max(page.height - max(ys1), 14.0), page.height / 3)
    L_area, R_area = ml, page.width - mr

    free_rows = [r for r, used in zip(rows, consumed) if not used]
    lines_all = [make_line(r) for r in free_rows]
    Lp, Rp = _region(lines_all)
    Rp = max(Rp, min(R_area, Rp))

    # split the text rows around tables so paragraphs never run through one
    items: list[tuple[float, str, object]] = []
    for r, ln, used in zip(rows, [make_line(r) for r in rows], consumed):
        if not used:
            items.append((ln.top, "line", ln))
    for t in tables:
        items.append((t.top, "table", t))
    items.sort(key=lambda it: (it[0], 0 if it[1] == "table" else 1))

    elements: list = []
    buf: list[Line] = []

    def flush() -> None:
        if not buf:
            return
        paras = build_paragraphs(buf, L_area, Rp)
        _harmonize_lists(paras)
        _align(paras, L_area, R_area, page_cx=page.width / 2)
        _spacing(paras)
        # a hanging/indent measured against the text column, not the page area
        for p in paras:
            if p.align == "left" and p.kind != "list":
                p.left_indent = max(0.0, p.body_left - L_area)
                if p.left_indent < 0.6 * p.size:
                    p.left_indent = 0.0
            if p.kind == "list":
                p.left_indent = max(0.0, (p.wrap_left if p.wrap_left is not None else p.body_left) - L_area)
        elements.extend(paras)
        buf.clear()

    for _top, kind, obj in items:
        if kind == "line":
            buf.append(obj)  # type: ignore[arg-type]
        else:
            flush()
            t: Table = obj  # type: ignore[assignment]
            t.left_indent = max(0.0, t.bounds.x - L_area)
            elements.append(t)
    flush()

    # spacing around tables
    for i, el in enumerate(elements):
        if isinstance(el, Table):
            prev = elements[i - 1] if i > 0 else None
            if isinstance(prev, Para):
                gap = el.top - prev.bottom
                prev.space_after = min(max(0.0, gap - 2.0), 36.0)
        elif i > 0 and isinstance(elements[i - 1], Table):
            gap = el.top - elements[i - 1].bottom
            el.space_before = min(max(0.0, gap - 2.0), 36.0)
    if elements and isinstance(elements[0], Para):
        elements[0].space_before = 0.0
    return elements, (ml, mt, mr, mb)


def _outer_grids(grids: list[Grid]) -> list[Grid]:
    out = []
    for g in grids:
        inside = any(
            h is not g and h.bounds.x <= g.bounds.x + 1 and h.bounds.y <= g.bounds.y + 1
            and h.bounds.right >= g.bounds.right - 1 and h.bounds.bottom >= g.bounds.bottom - 1
            and h.bounds.w * h.bounds.h > g.bounds.w * g.bounds.h
            for h in grids
        )
        if not inside:
            out.append(g)
    return out


# --------------------------------------------------------------------------- document level

# "研 究 主 題：台灣北部…", "作者姓名：王大明", "關鍵詞：…": a short label, a colon, then the
# value. Set larger than the body on a cover page, but a field line, not a heading.
_LABEL_RE = re.compile(r"^\s*([^：:\n]{1,16}?)\s*[：:]\s*(\S.*)$")


def _is_label_line(text: str) -> bool:
    m = _LABEL_RE.match(text)
    if m is None:
        return False
    label = re.sub(r"\s+", "", m.group(1))
    return 1 <= len(label) <= 8 and not label.isdigit()


def _classify(pages: list[PageDoc]) -> None:
    free: list[Para] = [e for p in pages for e in p.elements if isinstance(e, Para)]
    pool = free or [q for p in pages for e in p.elements if isinstance(e, Table)
                    for c in e.cells for q in c.paras]
    if not pool:
        return
    weight: Counter = Counter()
    for q in pool:
        weight[round(q.size * 2) / 2] += len(q.text.strip())
    body = weight.most_common(1)[0][0]
    body_bold = sum(len(q.text) * q.bold_frac for q in pool) / max(1, sum(len(q.text) for q in pool))

    cands: list[Para] = []
    label_size: float | None = None  # size of the label line just before this paragraph
    for p in free:
        t = p.text.strip()
        is_label = _is_label_line(t)
        # a value that wrapped onto the next line ("…空間動線與時 / 間配置") belongs to it
        wrapped = label_size is not None and not is_label and abs(label_size - p.size) <= 0.5
        label_size = p.size if is_label else None
        if is_label or wrapped:
            continue
        if p.kind == "list" and not _HEAD_NUM_RE.match(p.text):
            continue
        n = len(t)
        wide = sum(_wide(ch) for ch in t) * 2 >= max(1, n)
        if n == 0 or n > (40 if wide else 90) or p.n_lines > 2 or p.tabs:
            continue
        if t[-1] in "。.;；,，、":
            continue
        big = p.size >= body * 1.14
        boldish = p.bold_frac >= 0.8 and p.size >= body * 0.95 and body_bold < 0.4 and p.n_lines == 1
        if p.kind == "list":
            # a numbered line is a heading only when it also stands out
            if not (big or boldish):
                continue
        if big or boldish:
            p.kind = "heading"
            cands.append(p)
    sizes = sorted({round(p.size * 2) / 2 for p in cands if p.size >= body * 1.14}, reverse=True)
    merged: list[float] = []
    for s in sizes:
        if not merged or merged[-1] - s > 0.75:
            merged.append(s)

    def rank_of(p: Para) -> int:
        if p.size >= body * 1.14 and merged:
            s = round(p.size * 2) / 2
            return 1 + min(range(len(merged)), key=lambda i: abs(merged[i] - s))
        return len(merged) + 1

    def depth_of(p: Para) -> int | None:
        m = re.match(r"^(\d{1,2}(?:\.\d{1,2})*)", p.text.strip())
        return m.group(1).count(".") + 1 if m else None

    base: dict[int, int] = {}
    for p in cands:
        d = depth_of(p)
        if d is not None:
            r = rank_of(p)
            base[r] = min(base.get(r, d), d)
    for p in cands:
        r = rank_of(p)
        d = depth_of(p)
        p.level = max(1, min(4, r + (d - base[r] if d is not None else 0)))
        if p.marker:  # "1.1<TAB>Title" -> "1.1 Title"
            for run in p.runs:
                if "\t" in run.text:
                    run.text = run.text.replace("\t", p.marker_sep, 1)
                    break
            p.text = "".join(r_.text for r_ in p.runs)
        p.marker = None
        p.first_line = 0.0
        if p.align == "left":
            p.left_indent = max(0.0, p.first_left - p.area_left)
            if p.left_indent < 0.6 * p.size:
                p.left_indent = 0.0
        # centred / right-aligned headings keep the indents _align gave them


# --------------------------------------------------------------------------- running headers / footers

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_PAGE_NUM_RE = re.compile(
    r"^(?P<pre>\s*(?:第|page|p\.|[-–—])?\s*)"
    r"(?P<num>\d{1,4}|[ivxlcdm]{1,8}|[IVXLCDM]{1,8})"
    r"(?P<post>\s*(?:頁|[-–—])?\s*(?:(?:/|of|共)\s*\d{1,4}\s*頁?)?\s*)$",
    re.I,
)
# fraction of the page height that counts as the header / footer band
_BAND = 0.12


def _roman_value(t: str) -> int | None:
    t = t.lower()
    total = 0
    for i, ch in enumerate(t):
        v = _ROMAN[ch]
        nxt = _ROMAN[t[i + 1]] if i + 1 < len(t) else 0
        total += -v if v < nxt else v
    # only canonical numerals ("iv", not "iiii"), and not ordinary words such as "mix"
    back = ""
    n = total
    for val, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                     (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            back += sym
            n -= val
    return total if back == t and 0 < total < 400 else None


def page_number_of(text: str) -> tuple[str, str, str, int] | None:
    """``"- 3 -"`` -> ("- ", "decimal", " -", 3); ``"ii"`` -> ("", "lowerRoman", "", 2)."""
    m = _PAGE_NUM_RE.match(text)
    if m is None:
        return None
    num = m.group("num")
    if num.isdigit():
        fmt, val = "decimal", int(num)
    else:
        v = _roman_value(num)
        if v is None or (num != num.lower() and num != num.upper()):
            return None
        fmt, val = ("lowerRoman" if num.islower() else "upperRoman"), v
    return m.group("pre").lstrip(), fmt, m.group("post").rstrip(), val


def _hf_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", "", text))


def _band_rows(pg: PageInput, top: bool) -> list[list[Block]]:
    """The rows at the very top (or bottom) of the page that sit inside the band."""
    rows = group_rows([b for b in pg.blocks if b.text.strip()])
    if len(rows) < 2:
        return []
    boxes = [g.bounds for g in pg.grids]

    def in_grid(row: list[Block]) -> bool:
        return any(b.bounds.x < gb.right and b.bounds.right > gb.x and b.bounds.y < gb.bottom and b.bounds.bottom > gb.y
                   for b in row for gb in boxes)

    out: list[list[Block]] = []
    seq = rows if top else rows[::-1]
    for r in seq[:2]:
        y0, y1 = _row_extent(r)
        inside = y1 <= pg.height * _BAND if top else y0 >= pg.height * (1 - _BAND)
        if not inside or in_grid(r) or len("".join(b.text for b in r)) > 90:
            break
        out.append(r)
    return out


def _hf_line(row: list[Block], width: float) -> HFLine:
    ln = make_line(row)
    cx = (ln.x0 + ln.x1) / 2
    if len(row) == 1 and abs(cx - width / 2) < 0.06 * width:
        align = "center"
    elif len(row) == 1 and ln.x0 > width * 0.5:
        align = "right"
    else:
        align = "left"
    hf = HFLine(ln.runs, ln.text, align, ln.top, ln.bottom, ln.x0, ln.x1)
    if len(row) > 1:  # "left part<TAB>right part": positions are fixed later against the margins
        hf.tabs = [(b.bounds.right if i == len(row) - 1 else b.bounds.x, "right" if i == len(row) - 1 else "left")
                   for i, b in enumerate(row) if i > 0]
    hf.page_num = page_number_of(ln.text) if len(row) == 1 else None
    return hf


def split_running(inputs: list[PageInput]) -> tuple[list[PageInput], dict[int, tuple[list[HFLine], list[HFLine]]]]:
    """Takes running headers, footers and page numbers out of the page bodies.

    A band row counts when it is a page number ("3", "ii", "- 3 -", "第 3 頁"), or when the
    same text (digits ignored) sits in the same band on at least two pages. Word gets them
    back as its own header and footer, so they do not end up as body paragraphs.
    """
    n = len(inputs)
    seen: dict[tuple[bool, str], int] = {}
    cands: dict[int, list[tuple[bool, list[Block]]]] = {}
    for pg in inputs:
        lst = []
        for top in (True, False):
            for r in _band_rows(pg, top):
                key = _hf_key("".join(b.text for b in r))
                seen[(top, key)] = seen.get((top, key), 0) + 1
                lst.append((top, r))
        cands[pg.page] = lst
    need = 2 if n <= 6 else max(2, int(0.3 * n))
    # running heads are set no larger than the body text; a page title in the same place on
    # every page ("Inspection plan 2", "Inspection plan 3") is set bigger and stays in the body
    weight: Counter = Counter()
    for pg in inputs:
        for b in pg.blocks:
            weight[round(b.style.size * 2) / 2] += len(b.text.strip())
    body = weight.most_common(1)[0][0] if weight else 10.5
    out_inputs: list[PageInput] = []
    hf: dict[int, tuple[list[HFLine], list[HFLine]]] = {}
    for pg in inputs:
        head: list[HFLine] = []
        foot: list[HFLine] = []
        taken: set[int] = set()
        for top, r in cands[pg.page]:
            text = "".join(b.text for b in r)
            if not (len(r) == 1 and page_number_of(text)):
                if seen[(top, _hf_key(text))] < need:
                    continue
                if max(b.style.size for b in r) > 1.05 * body:
                    continue
            (head if top else foot).append(_hf_line(r, pg.width))
            taken.update(id(b) for b in r)
        head.sort(key=lambda h: h.top)
        foot.sort(key=lambda h: h.top)
        hf[pg.page] = (head, foot)
        out_inputs.append(replace(pg, blocks=[b for b in pg.blocks if id(b) not in taken]) if taken else pg)
    return out_inputs, hf


def _row_texts(t: Table, r: int) -> list[str]:
    cells = sorted((c for c in t.cells if c.r == r), key=lambda c: c.c)
    return [re.sub(r"\s+", "", "".join(p.text for p in c.paras)) for c in cells]


def _continues(a: PageDoc, b: PageDoc) -> bool:
    """True when the last table of page ``a`` carries on as the first table of page ``b``."""
    if not a.elements or not b.elements:
        return False
    ta, tb = a.elements[-1], b.elements[0]
    if not (isinstance(ta, Table) and isinstance(tb, Table) and ta.ruled and tb.ruled):
        return False
    if abs(a.width - b.width) > 2 or abs(a.height - b.height) > 2:
        return False
    if len(ta.col_w) != len(tb.col_w) or any(abs(x - y) > max(3.0, 0.03 * x) for x, y in zip(ta.col_w, tb.col_w)):
        return False
    # it has to run into the foot of one page and start at the head of the next
    return ta.bottom >= a.height - a.margins[3] - 0.08 * a.height and tb.top <= b.margins[1] + 0.08 * b.height


def _merge_continued_tables(docs: list[PageDoc]) -> list[PageDoc]:
    """Joins a table the PDF broke at a page boundary back into one table.

    The continuation page's remaining content follows in the same Word section, so the
    table flows across the break by itself and a repeated header row becomes a real
    "repeat header" row instead of a second copy of the text.
    """
    out: list[PageDoc] = []
    for pg in docs:
        if out and _continues(out[-1], pg):
            host = out[-1]
            ta, tb = host.elements[-1], pg.elements[0]
            base = len(ta.row_h)
            rows_b = list(tb.row_h)
            cells_b = list(tb.cells)
            if base and tb.row_h and _row_texts(ta, 0) == _row_texts(tb, 0) and any(_row_texts(tb, 0)) \
                    and all(c.rs == 1 for c in cells_b if c.r == 0):
                cells_b = [c for c in cells_b if c.r != 0]
                for c in cells_b:
                    c.r -= 1
                rows_b = rows_b[1:]
                ta.header_rows = max(ta.header_rows, 1)
            for c in cells_b:
                c.r += base
            ta.cells.extend(cells_b)
            ta.row_h.extend(rows_b)
            host.elements.extend(pg.elements[1:])
            continue
        out.append(pg)
    return out


def structure_document(inputs: list[PageInput]) -> list[PageDoc]:
    inputs, hf = split_running(inputs)
    docs: list[PageDoc] = []
    for pg in inputs:
        els, margins = _page_chunks(pg)
        head, foot = hf.get(pg.page, ([], []))
        docs.append(PageDoc(pg.page, pg.width, pg.height, margins, els, head, foot))
    docs = _merge_continued_tables(docs)
    _classify(docs)
    return docs
