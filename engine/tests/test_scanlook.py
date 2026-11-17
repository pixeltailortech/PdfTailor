"""Edits on a scanned page: one typeface per paragraph, and untouched characters keep their pixels."""
import numpy as np
import pytest

from scangen import Style
from scanpdf import PX_PER_PT, build_scan, write_scan_pdf
from pdftailor_engine.ocr.tesseract import find_tesseract
from pdftailor_engine.pdfio import PdfDoc
from pdftailor_engine.service import Session

import os

from scangen import NOTO_SERIF

pytestmark = pytest.mark.skipif(
    find_tesseract() is None or not os.path.isfile(NOTO_SERIF.format(w="Regular")),
    reason="needs tesseract and the Noto CJK fonts the synthetic scan is drawn with",
)

BODY = [
    "本校委由 ABC CATERING 辦理園遊會餐飲供應作業，依「活動場地借用管理辦",
    "法」第 12 條進行巡檢作業，對於應要求承辦廠商確實遵守事項進行查驗並",
    "回報結果，巡檢開始時間為 2025 年 3 月 3 日 Mail 通知廠商，廠商再將資",
    "料轉給外包團隊，最後廠商回信時間為 2025 年 4 月 7 日，以下內容為對方",
    "我方針對回覆內容逐項確認是否符合要求。",
    "ABC Catering 在處理場地設備與攤位相關資料(包含平面配置與使用時段)",
    "時，均採行嚴格的清潔控管與安全措施。僅限於執行供應業務所需之範圍進行",
    "使用與處理，並依據「最小用量原則」分派攤位電力。ABC 已建立完整的安全",
]


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    st = Style(family="noto-serif", size_px=int(12 * PX_PER_PT))
    items = [(60, 60 + 24 * i, t, st) for i, t in enumerate(BODY)]
    page, _ = build_scan(items, chart=False, degrade_kw=dict(blur=0.9, noise=6, jpeg=70, skew_deg=0.0))
    d = tmp_path_factory.mktemp("look")
    path = write_scan_pdf(str(d / "scan.pdf"), page)
    s = Session()
    s.open(path)
    return s, s.analyze(1)["blocks"], path, d


def test_one_family_for_the_whole_paragraph(scan):
    _s, blocks, _p, _d = scan
    fams = {b["style"]["family"] for b in blocks}
    assert fams == {"Noto Serif CJK TC"}, fams


def _edit(blocks, needle, old, new):
    b = next(b for b in blocks if needle in b["text"])
    e = dict(b)
    e["originalText"] = b["text"]
    e["text"] = b["text"].replace(old, new)
    assert e["text"] != b["text"]
    return e


def test_unchanged_characters_keep_their_pixels(scan):
    s, blocks, path, d = scan
    e = _edit(blocks, "時，均採行", "嚴格的", "完整的")  # same width: nothing moves
    out = str(d / "same.pdf")
    s.export([e], out, audit=False)
    a = PdfDoc(path).render(1, 300 / 72).astype(int)
    b = PdfDoc(out).render(1, 300 / 72).astype(int)
    x0, y0, w, h = e["bounds"]
    sc = 300 / 72
    row = slice(int(y0 * sc), int((y0 + h) * sc))
    changed = np.nonzero((np.abs(a - b).max(axis=2) > 30)[row].any(axis=0))[0]
    assert len(changed), "the edit left no trace at all"
    # the change is three characters wide (plus a little); the rest of the line is untouched
    assert changed.max() - changed.min() < 4.2 * 12 * sc
    assert changed.min() > (x0 + 3 * 12) * sc


def test_tail_moves_and_text_is_searchable(scan):
    import pypdfium2 as pdfium

    s, blocks, path, d = scan
    e = _edit(blocks, "我方針對", "符合要求", "符合本公司之要求")
    out = str(d / "longer.pdf")
    s.export([e], out, audit=False)
    text = pdfium.PdfDocument(out)[0].get_textpage().get_text_range()
    assert "符合本公司之要求" in text
    a = PdfDoc(path).render(1, 300 / 72).astype(int)
    b = PdfDoc(out).render(1, 300 / 72).astype(int)
    x0, y0, w, h = e["bounds"]
    sc = 300 / 72
    row = slice(int(y0 * sc), int((y0 + h) * sc))

    def ink_right(img):
        cols = np.nonzero((img[row].mean(axis=2) < 140).any(axis=0))[0]
        return cols.max()

    grew = (ink_right(b) - ink_right(a)) / sc / 12  # in ems
    assert 3.5 <= grew <= 4.6, grew  # four characters were added
    # the start of the line was not redrawn
    left = slice(int(x0 * sc), int((x0 + 8 * 12) * sc))
    assert np.abs(a - b).max(axis=2)[row, left].max() <= 30


