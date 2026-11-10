"""整理頁面：旋轉、刪除、重排；不動頁面內容、刪除頁的資料不殘留、原檔不變。"""
import csv
import hashlib
import json
import os
import re

import pikepdf
import pytest

from fixtures import make_image_pdf, make_multipage_pdf, page_bytes
from pdftailor_engine.audit import audit_paths
from pdftailor_engine.pdfio import PdfDoc
from pdftailor_engine.service import EngineError, Session

from test_server import Client


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _marks(path):
    """Which UNIQUEMARK each page of ``path`` carries, in page order."""
    out = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            m = re.search(rb"UNIQUEMARK(\d+)", page_bytes(page))
            out.append(int(m.group(1)) if m else None)
    return out


def _session(tmp_path, n=3, **kw):
    src = make_multipage_pdf(str(tmp_path / "src.pdf"), n, **kw)
    s = Session(workdir=str(tmp_path / "work"))
    s.open(src)
    return s, src


def _plan(*pairs):
    return [{"src": a, "rotate": b} for a, b in pairs]


def test_reorder(tmp_path):
    s, src = _session(tmp_path)
    out = str(tmp_path / "out.pdf")
    r = s.arrange(_plan((3, 0), (1, 0), (2, 0)), out)
    assert r["pageCount"] == 3 and r["deleted"] == [] and r["sha256"] == _sha(out)
    assert _marks(out) == [3, 1, 2]
    assert not os.path.exists(out + ".tmp")


def test_delete(tmp_path):
    s, src = _session(tmp_path)
    out = str(tmp_path / "out.pdf")
    r = s.arrange(_plan((1, 0), (3, 0)), out)
    assert r["pageCount"] == 2 and r["deleted"] == [2]
    assert _marks(out) == [1, 3]


def test_rotate(tmp_path):
    s, src = _session(tmp_path, n=3)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[2].obj.Rotate = 270  # an already rotated page: 270 + 90 must wrap to 0
        pdf.save(src)
    s.open(src)
    out = str(tmp_path / "out.pdf")
    s.arrange(_plan((1, 90), (2, 0), (3, 90)), out)
    a, b = PdfDoc(src), PdfDoc(out)
    try:
        assert b.pages[0].geometry.rotation == 90
        assert (b.pages[0].geometry.display_w, b.pages[0].geometry.display_h) == (a.pages[0].geometry.display_h, a.pages[0].geometry.display_w)
        assert b.pages[1].geometry.rotation == 0
        assert b.pages[2].geometry.rotation == 0  # (270 + 90) % 360
        # what is drawn is rotated too, not just a flag: pdfium's page size agrees
        assert abs(b.pages[0].pdfium_size[0] - a.pages[0].pdfium_size[1]) < 1.0
        assert not b.pages[0].size_mismatch
    finally:
        a.close()
        b.close()


def test_kept_scan_streams_are_byte_identical(tmp_path):
    src = make_image_pdf(str(tmp_path / "scan.pdf"), 3)
    s = Session(workdir=str(tmp_path / "work"))
    s.open(src)
    out = str(tmp_path / "out.pdf")
    s.arrange(_plan((3, 90), (1, 0)), out)  # page 2 deleted, page 3 rotated, order changed

    def raw_images(path, page):
        with pikepdf.open(path) as pdf:
            return [bytes(v.read_raw_bytes()) for _, v in pdf.pages[page].images.items()]

    assert raw_images(out, 0) == raw_images(src, 2)
    assert raw_images(out, 1) == raw_images(src, 0)
    with pikepdf.open(src) as a, pikepdf.open(out) as b:
        assert page_bytes(b.pages[1]) == page_bytes(a.pages[0])  # content stream too


