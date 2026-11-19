"""High-level operations the shell calls: open, analyse, preview, export.

One :class:`Session` holds one open document. Everything that crosses the process
boundary is plain JSON-able data (dicts, lists, file paths); pages are handed over as
PNG files in the session's temp folder rather than as bytes on the pipe.

Preview and export share one code path — a preview *is* an export to a temp file,
rendered — so what the user approves is exactly what gets written.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from dataclasses import replace
from typing import Any, Callable

import numpy as np

from .analysis.imaging import ink_mask
from .audit import build_arrange_audit, build_edit_audit, sha256_file, write_audit
from .erase import Erase
from .fontmatch import CJK_CANDIDATES, LATIN_CANDIDATES, score_fonts, score_latin, settle_families
from .fonts import FontIndex, default_index, is_cjk
from .geometry import Rect
from .models import Block, Edit, Run
from .pageops import PlanError, arrange as _arrange_pages, validate_plan
from .ocr.base import OcrEngine, OcrLine, OcrUnavailable, ExternalOcr
from .ocr.tesseract import TesseractOcr, find_tesseract
from .pdfio import PdfDoc
from .scanpage import analyze_scanned_page
from .textlayer import build_blocks, read_glyphs
from .writer import write_edits

ANALYZE_DPI = 300
WORD_DPI = 150  # page render used to find table rules and cell shading
PREVIEW_DPI = 110
COMMON_SIZES = [6, 6.5, 7, 7.5, 8, 9, 10, 10.5, 11, 12, 13, 14, 15, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 44, 48, 54, 60, 66, 72]
SNAP_TOLERANCE = 0.035

Progress = Callable[[str, float], bool]  # (stage, 0..1) -> keep going?


class Cancelled(Exception):
    pass


class EngineError(Exception):
    """An error meant for the user; the message is shown as is."""


FONT_LINES = 12  # lines per group whose typeface is actually measured


def snap_size(size: float) -> float:
    best = min(COMMON_SIZES, key=lambda c: abs(c - size))
    return float(best) if abs(best / size - 1) <= SNAP_TOLERANCE else round(size * 2) / 2


def _script(text: str) -> str:
    g = [c for c in text if not c.isspace()]
    n = sum(is_cjk(c) for c in g)
    # Two ideographs are enough: the line then needs a CJK family whatever else it holds.
    return "cjk" if g and (n >= 2 or n * 2 >= len(g)) else "latin"


def _size_clusters(sizes: list[float], gap: float = 1.12) -> list[int]:
    """Cluster index per size: sorted sizes are cut wherever two neighbours differ by more than ``gap``."""
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    out = [0] * len(sizes)
    k = 0
    for a, b in zip([None] + order[:-1], order):
        if a is not None and sizes[b] > gap * max(sizes[a], 0.1):
            k += 1
        out[b] = k
    return out


class Session:
    def __init__(self, fonts: FontIndex | None = None, workdir: str | None = None):
        self._fonts = fonts
        self.workdir = workdir or tempfile.mkdtemp(prefix="pdftailor-")
        os.makedirs(self.workdir, exist_ok=True)
        self.doc: PdfDoc | None = None
        self.blocks: dict[int, list[Block]] = {}
        self._lock = threading.RLock()
        self._serial = 0
        self._rapid: OcrEngine | None = None  # PP-OCRv5, loaded on first use
        self._rapid_failed = ""
        self._max_quality = False  # read everything with the medium models (the app's "highest accuracy" box)
        self.page_quality: dict[int, str] = {}  # how each scanned page in ``blocks`` was read

    # ---- lifecycle ------------------------------------------------------------------
    @property
    def fonts(self) -> FontIndex:
        if self._fonts is None:
            self._fonts = default_index()
        return self._fonts

    def _tmp(self, stem: str, ext: str) -> str:
        self._serial += 1
        return os.path.join(self.workdir, f"{stem}-{self._serial}{ext}")

    def close(self) -> None:
        if self.doc:
            self.doc.close()
            self.doc = None
        self.blocks.clear()
        self.page_quality.clear()

    def dispose(self) -> None:
        self.close()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _need_doc(self) -> PdfDoc:
        if self.doc is None:
            raise EngineError("尚未開啟 PDF。")
        return self.doc

    # ---- open -----------------------------------------------------------------------
    def open(self, path: str) -> dict[str, Any]:
        with self._lock:
            self.close()
            self.doc = PdfDoc(path)
            pages = []
            for i, info in enumerate(self.doc.pages, start=1):
                _glyphs, stats = read_glyphs(self.doc, i)
                d = info.geometry.to_json()
                d["textLayer"] = stats.kind  # none / invisible / visible
                d["needsOcr"] = stats.kind != "visible"
                pages.append(d)
            return {"path": self.doc.path, "pageCount": self.doc.page_count, "pages": pages}

    def render_page(self, page: int, dpi: float = PREVIEW_DPI, path: str | None = None) -> dict[str, Any]:
        doc = self._need_doc()
        out = path or self._tmp(f"page{page}", ".png")
        w, h = doc.render_png(page, dpi / 72.0, out)
        return {"page": page, "path": out, "width": w, "height": h, "dpi": dpi}

    # ---- analysis -------------------------------------------------------------------
    def _ocr_engine(self, name: str, lines: list[dict] | None, scale: float) -> OcrEngine:
        if lines is not None:
            # Lines from the shell (Windows OCR) come in display points.
            return ExternalOcr(
                [
                    OcrLine(
                        str(l.get("text", "")),
                        float(l["x"]) * scale,
                        float(l["y"]) * scale,
                        float(l["w"]) * scale,
                        float(l["h"]) * scale,
                        float(l.get("confidence", -1)),
                    )
                    for l in lines
                ]
            )
        if name == "auto":
            name = os.environ.get("PDFTAILOR_OCR", "auto").strip().lower() or "auto"
        if name in ("auto", "rapid", "pp-ocrv5"):
            # PP-OCRv5 first: it reads small and soft scans that Tesseract cannot. It is
            # optional (package and model files), so its absence is not an error in "auto".
            if self._rapid is None and not self._rapid_failed:
                try:
                    from .ocr.rapid import RapidOcr

                    self._rapid = RapidOcr()
                except Exception as ex:
                    self._rapid_failed = str(ex) or type(ex).__name__
            if self._rapid is not None:
                self._rapid.max_quality = self._max_quality or os.environ.get("PDFTAILOR_OCR_QUALITY", "").strip().lower() == "max"
                return self._rapid
            if name != "auto":
                raise EngineError(f"PP-OCR 無法使用：{self._rapid_failed}")
        if name in ("auto", "tesseract"):
            if find_tesseract() is None:
                raise EngineError("找不到 Tesseract OCR。請把 tesseract 放在引擎旁的 tesseract 資料夾，或設定 PDFTAILOR_TESSERACT。")
            return TesseractOcr()
        raise EngineError(f"不認得的 OCR 引擎：{name}")

    def _read_scan(self, rgb: np.ndarray, scale: float, ocr: str, ocr_lines: list[dict] | None, page: int):
        """OCR of one rendered page. Returns the result and the name of the engine used.

        If PP-OCRv5 was picked automatically and breaks on this page, the page is read
        with Tesseract instead and PP-OCRv5 is not tried again in this session.
        """
        try:
            engine = self._ocr_engine(ocr, ocr_lines, scale)
            try:
                return analyze_scanned_page(rgb, scale, engine, page), engine.name
            except OcrUnavailable:
                raise
            except Exception as ex:
                if engine is not self._rapid or ocr != "auto" or find_tesseract() is None:
                    raise
                self._rapid, self._rapid_failed = None, f"{type(ex).__name__}: {ex}"
                engine = TesseractOcr()
                res = analyze_scanned_page(rgb, scale, engine, page)
                res.notes.append(f"PP-OCR 辨識失敗（{self._rapid_failed}），本頁改用 Tesseract。")
                return res, engine.name
        except OcrUnavailable as ex:
            raise EngineError(str(ex)) from ex

    def analyze(
        self,
        page: int,
        ocr: str = "auto",
        ocr_lines: list[dict] | None = None,
        force_ocr: bool = False,
        progress: Progress | None = None,
        quality: str = "auto",
    ) -> dict[str, Any]:
        """``quality``: "auto" reads with the small models and has the medium one check the
        lines it is unsure of; "max" reads everything with the medium models (much slower)."""
        doc = self._need_doc()
        if not 1 <= page <= doc.page_count:
            raise EngineError(f"頁碼超出範圍：{page}")

        def step(stage: str, f: float) -> None:
            if progress is not None and progress(stage, f) is False:
                raise Cancelled()

        t0 = time.time()
        step("text-layer", 0.0)
        glyphs, stats = read_glyphs(doc, page)
        notes: list[str] = []
        if stats.kind == "visible" and not force_ocr:
            blocks = build_blocks(glyphs, page)
            mode = "text"
        else:
            scale = ANALYZE_DPI / 72.0
            step("render", 0.1)
            rgb = doc.render(page, scale)
            step("ocr", 0.2)
            self._max_quality = quality == "max"
            res, used = self._read_scan(rgb, scale, ocr, ocr_lines, page)
            self.page_quality[page] = "max" if quality == "max" else "auto"
            step("fonts", 0.85)
            blocks = res.blocks
            notes += res.notes
            notes.append(f"辨識引擎：{used}。")
            if used == "tesseract" and self._rapid_failed and ocr_lines is None and not any("PP-OCR" in n for n in notes):
                # say why the better engine is not in use, instead of quietly reading worse
                notes.append(f"PP-OCR 未啟用：{self._rapid_failed}")
            self._refine_fonts(blocks, rgb, scale)
            mode = "ocr"
            if stats.kind == "invisible":
                notes.append("此頁已有隱藏的 OCR 文字層；已重新辨識，輸出時舊的隱藏文字會一併更新。")
        with self._lock:
            self.blocks[page] = blocks
        step("done", 1.0)
        return {
            "page": page,
            "mode": mode,
            "blocks": [b.to_json() for b in blocks],
            "notes": notes,
            "seconds": round(time.time() - t0, 2),
        }

    def _refine_fonts(self, blocks: list[Block], rgb: np.ndarray, scale: float) -> None:
        """Font family by shape matching, size from the matched font, snapped to Office sizes.

        Families are decided per *group* of lines (same script, weight, slant and size),
        never per line: the lines of one paragraph must come out in one typeface, both in
        an edited PDF — where a line redrawn in the wrong face shows at once — and in Word.
        """
        H, W = rgb.shape[:2]
        masks: dict[int, np.ndarray] = {}
        for b in blocks:
            r = b.bounds.inflate(1.0, 1.0)
            x0, y0 = max(0, int(r.x * scale)), max(0, int(r.y * scale))
            x1, y1 = min(W, int(r.right * scale) + 1), min(H, int(r.bottom * scale) + 1)
            if x1 - x0 < 8 or y1 - y0 < 6:
                continue
            crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
            masks[b.id], _bg, _thr = ink_mask(crop, b.background)

        cluster = dict(zip((b.id for b in blocks), _size_clusters([b.style.size for b in blocks])))

        def key(b: Block):
            # Weight and slant are left out on purpose: a bold heading or an italic remark
            # is set in the paragraph's typeface far more often than not, and the few
            # glyphs of such a line are poor evidence for a typeface of their own.
            return (_script(b.text), cluster[b.id])

        def judge(script: str, pool: list[str]) -> tuple[dict, dict]:
            groups: dict[Any, list] = {}
            per_block: dict[int, Any] = {}
            # The longest lines of each group carry the evidence; a dozen of them settle the
            # family, and judging every line of a dense page only costs time.
            todo: dict[Any, list[Block]] = {}
            for b in blocks:
                if _script(b.text) == script and b.id in masks:
                    todo.setdefault(key(b), []).append(b)
            picked = [b for bs in todo.values() for b in sorted(bs, key=lambda b: -len(b.text))[:FONT_LINES]]
            for b in picked:
                fs = score_fonts(masks[b.id], b.text, b.style.bold, b.style.italic, self.fonts, scale, pool)
                if fs is not None:
                    per_block[b.id] = fs
                    groups.setdefault(key(b), []).append(fs)
            return settle_families(groups), per_block

        cjk_fam, cjk_scores = judge("cjk", CJK_CANDIDATES)
        # A Latin-only line on a Chinese page is often set in the Chinese font's own
        # letters (標楷體, 新細明體), so those families compete with the Latin ones.
        page_cjk = list(dict.fromkeys(cjk_fam.values()))
        latin_fam, latin_scores = judge("latin", LATIN_CANDIDATES + page_cjk)
        chosen = {**cjk_fam, **latin_fam}
        scores = {**cjk_scores, **latin_scores}

        def nearest(b: Block) -> str | None:
            """Family for a line whose own group could not be judged: the closest judged group."""
            k = key(b)
            same = [(kk, f) for kk, f in chosen.items() if kk[0] == k[0]]
            if not same:
                return None
            same.sort(key=lambda t: abs(t[0][1] - k[1]))
            return same[0][1]

        family_of = {b.id: chosen.get(key(b)) or nearest(b) for b in blocks}

        # The Latin letters inside Chinese lines: the Chinese font's own, or a companion
        # face (Times New Roman next to 標楷體). Decided per Chinese family and size group.
        lgroups: dict[Any, list] = {}
        latin_count = lambda b: sum(1 for c in b.text if c.isascii() and c.isalnum())
        for b in sorted(blocks, key=lambda b: -latin_count(b)):
            fam = family_of[b.id]
            if _script(b.text) != "cjk" or not fam or b.id not in masks:
                continue
            if len(lgroups.get((fam, cluster[b.id]), ())) >= FONT_LINES // 2:
                continue
            fs = score_latin(masks[b.id], b.text, b.style.bold, b.style.italic, self.fonts, scale, fam, LATIN_CANDIDATES)
            if fs is not None:
                lgroups.setdefault((fam, cluster[b.id]), []).append(fs)
        latin_of = settle_families(lgroups)

        def companion(b: Block) -> str | None:
            fam = family_of[b.id]
            if _script(b.text) != "cjk" or not fam:
                return None
            hit = latin_of.get((fam, cluster[b.id]))
            if hit is None:
                near = sorted((abs(k[1] - cluster[b.id]), v) for k, v in latin_of.items() if k[0] == fam)
                hit = near[0][1] if near else None
            return None if hit is None or hit == fam else hit

        # Size from the chosen font's own proportions. Lines that were not measured take the
        # correction their group's measured lines agreed on.
        measured: dict[int, float] = {}
        ratios: dict[Any, list[float]] = {}
        for b in blocks:
            fam, fs = family_of[b.id], scores.get(b.id)
            if fam and fs is not None and fs.em.get(fam) and fs.weight >= 4:
                r = fs.ink_h / fs.em[fam] / max(b.style.size, 0.1)
                if 0.7 <= r <= 1.4:
                    measured[b.id] = r
                    ratios.setdefault(key(b), []).append(r)
        typical = {k: float(np.median(v)) for k, v in ratios.items()}

        for b in blocks:
            fam = family_of[b.id]
            style = b.style
            size = style.size * measured.get(b.id, typical.get(key(b), 1.0))
            style = style.with_(family=fam, latin_family=companion(b), size=snap_size(size))
            b.style = style
            for run in b.runs:
                run.style = style
        self._settle_sizes(blocks)

    @staticmethod
    def _settle_sizes(blocks: list[Block]) -> None:
        """Steadies the sizes of scanned lines.

        Glyph matching sizes each line on its own and scatters one 10.5 pt list over 9–12 pt,
        which shows at once in the Word file. Chinese characters advance one em each, so a
        line of them gives its size from its length; that is used when it agrees roughly
        with the glyph size (far smaller glyphs mean spaced-out text, where the length says
        nothing). Then sizes that are close are pulled onto the sizes the page actually uses.
        """
        import unicodedata

        from .analysis.style import regularize_sizes

        sizes: list[float] = []
        reliable: list[bool] = []
        for b in blocks:
            t = b.text.strip()
            wide = sum(1 for ch in t if unicodedata.east_asian_width(ch) in "WF")
            size = b.style.size
            ok = False
            if wide >= 6 and wide >= 0.6 * len(t.replace(" ", "")):
                ems = sum(0.3 if ch.isspace() else (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.55) for ch in t)
                pitch = b.bounds.w / ems
                if 0.85 * pitch <= size <= 1.25 * pitch:
                    size, ok = pitch, True
            sizes.append(size)
            reliable.append(ok)
        sizes = regularize_sizes(sizes, reliable, pull_reliable=0.08, pull_unreliable=0.15)
        for b, sz in zip(blocks, sizes):
            if abs(sz - b.style.size) < 0.01:
                continue
            style = b.style.with_(size=snap_size(sz))
            b.style = style
            for run in b.runs:
                run.style = style

    # ---- preview / export -----------------------------------------------------------
    def _edits(self, edits: list[dict]) -> list[Edit]:
        out = [Edit.from_json(e) for e in edits]
        # The shell sends back family/size/weight but not the Latin companion font; while
        # the family is the one that was analysed, the companion still goes with it.
        with self._lock:
            known = {(b.page, b.id): b.style for bs in self.blocks.values() for b in bs}
        for e in out:
            st = known.get((e.page, e.id))
            if st is not None and st.latin_family and not e.style.latin_family and e.style.family == st.family:
                e.style = e.style.with_(latin_family=st.latin_family)
                e.runs = [Run(r.text, r.style.with_(latin_family=st.latin_family) if r.style.family == st.family else r.style, r.x0, r.x1) for r in e.runs]
        return out

    def preview(
        self, edits: list[dict], pages: list[int] | None = None, dpi: float = PREVIEW_DPI, erases: list[dict] | None = None
    ) -> dict[str, Any]:
        doc = self._need_doc()
        es = self._edits(edits)
        ers = [Erase.from_json(d) for d in erases or []]
        pdf_path = self._tmp("preview", ".pdf")
        report = write_edits(doc.path, es, pdf_path, fonts=self.fonts, doc=doc, erases=ers)
        want = pages or report.pages_changed or [1]
        out = PdfDoc(pdf_path)
        try:
            images = []
            for p in want:
                img = self._tmp(f"preview{p}", ".png")
                w, h = out.render_png(p, dpi / 72.0, img)
                images.append({"page": p, "path": img, "width": w, "height": h, "dpi": dpi})
        finally:
            out.close()
        return {"pdf": pdf_path, "images": images, "report": report.to_json()}

    def export(
        self,
        edits: list[dict],
        out_path: str,
        verify: bool = True,
        audit: bool = True,
        audit_text: bool = True,
        erases: list[dict] | None = None,
    ) -> dict[str, Any]:
        """``erases``: boxes to paint over, ``{"page", "rect": [x, y, w, h], "mode", "color"}``
        (see :mod:`erase`)."""
        doc = self._need_doc()
        out_path = os.path.abspath(out_path)
        if os.path.normcase(out_path) == os.path.normcase(doc.path):
            raise EngineError("不能覆蓋原始檔，請另存新檔。")
        es = self._edits(edits)
        ers = [Erase.from_json(d) for d in erases or []]
        bad = [e.page for e in ers if not 1 <= e.page <= doc.page_count]
        if bad:
            raise EngineError(f"頁碼超出範圍：{bad[0]}")
        src_sha = sha256_file(doc.path) if audit else None
        report = write_edits(doc.path, es, out_path, fonts=self.fonts, doc=doc, erases=ers)
        result = {"report": report.to_json(), "erases": [e.to_json() for e in ers]}
        if verify:
            result["verify"] = verify_output(doc, out_path, es, report.pages_changed, erases=ers)
        if audit:
            # The PDF is already written; a record that fails must not turn that into an
            # error. The shell shows the failure as a warning instead.
            try:
                rec = build_edit_audit(doc, out_path, es, result.get("verify"), audit_text, src_sha, erases=ers)
                result["audit"] = write_audit(rec, out_path)
            except Exception as ex:
                result["auditError"] = f"{type(ex).__name__}: {ex}"
        return result

    # ---- page organiser -------------------------------------------------------------
    def thumbnails(self, pages: list[int] | None = None, dpi: float = 36, progress: Progress | None = None) -> dict[str, Any]:
        doc = self._need_doc()
        want = list(pages) if pages else list(range(1, doc.page_count + 1))
        bad = [p for p in want if not 1 <= int(p) <= doc.page_count]
        if bad:
            raise EngineError(f"頁碼超出範圍：{bad[0]}")
        dpi = min(max(float(dpi), 12.0), 144.0)
        images = []
        for i, p in enumerate(want):
            if progress is not None and progress(f"頁 {p}", i / len(want)) is False:
                raise Cancelled()
            path = self._tmp(f"thumb{p}", ".png")
            w, h = doc.render_png(int(p), dpi / 72.0, path)
            images.append({"page": int(p), "path": path, "width": w, "height": h, "dpi": dpi})
        if progress is not None:
            progress("done", 1.0)
        return {"images": images}

    def arrange(self, plan: list[dict], out_path: str, audit: bool = True) -> dict[str, Any]:
        """Writes a new PDF with the planned pages (order = plan order, pages not in the
        plan are deleted, ``rotate`` is extra clockwise degrees). Works on the original
        file, not on text edits that have not been exported yet."""
        doc = self._need_doc()
        out_path = os.path.abspath(out_path)
        if os.path.normcase(out_path) == os.path.normcase(doc.path):
            raise EngineError("不能覆蓋原始檔，請另存新檔。")
        try:
            steps = validate_plan(plan, doc.page_count)
            src_sha = sha256_file(doc.path) if audit else None
            info = _arrange_pages(doc.path, plan, out_path)
        except PlanError as ex:
            raise EngineError(str(ex)) from None
        result: dict[str, Any] = {
            "path": out_path,
            "pageCount": info["pageCount"],
            "sha256": sha256_file(out_path),
            "deleted": info["deleted"],
        }
        if audit:
            try:
                norm = [{"src": s, "rotate": r} for s, r in steps]
                rec = build_arrange_audit(doc, out_path, norm, info["deleted"], src_sha)
                result["audit"] = write_audit(rec, out_path)
            except Exception as ex:
                result["auditError"] = f"{type(ex).__name__}: {ex}"
        return result

    # ---- Word -----------------------------------------------------------------------
    def export_docx(
        self,
        out_path: str,
        pages: list[int] | None = None,
        edits: list[dict] | None = None,
        ocr: str = "auto",
        progress: Progress | None = None,
        quality: str = "auto",
    ) -> dict[str, Any]:
        """Writes the chosen pages as a flowing Word document (see :mod:`docstruct`)."""
        from .docstruct import PageInput
        from .tables import find_grids

        doc = self._need_doc()
        out_path = os.path.abspath(out_path)
        if os.path.normcase(out_path) == os.path.normcase(doc.path):
            raise EngineError("不能覆蓋原始檔，請另存新檔。")
        want = sorted(set(pages or range(1, doc.page_count + 1)))
        bad = [p for p in want if not 1 <= p <= doc.page_count]
        if bad:
            raise EngineError(f"頁碼超出範圍：{bad[0]}")
        by_page: dict[int, dict[int, Edit]] = {}
        for e in self._edits(edits or []):
            by_page.setdefault(e.page, {})[e.id] = e

        def step(stage: str, f: float) -> None:
            if progress is not None and progress(stage, f) is False:
                raise Cancelled()

        inputs: list[PageInput] = []
        n = len(want)
        for i, pno in enumerate(want):
            lo, span = i / n, 1.0 / n
            step(f"頁 {pno}", lo)
            # A scanned page already read at the other quality is read again — unless it
            # carries edits, which belong to the lines as they were read.
            stale = self.page_quality.get(pno) not in (None, "max" if quality == "max" else "auto") and pno not in by_page
            if pno not in self.blocks or stale:
                self.analyze(pno, ocr=ocr, quality=quality, force_ocr=stale, progress=lambda st, f, lo=lo, span=span: (
                    progress is None or progress(f"頁 {pno}", lo + span * 0.7 * f) is not False))
            blocks = self._with_edits(self.blocks[pno], by_page.get(pno, {}))
            scale = WORD_DPI / 72.0
            rgb = doc.render(pno, scale)
            geo = doc.pages[pno - 1].geometry
            # letters are not rules: strokes inside the page's text blocks are left out
            grids = find_grids(rgb, scale, [(b.bounds.x * scale, b.bounds.y * scale, b.bounds.right * scale, b.bounds.bottom * scale)
                                            for b in blocks if b.text.strip()])
            inputs.append(PageInput(pno, geo.display_w, geo.display_h, blocks, grids, rgb, scale))
            step(f"頁 {pno}", lo + span * 0.9)
        step("寫入 Word", 0.95)
        from .docstruct import structure_document
        from .docxout import write_docx

        pages_doc = structure_document(inputs)
        stats = write_docx(pages_doc, out_path, title=os.path.splitext(os.path.basename(doc.path))[0])
        step("done", 1.0)
        return {"path": out_path, "pages": want, **stats}

    @staticmethod
    def _with_edits(blocks: list[Block], edits: dict[int, Edit]) -> list[Block]:
        if not edits:
            return list(blocks)
        out = []
        for b in blocks:
            e = edits.get(b.id)
            if e is None or not e.modified:
                out.append(b)
                continue
            if not e.text.strip():
                continue  # deleted in the editor
            runs = e.runs if e.runs and "".join(r.text for r in e.runs) == e.text else [Run(e.text, e.style, e.bounds.x, e.bounds.right)]
            out.append(replace(b, text=e.text, runs=runs, style=e.style, bounds=e.bounds, char_x=None))
        return out

    def fonts_list(self) -> dict[str, Any]:
        return {"families": self.fonts.families()}


def verify_output(
    src: PdfDoc, out_path: str, edits: list[Edit], pages: list[int], dpi: float = 72.0, erases: list[Erase] | None = None
) -> dict[str, Any]:
    """Renders changed pages of both files and checks that nothing moved outside the
    edited lines. A pixel that changed far from every edit means the writer touched
    something it should not have."""
    out = PdfDoc(out_path)
    scale = dpi / 72.0
    problems = []
    try:
        if out.page_count != src.page_count:
            problems.append({"page": 0, "issue": "page-count"})
        for p in pages:
            a = src.render(p, scale).astype(np.int16)
            b = out.render(p, scale).astype(np.int16)
            if a.shape != b.shape:
                problems.append({"page": p, "issue": "page-size"})
                continue
            diff = np.abs(a - b).max(axis=2) > 24
            allowed = np.zeros(diff.shape, dtype=bool)
            for e in edits:
                if e.page != p or not e.modified:
                    continue
                grow = 0.6 * e.style.size
                for r in (e.original_bounds, e.bounds):
                    rr = r.inflate(grow + max(0.0, 0.0), grow)
                    # the new text may run on to the right of its box
                    rr = Rect(rr.x - grow, rr.y, rr.w + grow + max(0.0, len(e.text) * e.style.size - rr.w), rr.h)
                    y0, y1 = max(0, int(rr.y * scale)), int(rr.bottom * scale) + 1
                    x0, x1 = max(0, int(rr.x * scale)), int(rr.right * scale) + 1
                    allowed[y0:y1, x0:x1] = True
            rewritten = False
            for er in erases or []:
                if er.page != p:
                    continue
                rr = er.rect.inflate(2.0)
                y0, y1 = max(0, int(rr.y * scale)), int(rr.bottom * scale) + 1
                x0, x1 = max(0, int(rr.x * scale)), int(rr.right * scale) + 1
                allowed[y0:y1, x0:x1] = True
                rewritten = rewritten or er.removed
            if rewritten:
                # The page's picture was stored again; a JPEG comes back a shade different
                # everywhere. Only a real change counts: clearly different, and not a speck.
                import cv2

                strong = (np.abs(a - b).max(axis=2) > 60).astype(np.uint8)
                diff = cv2.morphologyEx(strong, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) > 0
            stray = int((diff & ~allowed).sum())
            if stray > 0:
                problems.append({"page": p, "issue": "changed-outside-edits", "pixels": stray})
    finally:
        out.close()
    return {"ok": not problems, "problems": problems}
