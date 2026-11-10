"""修改紀錄：輸出後旁邊有 .audit.json / .audit.csv，原檔雜湊不變，內容可稽核。"""
import csv
import hashlib
import json
import os

import pytest

from fixtures import TextItem, make_text_pdf
from pdftailor_engine.audit import audit_paths, sha256_file
from pdftailor_engine.fonts import FontIndex
from pdftailor_engine.service import EngineError, Session

from test_server import Client


@pytest.fixture(scope="module")
def fonts(tmp_path_factory):
    return FontIndex.scan(cache_path=str(tmp_path_factory.mktemp("fi") / "fi.json"))


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _session(tmp_path, fonts):
    src = make_text_pdf(
        str(tmp_path / "src.pdf"),
        [
            TextItem(72, 100, "Invoice total 1200", size=14),
            TextItem(72, 160, "Keep this line", size=12),
            TextItem(72, 220, "Third line here", size=12),
        ],
    )
    s = Session(fonts=fonts, workdir=str(tmp_path / "work"))
    s.open(src)
    blocks = s.analyze(1)["blocks"]
    return s, src, blocks


def _edits(blocks, **by_text):
    """Every block as the shell sends it back; ``by_text`` maps a text fragment to changes."""
    out = []
    for b in blocks:
        e = dict(b, originalText=b["text"], originalBounds=b["bounds"])
        for frag, change in by_text.items():
            if frag in b["text"]:
                e.update(change)
        out.append(e)
    return out


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_files_written_and_hashes(tmp_path, fonts):
    s, src, blocks = _session(tmp_path, fonts)
    before = _sha(src)
    out = str(tmp_path / "out.pdf")
    r = s.export(_edits(blocks, Invoice={"text": "Invoice total 1500"}), out)
    assert "auditError" not in r
    j, c = r["audit"]["json"], r["audit"]["csv"]
    assert (j, c) == audit_paths(out) and os.path.isfile(j) and os.path.isfile(c)
    a = _load(j)
    assert a["schema"] == 1 and a["action"] == "edit-text" and a["includeText"] is True
    assert a["tool"]["name"] == "PdfTailor"
    assert a["operator"].keys() == {"user", "machine"}
    assert a["createdAt"][4] == "-" and ("+" in a["createdAt"] or "Z" in a["createdAt"])
    # hashes are the real ones, and the original is untouched by the export
    assert a["source"]["sha256"] == before == _sha(src) == sha256_file(src)
    assert a["output"]["sha256"] == _sha(out)
    assert a["source"]["bytes"] == os.path.getsize(src) and a["output"]["bytes"] == os.path.getsize(out)
    assert a["source"]["pageCount"] == 1 and a["output"]["pageCount"] == 1
    assert a["source"]["unchanged"] is True
    assert a["verify"]["ok"] is True
    # no half-written leftovers
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_only_modified_lines_are_recorded(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    r = s.export(_edits(blocks, Invoice={"text": "Invoice total 1500"}), str(tmp_path / "o.pdf"))
    ch = _load(r["audit"]["json"])["changes"]
    assert len(ch) == 1  # the other two lines were sent back unchanged
    c = ch[0]
    assert c["kinds"] == ["text"] and c["page"] == 1
    orig = blocks[0]["text"]  # (Helvetica is not embedded: pdfium may add a space inside "1200")
    assert orig.startswith("Invoice total")
    assert c["originalText"] == orig and c["newText"] == "Invoice total 1500"
    assert c["originalLength"] == len(orig) and c["newLength"] == 18
    assert len(c["originalBounds"]) == 4 and c["style"]["size"] > 0 and len(c["style"]["color"]) == 3


def test_kinds(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)

    def kinds(**by_text):
        r = s.export(_edits(blocks, **by_text), str(tmp_path / "k.pdf"))
        return [c["kinds"] for c in _load(r["audit"]["json"])["changes"]]

    assert kinds(Invoice={"text": ""}) == [["delete"]]
    assert kinds(Invoice={"text": "   "}) == [["delete"]]
    assert kinds(Invoice={"styleDirty": True}) == [["style"]]
    x, y, w, h = blocks[0]["bounds"]
    assert kinds(Invoice={"bounds": [x + 20, y, w, h]}) == [["move"]]
    assert kinds(Invoice={"text": "Invoice total 9", "styleDirty": True, "bounds": [x + 5, y, w, h]}) == [["text", "style", "move"]]


def test_without_text(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    r = s.export(_edits(blocks, Invoice={"text": "Invoice total 1500"}), str(tmp_path / "o.pdf"), audit_text=False)
    j, c = r["audit"]["json"], r["audit"]["csv"]
    a = _load(j)
    assert a["includeText"] is False
    ch = a["changes"][0]
    assert "originalText" not in ch and "newText" not in ch
    assert ch["originalLength"] == len(blocks[0]["text"]) and ch["newLength"] == 18
    for p in (j, c):
        raw = open(p, encoding="utf-8-sig").read()
        assert "Invoice total" not in raw  # the customer's text is not in the record
    row = _csv_rows(c)[1]
    assert row[3] == "" and row[4] == "" and row[5] == str(len(blocks[0]["text"])) and row[6] == "18"


def test_csv(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    r = s.export(_edits(blocks, Invoice={"text": "=1+1", "styleDirty": True}), str(tmp_path / "o.pdf"))
    raw = open(r["audit"]["csv"], "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM: Excel shows Chinese correctly
    rows = _csv_rows(r["audit"]["csv"])
    assert rows[0] == ["序號", "頁碼", "動作類型", "原文", "新文", "原字數", "新字數", "原位置", "新位置", "字型", "字級", "粗體", "斜體", "顏色"]
    row = rows[1]
    assert row[0] == "1" and row[1] == "1" and row[2] == "修改文字、修改樣式"
    assert row[3] == blocks[0]["text"]
    assert row[4] == "'=1+1"  # not a formula when opened in Excel
    assert _load(r["audit"]["json"])["changes"][0]["newText"] == "=1+1"  # the JSON keeps the truth


def test_chinese_text_survives(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    r = s.export(_edits(blocks, Invoice={"text": "金額 一百五十"}), str(tmp_path / "o.pdf"))
    assert _load(r["audit"]["json"])["changes"][0]["newText"] == "金額 一百五十"
    assert _csv_rows(r["audit"]["csv"])[1][4] == "金額 一百五十"


def test_never_overwrite_original_and_no_record(tmp_path, fonts):
    s, src, blocks = _session(tmp_path, fonts)
    before = _sha(src)
    with pytest.raises(EngineError, match="不能覆蓋原始檔"):
        s.export(_edits(blocks, Invoice={"text": "x"}), src)
    assert _sha(src) == before
    assert not [f for f in os.listdir(tmp_path) if ".audit." in f]


def test_audit_can_be_turned_off(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    r = s.export(_edits(blocks, Invoice={"text": "x"}), str(tmp_path / "o.pdf"), audit=False)
    assert "audit" not in r and "auditError" not in r
    assert not [f for f in os.listdir(tmp_path) if ".audit." in f]


def test_audit_failure_does_not_fail_the_export(tmp_path, fonts):
    s, _, blocks = _session(tmp_path, fonts)
    out = str(tmp_path / "o.pdf")
    os.makedirs(audit_paths(out)[0])  # a folder where the record should go
    r = s.export(_edits(blocks, Invoice={"text": "Invoice total 1500"}), out)
    assert os.path.isfile(out) and r["report"] is not None  # the PDF is there
    assert "audit" not in r and r["auditError"]  # and the shell is told the record failed
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_subprocess_protocol(tmp_path):
    src = make_text_pdf(str(tmp_path / "a.pdf"), [TextItem(72, 100, "Invoice total 1200", size=14)])
    c = Client(tmp_path)
    try:
        assert c.call(1, "open", path=src)["ok"]
        b = c.call(2, "analyze", page=1)["result"]["blocks"][0]
        edit = dict(b, originalText=b["text"], originalBounds=b["bounds"], text="Invoice total 3400")
        out = str(tmp_path / "out.pdf")
        r = c.call(3, "export", edits=[edit], out=out)
        assert r["ok"] and os.path.isfile(r["result"]["audit"]["json"]) and os.path.isfile(r["result"]["audit"]["csv"])
        out2 = str(tmp_path / "out2.pdf")
        r = c.call(4, "export", edits=[edit], out=out2, audit=False)
        assert r["ok"] and "audit" not in r["result"]
        assert not os.path.exists(audit_paths(out2)[0])
        out3 = str(tmp_path / "out3.pdf")
        r = c.call(5, "export", edits=[edit], out=out3, auditText=False)
        assert "originalText" not in _load(r["result"]["audit"]["json"])["changes"][0]
    finally:
        c.close()
