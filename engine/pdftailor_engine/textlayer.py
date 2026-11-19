"""Reads the real text layer of a page and turns it into editable lines.

Everything the editor needs to redraw a line "in the same voice" is available here: the
font each glyph was set in, its weight and italic angle, its size and its fill colour.
"""

from __future__ import annotations

import ctypes
import io
import math
import re
import unicodedata
from dataclasses import dataclass

import pypdfium2.raw as raw

from .fontnames import ParsedFont, decode_symbolic_char, is_private_use, parse_pdf_font_name
from .geometry import PageGeometry, Rect, UserRect
from .models import Block, Run, TextStyle
from .pdfio import PdfDoc

# FontDescriptor /Flags bits that matter here.
_FLAG_ITALIC = 0x40
_FLAG_FORCE_BOLD = 0x40000

# A gap wider than this many font sizes starts a new block (table cells, columns).
_BLOCK_SPLIT_EM = 2.2
# A gap wider than this many font sizes between glyphs reads as a space.
_SPACE_EM = 0.2
_CJK_SPACE_EM = 0.75
_ADV_SPACE_EM = 0.15
_CJK_ADV_SPACE_EM = 0.2


@dataclass
class Glyph:
    ch: str
    box: Rect  # display space
    size: float
    font_name: str
    parsed: ParsedFont
    weight: int
    italic_flag: bool
    bold_flag: bool
    color: tuple[int, int, int]
    invisible: bool
    upright: bool
    generated: bool
    unresolved_symbol: bool = False
    # horizontal extent of the glyph's advance (loose box), display space; the tight box of
    # a narrow glyph such as "1" leaves a gap that is not a space ("1 1 7" for 117)
    adv: tuple[float, float] | None = None
    # where the pen stood when the glyph was drawn (display x). Reading order follows this:
    # ink boxes of CJK glyphs and full-width punctuation overlap their neighbours.
    ox: float | None = None


@dataclass
class TextLayerStats:
    total: int = 0
    visible: int = 0
    invisible: int = 0
    skipped_rotated: int = 0
    unresolved_symbols: int = 0

    @property
    def kind(self) -> str:
        """``"none"``, ``"invisible"`` (an OCR layer hidden under a scan) or ``"visible"``."""
        if self.total == 0:
            return "none"
        if self.visible == 0:
            return "invisible"
        return "visible"


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
        or 0x3000 <= o <= 0x303F
        or 0x20000 <= o <= 0x2FFFF
    )


def _read_name(fn, handle) -> str:
    buf = ctypes.create_string_buffer(256)
    n = fn(handle, buf, len(buf))
    if n <= 0:
        return ""
    return buf.value.decode("utf-8", "replace")


# Names that say nothing about the face: "CIDFont+F1" (Microsoft Print to PDF), "F3",
# "TT1", "Font5". The real name is then read from the embedded font program.
_ANONYMOUS_FONT = re.compile(r"^(?:[A-Z]{6}\+)?(?:CIDFont\+)?(?:F|TT|T|C|Font)?\d+$|^CIDFont\+F\d+$", re.I)


def _embedded_font_name(font) -> str:
    """PostScript (or family) name from the embedded TrueType/OpenType ``name`` table."""
    try:
        size = ctypes.c_size_t(0)
        if not raw.FPDFFont_GetFontData(font, None, 0, ctypes.byref(size)) or size.value < 12:
            return ""
        buf = (ctypes.c_uint8 * size.value)()
        if not raw.FPDFFont_GetFontData(font, buf, size.value, ctypes.byref(size)):
            return ""
        from fontTools.ttLib import TTFont

        tt = TTFont(io.BytesIO(bytes(buf)), lazy=True, fontNumber=0)
        if "name" not in tt:
            return ""
        names = tt["name"]
        found = []
        for nid in (6, 4, 16, 1):  # PostScript name parses best ("MicrosoftJhengHeiBold")
            v = (names.getDebugName(nid) or "").strip()
            if v and not _ANONYMOUS_FONT.match(v):
                if parse_pdf_font_name(v).family:
                    return v
                found.append((nid, v))
        # no known family: the plain family name ("MS Gothic") beats a PostScript name
        # that splits badly ("MS-Gothic" -> "MS")
        for want in (16, 1, 4, 6):
            for nid, v in found:
                if nid == want:
                    return v
    except Exception:
        return ""
    return ""


