"""Painting over an area: the pixels must be gone from the file, not just hidden."""
import io
import json

import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from pdftailor_engine.pdfio import PdfDoc
from pdftailor_engine.service import Session

W, H = 595, 842
PAPER = (236, 230, 214)  # yellowed paper


def _scan(dpi=150):
    s = dpi / 72
    im = Image.new("RGB", (int(W * s), int(H * s)), PAPER)
    d = ImageDraw.Draw(im)
    for i in range(12):  # "text"
        d.rectangle([60 * s, (80 + i * 40) * s, 500 * s, (88 + i * 40) * s], fill=(30, 30, 30))
    d.ellipse([200 * s, 300 * s, 300 * s, 380 * s], fill=(90, 60, 30))  # a stain
    a = np.asarray(im).astype(np.float32) + np.random.default_rng(0).normal(0, 3, (im.height, im.width, 3))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _pdf(path, im, how="jpeg", rotate=0, pages=1, hidden_text=False):
    c = canvas.Canvas(str(path), pagesize=(W, H))
    for _ in range(pages):
        if how == "jpeg":
            b = io.BytesIO()
            im.save(b, "JPEG", quality=85)
            c.drawImage(ImageReader(Image.open(io.BytesIO(b.getvalue()))), 0, 0, W, H)
        else:
            c.drawImage(ImageReader(im.convert(how)), 0, 0, W, H)
        if hidden_text:
            t = c.beginText(210, H - 340)
            t.setTextRenderMode(3)
            t.setFont("Helvetica", 12)
            t.textOut("SECRETSTAIN")
            c.drawText(t)
            t = c.beginText(60, H - 87)
            t.setTextRenderMode(3)
            t.setFont("Helvetica", 12)
            t.textOut("KEEPME")
            c.drawText(t)
        c.showPage()
    c.save()
    if rotate:
        with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
            for p in pdf.pages:
                p.obj["/Rotate"] = rotate
            pdf.save(str(path))
    return str(path)


def _images(path, page=0):
    with pikepdf.open(path) as pdf:
        return [np.array(pikepdf.PdfImage(v).as_pil_image().convert("RGB")) for k, v in pdf.pages[page].images.items()]


BOX = [195, 295, 110, 90]  # display pt, around the stain


@pytest.mark.parametrize("how", ["jpeg", "RGB", "L"])
def test_pixels_are_rewritten_in_the_image_itself(tmp_path, how):
    src = _pdf(tmp_path / "s.pdf", _scan(), how)
    s = Session()
    s.open(src)
    out = str(tmp_path / "o.pdf")
    r = s.export([], out, erases=[{"page": 1, "rect": BOX, "mode": "auto"}])
    assert r["report"]["erased"] == 1 and r["report"]["eraseCovered"] == 0
    assert r["erases"][0]["removed"] is True
    assert r["verify"]["ok"], r["verify"]
    imgs = _images(out)
    assert len(imgs) == 1, "no cover image may be added when the picture was rewritten"
    im = imgs[0]
    k = im.shape[1] / W
    x0, y0, x1, y1 = [int(v * k) for v in (BOX[0], BOX[1], BOX[0] + BOX[2], BOX[1] + BOX[3])]
    inside = im[y0 + 2 : y1 - 2, x0 + 2 : x1 - 2].astype(int)
    target = np.array(PAPER if how != "L" else [int(np.dot(PAPER, [0.299, 0.587, 0.114]))] * 3)
    assert np.abs(inside.mean(axis=(0, 1)) - target).max() < 6      # paper colour, not white
    assert inside.min() > 150                                       # nothing of the stain is left
    before = _images(src)[0].astype(int)
    assert np.abs(before[: y0 - 8] - im[: y0 - 8].astype(int)).mean() < 3  # the rest is the same picture
    # the audit record says what happened
    audit = json.load(open(r["audit"]["json"], encoding="utf-8"))
    ch = [c for c in audit["changes"] if c["kinds"] == ["erase"]]
    assert len(ch) == 1 and ch[0]["eraseRemoved"] is True and ch[0]["originalBounds"] == [195.0, 295.0, 110.0, 90.0]