def test_second_export_of_a_session_has_no_missing_glyphs(scan):
    """Each export subsets the font afresh; the second one must not reuse the first subset."""
    import pypdfium2 as pdfium

    s, blocks, _path, d = scan
    for i, (needle, old, new) in enumerate([("時，均採行", "嚴格的", "完整的"), ("使用與處理", "分派", "核發")]):
        e = _edit(blocks, needle, old, new)
        e["styleDirty"] = True  # forces the vector path, where the font subset is what draws
        out = str(d / f"twice{i}.pdf")
        s.export([e], out, audit=False)
        text = pdfium.PdfDocument(out)[0].get_textpage().get_text_range()
        assert e["text"].replace(" ", "") in text.replace(" ", ""), text


def test_word_paragraph_gets_one_family_and_size():
    from pdftailor_engine.docstruct import _unify_scanned
    from pdftailor_engine.models import Run, TextStyle

    m = lambda fam, size, src="measured": TextStyle(family=fam, size=size, source=src)
    runs = [Run("第一行的中文內容很長很長", m("DFKai-SB", 12.0)), Run("第二行", m("PMingLiU", 11.5)),
            Run("第三行也是中文內容", m("DFKai-SB", 12.0)), Run("大標", m("DFKai-SB", 20.0))]
    _unify_scanned(runs)
    assert {r.style.family for r in runs} == {"DFKai-SB"}
    assert [r.style.size for r in runs] == [12.0, 12.0, 12.0, 20.0]
    real = [Run("文字層的字", m("PMingLiU", 11.0, "font")), Run("另一種字", m("DFKai-SB", 12.0, "font"))]
    _unify_scanned(real)
    assert [r.style.family for r in real] == ["PMingLiU", "DFKai-SB"]


def test_group_decides_together():
    from pdftailor_engine.fontmatch import FontScores, settle_families

    f = lambda w, a, b: FontScores(w, 12.0, {"A": a, "B": b}, {"A": 1.0, "B": 1.0})
    body = [f(30, 0.80, 0.70), f(30, 0.74, 0.76), f(30, 0.82, 0.71), f(8, 0.60, 0.66)]
    heading = [f(12, 0.70, 0.74)]  # a short line that leans the other way on its own
    fam = settle_families({"body": body, "heading": heading})
    assert fam == {"body": "A", "heading": "A"}
    other = [f(40, 0.5, 0.9), f(40, 0.5, 0.9), f(40, 0.5, 0.9)]  # a long passage truly in B keeps B
    assert settle_families({"body": body, "quote": other})["quote"] == "B"


def test_latin_companion_font_is_used_and_survives_the_shell():
    from pdftailor_engine.fonts import default_index
    from pdftailor_engine.models import TextStyle

    fonts = default_index()
    if not (fonts.has_family("Noto Serif CJK TC") and fonts.has_family("Liberation Serif")):
        pytest.skip("fonts not installed")
    st = TextStyle(family="Noto Serif CJK TC", latin_family="Liberation Serif", size=12)
    segs = fonts.resolve(st, "符合 GDPR 規範").segments
    by = {seg.text.strip(): seg.face.family for seg in segs}
    assert by["GDPR"] == "Liberation Serif" and by["符合"] == "Noto Serif CJK TC"
    plain = fonts.resolve(st.with_(latin_family=None), "符合 GDPR 規範").segments
    assert {seg.face.family for seg in plain} == {"Noto Serif CJK TC"}
    assert TextStyle.from_json(st.to_json()).latin_family == "Liberation Serif"


def test_double_spaced_lines_stay_one_paragraph():
    from pdftailor_engine.docstruct import Line, _Acc, _starts_new, _usual_pitch

    def line(i, pitch):
        ln = Line.__new__(Line)
        ln.__dict__.update(text="一二三四五六七八九十" * 3, size=12.0, x0=60.0, x1=500.0, bold_frac=0.0, tab_before=[], parts=[(60.0, 500.0)], runs=[])
        ln.__dict__["top"], ln.__dict__["bottom"] = 100 + i * pitch, 112 + i * pitch
        return ln

    for pitch, merged in ((16.0, True), (24.0, True), (28.0, True)):
        lines = [line(i, pitch) for i in range(5)]
        usual = _usual_pitch(lines)
        assert (not _starts_new(_Acc([lines[0]], None), lines[1], 500.0, usual)) is merged, pitch
    # a real paragraph gap inside double-spaced text still splits
    lines = [line(i, 24.0) for i in range(5)]
    gap = line(0, 0)
    gap.__dict__["top"], gap.__dict__["bottom"] = lines[-1].top + 40, lines[-1].bottom + 40
    assert _starts_new(_Acc([lines[-1]], None), gap, 500.0, _usual_pitch(lines))