def _render_scale(tp, idx: int) -> float:
    """How much the text and page matrices (Tm x CTM) scale the font size vertically.

    ``FPDFText_GetFontSize`` is the bare ``Tf`` operand. Producers that draw in device
    units — Microsoft Print to PDF sets ``0.75 0 0 -0.75 …`` and writes 16 for 12 pt type —
    or that use ``1 Tf`` with a scaled ``Tm`` would otherwise read 1.33x or 12x too big.
    """
    m = raw.FS_MATRIX()
    try:
        if not raw.FPDFText_GetMatrix(tp.raw, idx, ctypes.byref(m)):
            return 1.0
    except Exception:
        return 1.0
    s = math.hypot(m.c, m.d)
    return s if 1e-3 < s < 1e3 else 1.0


def read_glyphs(doc: PdfDoc, page_number: int) -> tuple[list[Glyph], TextLayerStats]:
    geom = doc.geometry(page_number)
    page = doc.pdfium[page_number - 1]
    tp = page.get_textpage()
    stats = TextLayerStats()
    glyphs: list[Glyph] = []
    font_cache: dict[int, tuple[str, str, int, bool, bool]] = {}
    base_dx, base_dy = geom.baseline_matrix()
    # pdfium reports a glyph's angle clockwise, i.e. the negative of the usual
    # counter-clockwise convention (measured: /Rotate 90 -> 270, /Rotate 270 -> 90).
    want_angle = -math.atan2(base_dy, base_dx)

    try:
        n = tp.count_chars()
        i = 0
        while i < n:
            code = raw.FPDFText_GetUnicode(tp.raw, i)
            step = 1
            if 0xD800 <= code <= 0xDBFF and i + 1 < n:  # surrogate pair
                low = raw.FPDFText_GetUnicode(tp.raw, i + 1)
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    step = 2
            idx = i
            i += step
            if code < 32 or code == 0xFFFE or code == 0xFFFF:
                continue  # line breaks and other controls pdfium injects
            generated = bool(raw.FPDFText_IsGenerated(tp.raw, idx))
            if generated:
                continue  # synthesised spaces: spacing is re-derived from real gaps

            left, bottom, right, top = tp.get_charbox(idx, loose=False)
            if right <= left or top <= bottom:
                if chr(code) != " ":
                    continue
            box = geom.user_rect_to_display(UserRect(left, bottom, right, top))
            adv = None
            try:
                ll, lb, lr, lt = tp.get_charbox(idx, loose=True)
                if lr > ll and lt > lb:
                    lbox = geom.user_rect_to_display(UserRect(ll, lb, lr, lt))
                    adv = (lbox.x, lbox.right)
            except Exception:
                adv = None

            size = float(raw.FPDFText_GetFontSize(tp.raw, idx)) * _render_scale(tp, idx)
            ox = None
            try:
                px, py = ctypes.c_double(0), ctypes.c_double(0)
                if raw.FPDFText_GetCharOrigin(tp.raw, idx, ctypes.byref(px), ctypes.byref(py)):
                    o = geom.user_rect_to_display(UserRect(px.value, py.value, px.value, py.value))
                    ox = o.x
            except Exception:
                ox = None
            angle = float(raw.FPDFText_GetCharAngle(tp.raw, idx))
            diff = abs((angle - want_angle + math.pi) % (2 * math.pi) - math.pi)
            upright = diff < math.radians(12)

            textobj = raw.FPDFText_GetTextObject(tp.raw, idx)
            invisible = False
            font_name, family_name, weight, bold_flag, italic_flag = "", "", 400, False, False
            if textobj:
                mode = raw.FPDFTextObj_GetTextRenderMode(textobj)
                invisible = mode == 3  # FPDF_TEXTRENDERMODE_INVISIBLE
                font = raw.FPDFTextObj_GetFont(textobj)
                key = ctypes.cast(font, ctypes.c_void_p).value or 0
                if key not in font_cache:
                    flags = raw.FPDFFont_GetFlags(font)
                    angle_i = ctypes.c_int(0)
                    has_angle = raw.FPDFFont_GetItalicAngle(font, ctypes.byref(angle_i))
                    base = _read_name(raw.FPDFFont_GetBaseFontName, font)
                    fam = _read_name(raw.FPDFFont_GetFamilyName, font)
                    if parse_pdf_font_name(base or fam).family is None:
                        real = _embedded_font_name(font)
                        if real and (parse_pdf_font_name(real).family or _ANONYMOUS_FONT.match(base or fam or "F1")):
                            base = real
                    w = int(raw.FPDFFont_GetWeight(font))
                    ital = bool(flags & _FLAG_ITALIC) or (bool(has_angle) and angle_i.value != 0)
                    # pdfium reports a made-up weight (4140 seen from Microsoft Print to PDF)
                    # when the descriptor has none; only a real 100-1000 value means anything
                    if not 100 <= w <= 1000:
                        w = 400
                    bold = bool(flags & _FLAG_FORCE_BOLD) or w >= 600
                    font_cache[key] = (base, fam, w, bold, ital)
                font_name, family_name, weight, bold_flag, italic_flag = font_cache[key]
                if mode == 2:  # fill + stroke: how Word prints bold for a face without a bold style
                    bold_flag = True

            r, g, b, a = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
            if raw.FPDFText_GetFillColor(tp.raw, idx, r, g, b, a):
                color = (int(r.value), int(g.value), int(b.value))
            else:
                color = (0, 0, 0)

            parsed = parse_pdf_font_name(font_name or family_name)
            ch = chr(code)
            unresolved = False
            if parsed.symbolic or is_private_use(ch):
                decoded = decode_symbolic_char(ch, parsed.family)
                unresolved = is_private_use(decoded)
                ch = decoded

            stats.total += 1
            if invisible:
                stats.invisible += 1
            else:
                stats.visible += 1
            if not upright:
                stats.skipped_rotated += 1
            if unresolved:
                stats.unresolved_symbols += 1

            glyphs.append(
                Glyph(
                    ch=ch,
                    box=box,
                    size=size,
                    font_name=font_name,
                    parsed=parsed,
                    weight=weight,
                    italic_flag=italic_flag,
                    bold_flag=bold_flag,
                    color=color,
                    invisible=invisible,
                    upright=upright,
                    generated=False,
                    unresolved_symbol=unresolved,
                    adv=adv,
                    ox=ox,
                )
            )
    finally:
        tp.close()
    return glyphs, stats