def test_white_and_custom_colour(tmp_path):
    src = _pdf(tmp_path / "s.pdf", _scan(), "RGB")
    s = Session()
    s.open(src)
    out = str(tmp_path / "o.pdf")
    s.export([], out, erases=[{"page": 1, "rect": BOX, "mode": "white"}, {"page": 1, "rect": [60, 600, 40, 30], "mode": "color", "color": [200, 10, 10]}])
    im = _images(out)[0]
    k = im.shape[1] / W
    assert (im[int(320 * k), int(250 * k)] == 255).all()
    assert tuple(im[int(615 * k), int(80 * k)]) == (200, 10, 10)


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_rotated_pages(tmp_path, rotate):
    src = _pdf(tmp_path / "s.pdf", _scan(100), "RGB", rotate=rotate)
    doc = PdfDoc(src)
    before = doc.render(1, 1.0).astype(int)
    hh, ww = before.shape[:2]
    box = [ww * 0.2, hh * 0.3, ww * 0.25, hh * 0.2]  # in the rotated (display) frame
    s = Session()
    s.open(src)
    out = str(tmp_path / "o.pdf")
    r = s.export([], out, erases=[{"page": 1, "rect": box, "mode": "white"}])
    assert r["report"]["erased"] == 1
    after = PdfDoc(out).render(1, 1.0).astype(int)
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[0] + box[2]), int(box[1] + box[3])
    assert after[y0 + 3 : y1 - 3, x0 + 3 : x1 - 3].min() >= 250      # the box is white where it was drawn
    outside = np.ones(before.shape[:2], bool)
    outside[y0 - 3 : y1 + 3, x0 - 3 : x1 + 3] = False
    assert np.abs(before - after).max(axis=2)[outside].max() <= 8    # and nowhere else


def test_text_under_the_box_is_removed_and_other_pages_keep_their_picture(tmp_path):
    src = _pdf(tmp_path / "s.pdf", _scan(), "RGB", pages=2, hidden_text=True)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:  # both pages share one picture
        pdf.pages[1].obj["/Resources"] = pdf.pages[0].obj["/Resources"]
        pdf.pages[1].obj["/Contents"] = pdf.pages[0].obj["/Contents"]
        pdf.save(src)
    s = Session()
    s.open(src)
    out = str(tmp_path / "o.pdf")
    s.export([], out, erases=[{"page": 1, "rect": BOX, "mode": "auto"}])
    import pypdfium2 as pdfium

    d = pdfium.PdfDocument(out)
    t1, t2 = d[0].get_textpage().get_text_range(), d[1].get_textpage().get_text_range()
    assert "SECRETSTAIN" not in t1 and "KEEPME" in t1
    assert "SECRETSTAIN" in t2
    k = _images(out, 1)[0].shape[1] / W
    assert _images(out, 1)[0][int(340 * k), int(250 * k)].astype(int).sum() < 300  # page 2 still has its stain
    raw = open(out, "rb").read()
    assert raw.count(b"SECRETSTAIN") <= 1


def test_vector_page_is_covered_and_says_so(tmp_path):
    p = str(tmp_path / "v.pdf")
    c = canvas.Canvas(p, pagesize=(W, H))
    c.setFont("Helvetica", 14)
    c.drawString(100, 700, "A vector page with a line")
    c.line(50, 500, 500, 500)
    c.save()
    s = Session()
    s.open(p)
    out = str(tmp_path / "o.pdf")
    r = s.export([], out, erases=[{"page": 1, "rect": [40, 330, 480, 30], "mode": "white"}])
    assert r["report"]["erased"] == 0 and r["report"]["eraseCovered"] == 1
    assert any(n["note"] == "erase-covered-only" for n in r["report"]["notes"])
    after = PdfDoc(out).render(1, 1.0)
    assert after[342, 200].min() >= 250  # the line is hidden


def test_preview_shows_the_box(tmp_path):
    src = _pdf(tmp_path / "s.pdf", _scan(100), "RGB")
    s = Session()
    s.open(src)
    pv = s.preview([], [1], 72, erases=[{"page": 1, "rect": BOX, "mode": "white"}])
    im = np.asarray(Image.open(pv["images"][0]["path"]).convert("RGB"))
    assert im[340, 250].min() >= 250
