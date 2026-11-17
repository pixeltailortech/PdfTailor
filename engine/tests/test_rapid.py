"""The PP-OCRv5 adapter, without the models: how detector segments become lines."""
from pdftailor_engine.ocr.rapid import _char_words, find_models, merge_segments


def test_segments_of_one_row_join_and_columns_stay_apart():
    segs = [
        ("旅客請依", 100, 100, 150, 30, 95.0),
        ("ABC DATA", 258, 101, 120, 29, 90.0),
        ("指示登機", 386, 100, 150, 30, 96.0),
        ("備註", 900, 100, 60, 30, 97.0),  # far to the right: another column
        ("第二行文字", 100, 150, 150, 30, 93.0),
    ]
    lines = merge_segments(segs)
    assert [l.text for l in lines] == ["旅客請依ABC DATA指示登機", "備註", "第二行文字"]
    first = lines[0]
    assert first.x == 100 and abs(first.x + first.w - 536) < 1e-6
    assert "".join(w.text for w in first.words) == "旅客請依ABCDATA指示登機"


def test_latin_segments_get_a_space_between_them():
    lines = merge_segments([("ABC Data", 10, 10, 80, 20, 90.0), ("Services", 96, 10, 80, 20, 90.0)])
    assert [l.text for l in lines] == ["ABC Data Services"]


def test_character_boxes_follow_character_widths():
    ws = _char_words("中A 文", 0, 0, 285, 30, 90.0)
    assert [w.text for w in ws] == ["中", "A", "文"]
    assert ws[0].w > 1.7 * ws[1].w  # a full-width character is about twice a letter
    assert ws[0].start and ws[2].start


def test_best_models_are_picked(tmp_path, monkeypatch):
    from pdftailor_engine.ocr.rapid import describe

    names = ["PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx", "ch_PP-OCRv5_det_server.onnx",
             "ch_PP-OCRv5_rec_server.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    monkeypatch.setenv("PDFTAILOR_OCR_MODELS", str(tmp_path))
    m = find_models()
    assert m["det"].endswith("ch_PP-OCRv5_det_server.onnx") and m["rec"].endswith("ch_PP-OCRv5_rec_server.onnx")
    for n in ("PP-OCRv6_det_medium.onnx", "PP-OCRv6_rec_medium.onnx"):
        (tmp_path / n).write_bytes(b"x")
    m = find_models()
    assert m["det"].endswith("PP-OCRv6_det_medium.onnx") and m["rec"].endswith("PP-OCRv6_rec_medium.onnx")
    assert describe(m["rec"]) == ("PP-OCRv6", "medium") and describe("ch_PP-OCRv5_det_server.onnx") == ("PP-OCRv5", "server")


def test_detection_alone_is_not_enough(tmp_path, monkeypatch):
    (tmp_path / "ch_PP-OCRv5_det_server.onnx").write_bytes(b"x")
    monkeypatch.setenv("PDFTAILOR_OCR_MODELS", str(tmp_path))
    monkeypatch.setattr("pdftailor_engine.ocr.rapid.model_dirs", lambda: [str(tmp_path)])
    assert find_models() == {}


def test_stray_simplified_characters_become_traditional_only_on_traditional_pages():
    from pdftailor_engine.ocr.rapid import is_traditional_page, to_traditional

    page = ["旅客之行李託運資料、航班組合明细與往来紀錄", "應與其他班次之資料有明確區隔"]
    assert is_traditional_page(page)
    assert to_traditional(page[0]) == "旅客之行李託運資料、航班組合明細與往來紀錄"
    assert to_traditional("公里 計劃 范先生 後面") == "公里 計劃 范先生 後面"  # ambiguous characters are left alone
    assert not is_traditional_page(["旅客之行李托运资料、航班组合明细"])  # a simplified page stays as printed
    assert not is_traditional_page(["ABC Data Services does not typically need"])
