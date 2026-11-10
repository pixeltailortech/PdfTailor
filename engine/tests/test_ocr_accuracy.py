"""Regressions found on a real scanned offer letter (a ruled form, scanned slightly askew)
and a web-generated boarding pass. Each test pins one cause of a wrong or mangled line."""

import cv2
import numpy as np
import pytest

from fixtures import TextItem, make_text_pdf
from pdftailor_engine.models import Block, Run, TextStyle
from pdftailor_engine.geometry import Rect
from pdftailor_engine.ocr.base import OcrLine, OcrWord
from pdftailor_engine.ocr.tesseract import TesseractOcr, join_chars
from pdftailor_engine.pdfio import PdfDoc
from pdftailor_engine.scanpage import _dedupe, _harmonize_markers, _split_at_cells, cjk_punctuation
from pdftailor_engine.tables import erase_rules, find_grids
from pdftailor_engine.textlayer import build_blocks, read_glyphs

HOCR = """<?xml version="1.0" encoding="UTF-8"?>
<html><body><div class='ocr_page'>
 <span class='ocr_line' id='line_1_1' title="bbox 100 10 400 40">
  <span class='ocrx_word' id='word_1_1' title='bbox 100 10 160 40; x_wconf 95'>
   <span class='ocrx_cinfo' title='x_bboxes 100 10 130 40; x_conf 99'>報</span>
   <span class='ocrx_cinfo' title='x_bboxes 130 10 400 40; x_conf 98'>到</span>
  </span>
  <span class='ocrx_word' id='word_1_2' title='bbox 200 10 260 40; x_wconf 90'>
   <span class='ocrx_cinfo' title='x_bboxes 200 10 215 40; x_conf 90'>A</span>
   <span class='ocrx_cinfo' title='x_bboxes 216 10 230 40; x_conf 91'>&amp;</span>
  </span>
  <span class='ocrx_word' id='word_1_3' title='bbox 240 10 260 40; x_wconf 90'>
   <span class='ocrx_cinfo' title='x_bboxes 240 10 260 40; x_conf 90'>B</span>
  </span>
 </span>
</div></body></html>"""


def test_hocr_gives_a_box_per_character_and_clamps_runaway_widths():
    [line] = TesseractOcr._parse_lines(HOCR)
    assert line.text == "報到A& B"
    assert [w.text for w in line.words] == ["報", "到", "A", "&", "B"]
    # 到 was reported 270 px wide on a 30 px tall line: cut to a character's width
    assert line.words[1].w <= 1.3 * 30 + 1
    assert [w.start for w in line.words] == [True, False, True, False, True]


def _line(chars: list[tuple[str, float]], y: float = 100, h: float = 30) -> OcrLine:
    words = [OcrWord(t, x, y, 28, h, 95.0, i == 0) for i, (t, x) in enumerate(chars)]
    return OcrLine(join_chars(words), words[0].x, y, words[-1].x + 28 - words[0].x, h, 95.0, words)


def test_a_line_running_across_table_rules_is_cut_at_them():
    # "報到|110年|報到" with drawn rules at x=90 and x=230; the | marks are the rules themselves
    line = _line([("報", 0), ("到", 30), ("|", 80), ("1", 100), ("1", 130), ("0", 160), ("年", 190),
                  ("|", 225), ("報", 250), ("到", 280)])
    rules = [(90.0, 50.0, 200.0), (232.0, 50.0, 200.0)]
    parts = _split_at_cells([line], rules)
    assert [p.text for p in parts] == ["報到", "110年", "報到"]


def test_a_missing_rule_does_not_cut():
    line = _line([("受", 0), ("文", 30), ("者", 60)])
    assert [p.text for p in _split_at_cells([line], [(45.0, 0.0, 50.0)])] == ["受文者"]  # rule ends above the line


def test_cjk_punctuation_becomes_full_width():
    assert cjk_punctuation("台端至本公司服務, 錄用相關事項如下:") == "台端至本公司服務，錄用相關事項如下："
    assert cjk_punctuation("含伙食津貼$2,400 ,未來") == "含伙食津貼$2,400，未來"
    assert cjk_punctuation("Total: 3, 4") == "Total: 3, 4"
    assert cjk_punctuation("電話: (02)2719-6678") == "電話：(02)2719-6678"


def _block(i: int, text: str, x: float, y: float) -> Block:
    st = TextStyle(family=None, size=10.0)
    r = Rect(x, y, 10.0 * len(text), 12.0)
    return Block(id=i, page=1, text=text, bounds=r, source="ocr", style=st, runs=[Run(text, st, r.x, r.right)])


