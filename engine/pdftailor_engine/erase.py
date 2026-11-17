"""Painting over damaged or dirty areas of a page, for good.

The user draws a box over a tear, a stain or a stray mark; the box is filled with the
colour of the paper around it, with the paper's grain (or plain white, or a colour of
their choice). Growing the surroundings into the box (in-painting) was tried and dropped:
it drags neighbouring text and rules into the box as dark smears.

A box is not just laid over the page. On a scan the page *is* a picture, and a cover on
top of it would leave the original pixels in the file, where any PDF tool can lift the
cover off again. So the picture itself is rewritten: the pixels under the box are
replaced in every image the page draws directly, and the image is stored again. Text
that starts inside the box — real text, or the invisible text layer of an earlier OCR —
is removed from the content stream as well.

What cannot be rewritten is covered instead, and the report says so (``covered``):
vector drawings, images inside form XObjects, stencil masks, and images in formats this
build cannot decode. For those a cover is painted on top, which hides but does not remove.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass, field

import numpy as np

from .contentstream import IDENTITY, Matrix, T, mmul, tokenize
from .geometry import PageGeometry, Rect
from .redact import _inherited, _shallow_dict, page_content_bytes, redact_page

MODES = ("auto", "white", "color")
RING_PT = 10.0  # how far around the box the paper is sampled
JPEG_QUALITY = 92


@dataclass
class Erase:
    page: int
    rect: Rect  # display pt
    mode: str = "auto"
    color: tuple[int, int, int] = (255, 255, 255)
    # filled in when written
    fill: tuple[int, int, int] | None = None  # the colour used
    removed: bool = False  # the pixels underneath were rewritten (not only covered)

    @staticmethod
    def from_json(d: dict) -> "Erase":
        mode = str(d.get("mode", "auto"))
        if mode not in MODES:
            mode = "auto"
        c = d.get("color") or [255, 255, 255]
        return Erase(int(d["page"]), Rect.from_list(d["rect"]), mode, (int(c[0]), int(c[1]), int(c[2])))

    def to_json(self) -> dict:
        r = self.rect
        return {
            "page": self.page,
            "rect": [round(r.x, 2), round(r.y, 2), round(r.w, 2), round(r.h, 2)],
            "mode": self.mode,
            "color": list(self.color),
            "fill": list(self.fill) if self.fill else None,
            "removed": self.removed,
        }


@dataclass
class _Placed:
    name: str  # resource name, with the slash
    obj: object  # the image stream
    ctm: Matrix  # unit square -> page user space


@dataclass
class EraseResult:
    removed: int = 0
    covered: int = 0
    text_runs: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# what the box is filled with
# --------------------------------------------------------------------------------------
def make_patch(page_rgb: np.ndarray, scale: float, e: Erase) -> tuple[np.ndarray, Rect] | None:
    """Pixels for the box, at ``scale`` px per pt, and the exact rect they cover."""
    from .analysis.imaging import estimate_background

    H, W = page_rgb.shape[:2]
    x0, y0 = max(0, int(round(e.rect.x * scale))), max(0, int(round(e.rect.y * scale)))
    x1, y1 = min(W, int(round(e.rect.right * scale))), min(H, int(round(e.rect.bottom * scale)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    exact = Rect(x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale)
    h, w = y1 - y0, x1 - x0
    if e.mode == "white":
        e.fill = (255, 255, 255)
        return np.full((h, w, 3), 255, np.uint8), exact
    if e.mode == "color":
        e.fill = e.color
        return np.full((h, w, 3), e.color, np.uint8), exact

    # the paper around the box
    m = max(4, int(round(RING_PT * scale)))
    rx0, ry0, rx1, ry1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
    crop = page_rgb[ry0:ry1, rx0:rx1]
    inner = np.zeros(crop.shape[:2], bool)
    inner[y0 - ry0 : y1 - ry0, x0 - rx0 : x1 - rx0] = True
    ring = crop[~inner]
    if ring.size == 0:
        e.fill = (255, 255, 255)
        return np.full((h, w, 3), 255, np.uint8), exact
    bg, _cover = estimate_background(ring.reshape(-1, 1, 3))
    bg_arr = np.array(bg, np.float32)
    near = np.abs(ring.astype(np.float32) - bg_arr).max(axis=1) < 24
    paper = ring[near] if near.sum() >= 16 else ring
    colour = paper.astype(np.float32).mean(axis=0)
    grain = float(np.clip(paper.astype(np.float32).mean(axis=1).std(), 0.0, 6.0))
    e.fill = tuple(int(round(v)) for v in colour)

    patch = np.empty((h, w, 3), np.float32)
    patch[:] = colour
    if grain > 0.3:
        # the paper's own grain, so the box is not a flat tile on a noisy scan
        rng = np.random.default_rng(int(x0) * 7919 + int(y0) * 104729 + w)
        patch += rng.normal(0.0, grain, (h, w, 1)).astype(np.float32)
    return np.clip(patch, 0, 255).astype(np.uint8), exact


# --------------------------------------------------------------------------------------
# the images a page draws
# --------------------------------------------------------------------------------------
_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "sh"}


def _placed_images(page) -> tuple[list[_Placed], bool]:
    """Images drawn directly by the page's content stream, and whether the page also
    draws things that are not rewritten: form XObjects, vector paths, inline images."""
    res = _inherited(page.obj, "/Resources")
    xobjs = res.get("/XObject") if res is not None else None
    out: list[_Placed] = []
    forms = False
    ctm: Matrix = IDENTITY
    stack: list[Matrix] = []
    operands = []
    for tok in tokenize(page_content_bytes(page)):
        if tok.is_operand:
            operands.append(tok)
            continue
        if tok.kind is T.INLINE_IMAGE:
            forms = True
            continue
        if tok.kind is not T.OPERATOR:
            continue
        op = tok.text
        if op in _PAINT:
            forms = True
        elif op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm" and len(operands) >= 6:
            ctm = mmul(tuple(t.number for t in operands[-6:]), ctm)  # type: ignore[arg-type]
        elif op == "Do" and operands and operands[-1].kind is T.NAME:
            name = operands[-1].text or ""
            key = name if name.startswith("/") else "/" + name
            obj = xobjs.get(key) if xobjs is not None else None
            if obj is not None:
                sub = str(obj.get("/Subtype"))
                if sub == "/Image":
                    out.append(_Placed(key, obj, ctm))
                elif sub == "/Form":
                    forms = True
        operands = []
    return out, forms


def _affine(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> np.ndarray:
    """2x3 matrix taking three ``src`` points to ``dst``."""
    import cv2

    return cv2.getAffineTransform(np.float32(src), np.float32(dst))


def _apply(m: np.ndarray, x: float, y: float) -> tuple[float, float]:
    return float(m[0, 0] * x + m[0, 1] * y + m[0, 2]), float(m[1, 0] * x + m[1, 1] * y + m[1, 2])


def _image_to_display(geom: PageGeometry, ctm: Matrix, w: int, h: int) -> np.ndarray | None:
    """Image pixel (column, row) -> display pt, as a 2x3 matrix. None if degenerate."""
    a, b, c, d, e, f = ctm

    def at(u: float, v: float) -> tuple[float, float]:
        # pixel -> unit square (row 0 is the top of the image, y = 1) -> user space -> display
        x, y = u / w, 1.0 - v / h
        return geom.user_to_display(a * x + c * y + e, b * x + d * y + f)

    pts = [(0.0, 0.0), (float(w), 0.0), (0.0, float(h))]
    dst = [at(*p) for p in pts]
    area = abs((dst[1][0] - dst[0][0]) * (dst[2][1] - dst[0][1]) - (dst[2][0] - dst[0][0]) * (dst[1][1] - dst[0][1]))
    if area < 1e-6:
        return None
    return _affine(pts, dst)


def _store(pdf, old, pil) -> object | None:
    """A new image stream holding ``pil``, keeping what can be kept of ``old``'s settings."""
    import pikepdf
    from pikepdf import Name

    mode = pil.mode
    if mode not in ("L", "RGB", "CMYK"):
        return None
    comps = {"L": 1, "RGB": 3, "CMYK": 4}[mode]
    keep = pikepdf.Dictionary()
    for k, v in old.items():
        if k in ("/Length", "/Filter", "/DecodeParms", "/Decode", "/Width", "/Height", "/BitsPerComponent", "/ColorSpace"):
            continue
        keep[k] = v
    new = pdf.make_stream(b"")
    filt = old.get("/Filter")
    was_jpeg = filt == Name.DCTDecode or (isinstance(filt, pikepdf.Array) and len(filt) == 1 and filt[0] == Name.DCTDecode)
    if was_jpeg:
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=JPEG_QUALITY, subsampling=0 if mode != "L" else None)
        new.write(buf.getvalue(), filter=Name.DCTDecode)
    else:
        new.write(zlib.compress(pil.tobytes(), 6), filter=Name.FlateDecode)
    for k, v in keep.items():
        new[k] = v
    new["/Type"] = Name.XObject
    new["/Subtype"] = Name.Image
    new["/Width"] = pil.width
    new["/Height"] = pil.height
    new["/BitsPerComponent"] = 8
    # Keep a colour space with the same number of components (an ICC profile, say), unless
    # the picture had to be converted (palette, 1-bit) or CMYK was read back inverted.
    cs = old.get("/ColorSpace")
    same = False
    if cs is not None and int(old.get("/BitsPerComponent", 8)) == 8 and "/Decode" not in old:
        if isinstance(cs, pikepdf.Name):
            same = {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}.get(str(cs)) == comps
        elif isinstance(cs, pikepdf.Array) and len(cs) == 2 and str(cs[0]) == "/ICCBased":
            try:
                same = int(cs[1].get("/N", 0)) == comps
            except Exception:
                same = False
    new["/ColorSpace"] = cs if same else {1: Name.DeviceGray, 3: Name.DeviceRGB, 4: Name.DeviceCMYK}[comps]
    return new