def test_deleted_pages_leave_no_data(tmp_path):
    src = make_multipage_pdf(str(tmp_path / "m.pdf"), 4)
    s = Session(workdir=str(tmp_path / "work"))
    s.open(src)
    assert b"UNIQUEMARK2" in open(src, "rb").read()
    out = str(tmp_path / "out.pdf")
    s.arrange(_plan((1, 0), (3, 0)), out)
    raw = open(out, "rb").read()
    assert b"UNIQUEMARK2" not in raw and b"UNIQUEMARK4" not in raw
    assert b"UNIQUEMARK1" in raw and b"UNIQUEMARK3" in raw

    img = make_image_pdf(str(tmp_path / "img.pdf"), 3)
    s.open(img)
    out2 = str(tmp_path / "img_out.pdf")
    s.arrange(_plan((1, 0), (3, 0)), out2)
    with pikepdf.open(out2) as pdf:
        n_images = sum(1 for o in pdf.objects if o._type_code == pikepdf.ObjectType.stream and o.get("/Subtype") == "/Image")
    assert n_images == 2  # the deleted page's image is not in the file


def test_bookmarks_and_links_do_not_keep_deleted_pages(tmp_path):
    src = make_multipage_pdf(str(tmp_path / "m.pdf"), 3, outline=True, links=True)
    with pikepdf.open(src) as pdf:
        assert "/Outlines" in pdf.Root and len(pdf.pages[0].Annots) == 2
    s = Session(workdir=str(tmp_path / "work"))
    s.open(src)

    out = str(tmp_path / "out.pdf")
    s.arrange(_plan((1, 0), (2, 0)), out)  # page 3 deleted: bookmark + link point at it
    raw = open(out, "rb").read()
    assert b"UNIQUEMARK3" not in raw
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 2
        assert "/Outlines" not in pdf.Root
        annots = pdf.pages[0].Annots
        assert len(annots) == 1  # only the link to the page that is still there
        assert annots[0].Dest[0].objgen == pdf.pages[1].obj.objgen
    d = PdfDoc(out)  # pdfium opens it too
    try:
        assert d.page_count == 2
    finally:
        d.close()

    # only reordering: nothing is deleted, so the bookmarks stay
    out2 = str(tmp_path / "out2.pdf")
    s.arrange(_plan((3, 0), (2, 0), (1, 0)), out2)
    with pikepdf.open(out2) as pdf:
        assert "/Outlines" in pdf.Root and len(pdf.pages[2].Annots) == 2


def test_inherited_attributes(tmp_path):
    src = make_multipage_pdf(str(tmp_path / "m.pdf"), 3)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        tree = pdf.Root.Pages
        tree.Rotate = 90
        tree.MediaBox = pikepdf.Array([0, 0, 300, 400])
        for p in pdf.pages:
            del p.obj["/Rotate"]
            del p.obj["/MediaBox"]
        pdf.save(src)
    s = Session(workdir=str(tmp_path / "work"))
    s.open(src)
    out = str(tmp_path / "out.pdf")
    s.arrange(_plan((3, 90), (1, 0)), out)
    a, b = PdfDoc(src), PdfDoc(out)
    try:
        assert a.pages[0].geometry.rotation == 90
        assert b.pages[0].geometry.rotation == 180  # inherited 90 + extra 90
        assert b.pages[1].geometry.rotation == 90
        assert (b.pages[1].geometry.display_w, b.pages[1].geometry.display_h) == (a.pages[0].geometry.display_w, a.pages[0].geometry.display_h)
        assert not b.pages[0].size_mismatch and not b.pages[1].size_mismatch
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "plan, message",
    [
        ([], "至少要保留 1 頁。"),
        ([{"src": 4, "rotate": 0}], "頁碼超出範圍：4"),
        ([{"src": 0, "rotate": 0}], "頁碼超出範圍：0"),
        ([{"src": 1, "rotate": 0}, {"src": 1, "rotate": 90}], "每一頁最多只能出現一次。"),
        ([{"src": 1, "rotate": 45}], "旋轉角度只能是 0、90、180、270。"),
    ],
)
def test_validation(tmp_path, plan, message):
    s, src = _session(tmp_path)
    out = str(tmp_path / "out.pdf")
    with pytest.raises(EngineError) as ex:
        s.arrange(plan, out)
    assert str(ex.value) == message
    assert not os.path.exists(out) and not os.path.exists(out + ".tmp")