def test_list_markers_in_one_column_are_harmonised():
    blocks = [
        _block(0, "※身分證影本3份", 100, 100),
        _block(1, "K彩色二吋照片2張", 100, 115),
        _block(2, "※個人信用報告正本", 100, 130),
        _block(3, "HK各項專業資格", 101, 145),
        _block(4, "※最高學歷畢業證書", 100, 160),
        _block(5, "際", 100, 175),
        _block(6, "凱基商業銀行", 118, 175),
        _block(7, "K線圖", 300, 175),  # another column: left alone
    ]
    out = [b.text for b in _harmonize_markers(blocks)]
    assert out == ["※身分證影本3份", "※彩色二吋照片2張", "※個人信用報告正本", "※各項專業資格",
                   "※最高學歷畢業證書", "※凱基商業銀行", "K線圖"]


def test_dedupe_keeps_a_small_line_under_a_tall_title():
    title = OcrLine("中國人壽保險股份有限公司", 0, 0, 300, 32, 98)
    sub = OcrLine("CHINA LIFE INSURANCE CO,LTD", 40, 24, 160, 14, 99)
    assert {l.text for l in _dedupe([title, sub])} == {title.text, sub.text}
    again = OcrLine("中國人壽保險股份有限公同", 2, 1, 296, 31, 90)
    assert len(_dedupe([title, again])) == 1


def _skewed_form(angle: float = 0.4) -> np.ndarray:
    """A 3×2 ruled table with heavy two-stroke 'characters' in its cells, turned a little."""
    s = 300 / 72
    W, H = int(595 * s), int(400 * s)
    img = np.full((H, W, 3), 255, np.uint8)
    xs = [60, 200, 540]
    ys = [60, 140, 220, 300]
    t = 3
    for x in xs:
        cv2.line(img, (int(x * s), int(ys[0] * s)), (int(x * s), int(ys[-1] * s)), (0, 0, 0), t)
    for y in ys:
        cv2.line(img, (int(xs[0] * s), int(y * s)), (int(xs[-1] * s), int(y * s)), (0, 0, 0), t)
    # bold 14 pt "glyphs": vertical strokes as long as a short rule, with crossbars
    for cx in range(230, 520, 24):
        for cy in (80, 160, 240):
            x0, y0 = int(cx * s), int(cy * s)
            cv2.rectangle(img, (x0, y0), (x0 + 8, y0 + int(14 * s)), (0, 0, 0), -1)
            cv2.rectangle(img, (x0 + 30, y0), (x0 + 38, y0 + int(14 * s)), (0, 0, 0), -1)
            cv2.rectangle(img, (x0, y0 + 20), (x0 + 38, y0 + 26), (0, 0, 0), -1)
    m = cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (W, H), borderValue=(255, 255, 255))


def test_skewed_scan_table_is_found_without_letter_strokes_as_columns():
    s = 300 / 72
    grids = find_grids(_skewed_form(), s)
    big = max(grids, key=lambda g: g.bounds.w * g.bounds.h)
    assert (big.rows, big.cols) == (3, 2), (big.rows, big.cols, big.xs)


def test_erase_rules_removes_the_rules_but_not_the_letters():
    s = 300 / 72
    rgb = _skewed_form(0.0)
    grids = find_grids(rgb, s)
    clean = erase_rules(rgb, grids, s)
    x = int(60 * s)
    assert rgb[int(100 * s), x - 3:x + 4].min() < 100  # a rule was there
    assert clean[int(100 * s), x - 3:x + 4].min() > 200  # and is gone
    glyph = (slice(int(80 * s), int(94 * s)), slice(int(230 * s), int(230 * s) + 8))
    assert clean[glyph].mean() < 60  # letters stay


def test_digits_are_not_spaced_apart(tmp_path):
    """A narrow glyph's tight box ("1") left a gap that was taken for a space: "1 1 7"."""
    path = str(tmp_path / "bp.pdf")
    make_text_pdf(path, [TextItem(72, 100, "Security nb: 117 - Ticket: 189210584655801", size=8),
                         TextItem(72, 140, "JX134957476", size=11)])
    doc = PdfDoc(path)
    glyphs, _ = read_glyphs(doc, 1)
    texts = [b.text for b in build_blocks(glyphs, 1)]
    assert "Security nb: 117 - Ticket: 189210584655801" in texts
    assert "JX134957476" in texts


def test_a_size_change_with_a_gap_starts_a_new_block(tmp_path):
    path = str(tmp_path / "two.pdf")
    make_text_pdf(path, [TextItem(72, 100, "documents for the journey", size=8),
                         TextItem(190, 100, "JX134957476", size=11)])
    doc = PdfDoc(path)
    glyphs, _ = read_glyphs(doc, 1)
    texts = [b.text for b in build_blocks(glyphs, 1)]
    assert texts == ["documents for the journey", "JX134957476"]