def glyph_style(g: Glyph) -> TextStyle:
    return TextStyle(
        family=g.parsed.family or (g.parsed.hint or None),
        size=g.size,
        bold=g.parsed.bold or g.bold_flag,
        italic=g.parsed.italic or g.italic_flag,
        color=g.color,
        source="font",
    )


def _style_key(s: TextStyle) -> tuple:
    return (s.family or "", s.bold, s.italic, s.color, round(s.size * 2) / 2)


def _cluster_lines(glyphs: list[Glyph]) -> list[list[Glyph]]:
    """Groups glyphs into visual lines by vertical position."""
    if not glyphs:
        return []
    sizes = sorted(g.size for g in glyphs if g.size > 0)
    typical = sizes[len(sizes) // 2] if sizes else 10.0
    ordered = sorted(glyphs, key=lambda g: g.box.cy)
    lines: list[list[Glyph]] = []
    line_cy = 0.0
    line_size = 0.0
    for g in ordered:
        # The tolerance follows the type size of the line itself: punctuation such as "、"
        # or "，" has its ink at the bottom of the em box, up to ~0.4 em from the line's
        # centre, and a 16 pt heading on a page of 10.5 pt text must not be judged by 10.5.
        tol = max(1.5, 0.5 * max(typical, g.size, line_size))
        if lines and abs(g.box.cy - line_cy) <= tol:
            lines[-1].append(g)
            line_cy = sum(x.box.cy for x in lines[-1]) / len(lines[-1])
            line_size = max(line_size, g.size)
        else:
            lines.append([g])
            line_cy = g.box.cy
            line_size = g.size
    return lines


def _split_segments(line: list[Glyph]) -> list[list[Glyph]]:
    """Splits a line at wide gaps so table cells and columns stay separate blocks."""
    # Order by where each glyph starts (its advance), not by its ink: a full-width "（"
    # or "「" has its ink at the right of its em box, so "（觀" sorted by ink reads "觀（".
    line = sorted(line, key=lambda g: g.ox if g.ox is not None else g.box.x)
    segments: list[list[Glyph]] = [[line[0]]]
    for prev, cur in zip(line, line[1:]):
        size = max(prev.size, cur.size, 1.0)
        gap = cur.box.x - prev.box.right
        small = max(min(prev.size, cur.size), 1.0)
        # a wide gap, or a clear gap where the type size changes (a note in 8 pt running
        # into the 11 pt value in the box beside it)
        if gap > _BLOCK_SPLIT_EM * size or (abs(prev.size - cur.size) > 0.15 * size and gap > 1.0 * small):
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


def _needs_space(prev: Glyph, cur: Glyph) -> bool:
    if prev.ch == " " or cur.ch == " ":
        return False
    size = max(prev.size, cur.size, 1.0)
    gap = cur.box.x - prev.box.right
    if is_cjk(prev.ch) and is_cjk(cur.ch):
        if prev.ox is not None and cur.ox is not None and unicodedata.east_asian_width(prev.ch) in "WF":
            # pen positions: a full-width glyph advances one em, anything more is a space
            # the producer drew as a move rather than a glyph ("第一章 緒論" from Word)
            return cur.ox - prev.ox - prev.size >= _CJK_ADV_SPACE_EM * size
        return gap >= _CJK_SPACE_EM * size
    if prev.adv is not None and cur.adv is not None and prev.adv[1] > prev.adv[0]:
        # between advances, a real space is ~0.25 em; kerning and rounding stay well below
        return cur.adv[0] - prev.adv[1] >= _ADV_SPACE_EM * size
    return gap >= _SPACE_EM * size


def build_blocks(glyphs: list[Glyph], page_number: int) -> list[Block]:
    usable = [g for g in glyphs if g.upright and not g.invisible]
    blocks: list[Block] = []
    next_id = 0
    for line in sorted(_cluster_lines(usable), key=lambda l: min(g.box.y for g in l)):
        for seg in _split_segments(line):
            text_parts: list[str] = []
            runs: list[Run] = []
            cx: list[tuple[float, float]] = []
            prev: Glyph | None = None
            for g in seg:
                piece = g.ch
                if prev is not None and _needs_space(prev, g):
                    piece = " " + piece
                    cx.append((prev.box.right, g.box.x))
                cx.extend([(g.box.x, g.box.right)] * len(g.ch))
                style = glyph_style(g)
                x0 = g.box.x
                if runs and _style_key(runs[-1].style) == _style_key(style):
                    runs[-1].text += piece
                    runs[-1].x1 = g.box.right
                else:
                    runs.append(Run(piece, style, x0, g.box.right))
                text_parts.append(piece)
                prev = g

            # Trim at the run level so block.text always equals the runs joined together.
            while runs and not runs[0].text.strip():
                runs.pop(0)
            while runs and not runs[-1].text.strip():
                runs.pop()
            if not runs:
                continue
            runs[0].text = runs[0].text.lstrip()
            runs[-1].text = runs[-1].text.rstrip()
            text = "".join(r.text for r in runs)
            if not text.strip():
                continue
            full = "".join(text_parts)
            lead = len(full) - len(full.lstrip())
            char_x = cx[lead:lead + len(text)] if len(cx) == len(full) and full.strip() == text else None
            left = min(g.box.x for g in seg)
            top = min(g.box.y for g in seg)
            right = max(g.box.right for g in seg)
            bottom = max(g.box.bottom for g in seg)

            # The dominant run (by character count) speaks for the whole line.
            weight: dict[tuple, int] = {}
            by_key: dict[tuple, TextStyle] = {}
            for r in runs:
                k = _style_key(r.style)
                weight[k] = weight.get(k, 0) + len(r.text.strip())
                by_key[k] = r.style
            dominant = by_key[max(weight, key=weight.get)]

            block = Block(
                id=next_id,
                page=page_number,
                text=text,
                bounds=Rect.from_bounds(left, top, right, bottom),
                source="text",
                style=dominant,
                runs=runs,
                char_x=char_x,
            )
            if any(g.unresolved_symbol for g in seg):
                block.notes.append("含無法對應的私用區符號，輸出時可能顯示成空白或方框。")
            blocks.append(block)
            next_id += 1
    return blocks


def count_text_stats(doc: PdfDoc, page_number: int) -> TextLayerStats:
    """Cheap classification of a page's text layer without building blocks."""
    _, stats = read_glyphs(doc, page_number)
    return stats