def test_never_overwrites_the_original(tmp_path):
    s, src = _session(tmp_path)
    before = _sha(src)
    with pytest.raises(EngineError, match="不能覆蓋原始檔"):
        s.arrange(_plan((1, 0)), src)
    assert _sha(src) == before
    assert not [f for f in os.listdir(tmp_path) if ".audit." in f or f.endswith(".tmp")]


def test_original_unchanged_after_arrange(tmp_path):
    s, src = _session(tmp_path)
    before = _sha(src)
    r = s.arrange(_plan((2, 90), (1, 0)), str(tmp_path / "out.pdf"))
    assert _sha(src) == before
    with open(r["audit"]["json"], encoding="utf-8") as f:
        assert json.load(f)["source"]["sha256"] == before


def test_thumbnails(tmp_path):
    s, src = _session(tmp_path, n=4)
    r = s.thumbnails()
    assert [i["page"] for i in r["images"]] == [1, 2, 3, 4]
    assert all(os.path.isfile(i["path"]) for i in r["images"])
    w, h = r["images"][0]["width"], r["images"][0]["height"]
    assert abs(w / h - 300 / 400) < 0.03
    assert w < 300  # 36 dpi: smaller than the page's 72 dpi size
    r2 = s.thumbnails([2, 4], dpi=72)
    assert [i["page"] for i in r2["images"]] == [2, 4] and r2["images"][0]["width"] == 300
    with pytest.raises(EngineError, match="頁碼超出範圍：9"):
        s.thumbnails([1, 9])
    events = []
    s.thumbnails(progress=lambda stage, f: events.append(stage) or True)
    assert "頁 1" in events and "頁 4" in events


def test_audit_record(tmp_path):
    s, src = _session(tmp_path, n=4)
    out = str(tmp_path / "out.pdf")
    r = s.arrange(_plan((3, 90), (1, 0)), out)
    assert (r["audit"]["json"], r["audit"]["csv"]) == audit_paths(out)
    with open(r["audit"]["json"], encoding="utf-8") as f:
        a = json.load(f)
    assert a["schema"] == 1 and a["action"] == "arrange-pages"
    assert a["changes"] == [{"outPage": 1, "srcPage": 3, "rotate": 90}, {"outPage": 2, "srcPage": 1, "rotate": 0}]
    assert a["deletedSrcPages"] == [2, 4]
    assert a["source"]["pageCount"] == 4 and a["output"]["pageCount"] == 2
    assert a["output"]["sha256"] == _sha(out) == r["sha256"]
    with open(r["audit"]["csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["序號", "輸出頁碼", "原始頁碼", "旋轉角度", "動作類型"]
    assert rows[1][1:] == ["1", "3", "90", "旋轉"] and rows[2][1:] == ["2", "1", "0", "保留"]
    assert [r[4] for r in rows[3:]] == ["刪除", "刪除"] and [r[2] for r in rows[3:]] == ["2", "4"]

    out2 = str(tmp_path / "out2.pdf")
    r2 = s.arrange(_plan((1, 0)), out2, audit=False)
    assert "audit" not in r2 and not os.path.exists(audit_paths(out2)[0])


def test_subprocess_protocol(tmp_path):
    src = make_multipage_pdf(str(tmp_path / "a.pdf"), 3)
    c = Client(tmp_path)
    try:
        assert c.call(1, "open", path=src)["result"]["pageCount"] == 3
        t = c.call(2, "thumbnails", dpi=30)
        assert t["ok"] and len(t["result"]["images"]) == 3
        assert any(e["event"] == "progress" and e["id"] == 2 for e in c.events)
        out = str(tmp_path / "out.pdf")
        r = c.call(3, "arrange", plan=[{"src": 2, "rotate": 180}, {"src": 1, "rotate": 0}], out=out)
        assert r["ok"] and r["result"]["pageCount"] == 2 and r["result"]["deleted"] == [3]
        assert os.path.isfile(r["result"]["audit"]["json"])
        bad = c.call(4, "arrange", plan=[], out=str(tmp_path / "x.pdf"))
        assert bad["ok"] is False and bad["error"]["code"] == "user" and "至少要保留" in bad["error"]["message"]
        assert c.call(5, "arrange", plan=[{"src": 1}], out=src)["error"]["code"] == "user"
    finally:
        c.close()