def _rewrite(pdf, placed: _Placed, geom: PageGeometry, jobs: list[tuple[Erase, np.ndarray, Rect]], scale: float):
    """Replaces the pixels under the boxes in one image. Returns (new stream or None,
    the boxes that lie wholly inside this image)."""
    import cv2
    import pikepdf
    from PIL import Image

    obj = placed.obj
    if bool(obj.get("/ImageMask", False)):
        return None, []
    try:
        pil = pikepdf.PdfImage(obj).as_pil_image()
    except Exception:
        return None, []
    if pil.mode in ("1", "P", "LA", "I;16", "I"):
        pil = pil.convert("L" if pil.mode in ("1", "I;16", "I") else "RGB")
    elif pil.mode == "RGBA":
        pil = pil.convert("RGB")
    if pil.mode not in ("L", "RGB", "CMYK"):
        return None, []
    w, h = pil.size
    to_disp = _image_to_display(geom, placed.ctm, w, h)
    if to_disp is None:
        return None, []
    to_img = cv2.invertAffineTransform(to_disp)
    arr = np.array(pil)
    touched = False
    inside: list[Erase] = []
    for e, patch, exact in jobs:
        corners = [(exact.x, exact.y), (exact.right, exact.y), (exact.right, exact.bottom), (exact.x, exact.bottom)]
        pts = np.array([_apply(to_img, x, y) for x, y in corners])
        fx0, fy0, fx1, fy1 = pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()
        x0, y0 = max(0, int(np.floor(fx0))), max(0, int(np.floor(fy0)))
        x1, y1 = min(w, int(np.ceil(fx1))), min(h, int(np.ceil(fy1)))
        if x1 <= x0 or y1 <= y0:
            continue
        # region pixel -> patch pixel: image px -> display pt -> patch px
        m = to_disp.copy()
        m[:, 2] += m[:, 0] * x0 + m[:, 1] * y0  # shift origin to the region
        m[0] = (m[0] - np.array([0, 0, exact.x])) * scale
        m[1] = (m[1] - np.array([0, 0, exact.y])) * scale
        size = (x1 - x0, y1 - y0)
        flags = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
        fill = cv2.warpAffine(patch, m, size, flags=flags, borderMode=cv2.BORDER_REPLICATE)
        ones = np.full(patch.shape[:2], 255, np.uint8)
        cover = cv2.warpAffine(ones, m, size, flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
        # pixels cut by the box's edge go too: half a covered pixel would still show the stain
        cover = cv2.dilate(cover.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        if not cover.any():
            continue
        if pil.mode == "L":
            fill_m = cv2.cvtColor(fill, cv2.COLOR_RGB2GRAY)
        elif pil.mode == "CMYK":
            fill_m = np.array(Image.fromarray(fill, "RGB").convert("CMYK"))
        else:
            fill_m = fill
        region = arr[y0:y1, x0:x1]
        region[cover] = fill_m[cover]
        touched = True
        if fx0 >= -0.5 and fy0 >= -0.5 and fx1 <= w + 0.5 and fy1 <= h + 0.5:
            inside.append(e)
    if not touched:
        return None, []
    new = _store(pdf, obj, Image.fromarray(arr, pil.mode))
    return new, (inside if new is not None else [])


def erase_on_page(pdf, page, geom: PageGeometry, page_rgb: np.ndarray, scale: float, erases: list[Erase], canvas, paint_patch) -> EraseResult:
    """Applies the boxes to one page of ``pdf`` (pikepdf). ``canvas`` is the overlay being
    drawn for the page and ``paint_patch(canvas, geom, pil_image, rect)`` draws on it —
    used only for boxes whose content could not be rewritten."""
    from PIL import Image

    result = EraseResult()
    jobs: list[tuple[Erase, np.ndarray, Rect]] = []
    for e in erases:
        made = make_patch(page_rgb, scale, e)
        if made is not None:
            jobs.append((e, made[0], made[1]))
    if not jobs:
        return result

    # 1. text that starts inside a box (visible text and old invisible OCR layers)
    regions = []
    for _e, _p, exact in jobs:
        ur = geom.display_rect_to_user(exact)
        regions.append((ur.left, ur.bottom, ur.right, ur.top))
    result.text_runs = redact_page(pdf, page, regions)

    # 2. the pictures themselves
    placed, has_forms = _placed_images(page)
    done: set[int] = set()
    replaced: dict[str, object] = {}
    for im in placed:
        if im.name in replaced:
            continue  # the same picture drawn twice: rewritten once
        new, inside = _rewrite(pdf, im, geom, jobs, scale)
        if new is None:
            continue
        replaced[im.name] = pdf.make_indirect(new)
        done.update(id(e) for e in inside)
    if replaced:
        # Resources (and the XObject dictionary) may be shared with other pages: this page
        # gets its own copies, so the other pages keep the untouched picture.
        res = _inherited(page.obj, "/Resources")
        new_res = _shallow_dict(res)
        xobjs = _shallow_dict(new_res["/XObject"])
        for name, obj in replaced.items():
            xobjs[name] = obj
        new_res["/XObject"] = xobjs
        page.obj["/Resources"] = new_res

    # 3. whatever is not a rewritten picture gets a cover
    only_picture = bool(placed) and not has_forms
    for e, patch, exact in jobs:
        if id(e) in done and only_picture:
            e.removed = True
            result.removed += 1
            continue
        e.removed = False
        result.covered += 1
        paint_patch(canvas, geom, Image.fromarray(patch, "RGB"), exact)
    if result.covered:
        result.notes.append("erase-covered-only")
    return result
