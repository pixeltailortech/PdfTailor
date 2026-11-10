"""Tesseract (Apache-2.0) driven through its command line.

The command line, not a binding, on purpose: no native library has to be linked into
the engine, the user can drop in a better ``chi_tra`` model, and a bundled
``tesseract.exe`` works the same as an installed one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from html.parser import HTMLParser
import sys
import tempfile

import numpy as np
from PIL import Image

from .base import OcrEngine, OcrLine, OcrUnavailable, OcrWord

_WIN_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_tesseract() -> str | None:
    """Looks for tesseract: env var, next to the engine, PATH, usual install folders."""
    env = os.environ.get("PDFTAILOR_TESSERACT")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    for root in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        for sub in ("tesseract", "Tesseract-OCR", "."):
            for exe in ("tesseract.exe", "tesseract"):
                p = os.path.join(root, sub, exe)
                if os.path.isfile(p):
                    return p
    found = shutil.which("tesseract")
    if found:
        return found
    for p in _WIN_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF


def join_words(words: list[str]) -> str:
    """Joins Tesseract words back into a line.

    Tesseract puts a space between neighbouring Chinese characters ("發 票"), which is an
    artefact. A space is kept only between two Latin-ish neighbours.
    """
    out = ""
    for w in words:
        if not w:
            continue
        if out:
            a, b = out[-1], w[0]
            if not (_is_cjk(a) or _is_cjk(b)):
                out += " "
        out += w
    return out


class TesseractOcr(OcrEngine):
    name = "tesseract"
    can_recrop = True

    def __init__(self, languages: str = "chi_tra+eng", exe: str | None = None, tessdata: str | None = None):
        self.exe = exe or find_tesseract()
        if not self.exe:
            raise OcrUnavailable(
                "找不到 Tesseract。請安裝 Tesseract OCR 並含 chi_tra 語言資料，"
                "或把 tesseract 資料夾放在引擎旁邊（也可用環境變數 PDFTAILOR_TESSERACT 指定）。"
            )
        bundled = os.path.join(os.path.dirname(self.exe), "tessdata")
        # A copied Tesseract folder must use its own language data, not whatever an
        # unrelated install put in TESSDATA_PREFIX.
        self.tessdata = tessdata or (bundled if os.path.isdir(bundled) else os.environ.get("TESSDATA_PREFIX"))
        self.languages = self._usable_languages(languages)

    # ---- plumbing ------------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["OMP_THREAD_LIMIT"] = "1"  # callers parallelise across crops themselves
        if self.tessdata:
            env["TESSDATA_PREFIX"] = self.tessdata
        return env

    def _run(self, args: list[str], timeout: float = 120) -> str:
        proc = subprocess.run(
            [self.exe, *args],
            capture_output=True,
            timeout=timeout,
            env=self._env(),
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0 and not proc.stdout:
            raise OcrUnavailable(proc.stderr.decode("utf-8", "replace").strip() or "Tesseract 執行失敗")
        return proc.stdout.decode("utf-8", "replace")

    def installed_languages(self) -> set[str]:
        try:
            out = self._run(["--list-langs"], timeout=30)
        except Exception:
            return set()
        return {l.strip() for l in out.splitlines()[1:] if l.strip()}

    def _usable_languages(self, wanted: str) -> str:
        have = self.installed_languages()
        picked = [l for l in wanted.split("+") if l in have]
        if not picked:
            if "eng" in have:
                picked = ["eng"]
            else:
                raise OcrUnavailable(
                    "Tesseract 沒有可用的語言資料。請安裝 chi_tra（繁體中文）與 eng 的 traineddata。"
                )
        return "+".join(picked)

    @property
    def has_chinese(self) -> bool:
        return "chi_tra" in self.languages or "chi_sim" in self.languages

    @staticmethod
    def _save(rgb: np.ndarray, path: str) -> None:
        Image.fromarray(rgb).save(path, format="PNG")

    # ---- engine API ----------------------------------------------------------------
    def recognize_page(self, rgb: np.ndarray, psm: int = 3) -> list[OcrLine]:
        with tempfile.TemporaryDirectory(prefix="pt-ocr-") as tmp:
            img = os.path.join(tmp, "p.png")
            self._save(rgb, img)
            hocr = self._run([img, "stdout", "-l", self.languages, "--psm", str(psm), "--dpi", str(int(round(self.dpi))), "-c", "tessedit_create_hocr=1", "-c", "tessedit_create_txt=0", "-c", "hocr_char_boxes=1"])
        return self._parse_lines(hocr)

    def recognize_crop(self, rgb: np.ndarray) -> tuple[str, float]:
        with tempfile.TemporaryDirectory(prefix="pt-ocr-") as tmp:
            img = os.path.join(tmp, "c.png")
            self._save(rgb, img)
            hocr = self._run([img, "stdout", "-l", self.languages, "--psm", "7", "--dpi", str(int(round(self.dpi))), "-c", "tessedit_create_hocr=1", "-c", "tessedit_create_txt=0", "-c", "hocr_char_boxes=1"])
        lines = self._parse_lines(hocr)
        if not lines:
            return "", -1.0
        text = join_words([l.text for l in lines]) if len(lines) > 1 else lines[0].text
        confs = [l.confidence for l in lines if l.confidence >= 0]
        return text, (sum(confs) / len(confs) if confs else -1.0)

    @staticmethod
    def _parse_lines(hocr: str) -> list[OcrLine]:
        """Lines from Tesseract's hOCR, with a box per character (``hocr_char_boxes``).

        The character boxes let the analyser cut a line at a table rule or a symbol and keep
        the engine's reading of each piece. Tesseract's LSTM boxes are approximate (a
        character can be reported a little wide), which is enough to tell cells apart.
        """
        p = _HocrParser()
        p.feed(hocr)
        p.close()
        lines: list[OcrLine] = []
        for chars, box in zip(p.lines, p.boxes):
            chars = [c for c in chars if c.text.strip()]
            if not chars:
                continue
            # The LSTM sometimes reports one character as wide as the next several
            # (中 868–1190 px while 國 starts at 986). Its left edge is right, so the box is
            # cut to a character's usual width; otherwise it would land in the wrong piece
            # when the line is cut apart.
            hmed = float(np.median([c.h for c in chars]))
            for c in chars:
                limit = 1.3 * hmed * max(1, len(c.text.strip()))
                if c.w > limit:
                    c.w = limit
            text = join_chars(chars)
            if not text:
                continue
            x0 = min(c.x for c in chars)
            x1 = max(c.x + c.w for c in chars)
            y0 = min(c.y for c in chars)
            y1 = max(c.y + c.h for c in chars)
            confs = [c.confidence for c in chars if c.confidence >= 0]
            lines.append(OcrLine(text, x0, y0, x1 - x0, y1 - y0, sum(confs) / len(confs) if confs else -1.0, chars))
        return lines


def join_chars(chars: list[OcrWord]) -> str:
    """Joins characters back into text: a space only where a new word starts between two
    Latin-ish neighbours (Tesseract's spaces between Chinese characters are artefacts)."""
    out = ""
    for c in chars:
        t = c.text.strip()
        if not t:
            continue
        if out and c.start and not (_is_cjk(out[-1]) or _is_cjk(t[0])):
            out += " "
        out += t
    return out


def _bbox(title: str, key: str) -> tuple[int, int, int, int] | None:
    for part in title.split(";"):
        part = part.strip()
        if part.startswith(key + " "):
            try:
                a, b, c, d = (int(v) for v in part[len(key) + 1:].split()[:4])
            except ValueError:
                return None
            return a, b, c, d
    return None


def _num(title: str, key: str) -> float:
    for part in title.split(";"):
        part = part.strip()
        if part.startswith(key + " "):
            try:
                return float(part[len(key) + 1:].split()[0])
            except (ValueError, IndexError):
                return -1.0
    return -1.0


class _HocrParser(HTMLParser):
    """Collects ocr_line → ocrx_word → ocrx_cinfo into per-line character lists."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[list[OcrWord]] = []
        self.boxes: list[tuple[int, int, int, int] | None] = []  # each line's own bbox
        self._stack: list[str] = []  # class of every open span
        self._word: tuple[tuple[int, int, int, int], float] | None = None
        self._word_has_chars = False
        self._word_text = ""
        self._char: tuple[tuple[int, int, int, int], float] | None = None
        self._char_text = ""
        self._first = True

    def handle_starttag(self, tag, attrs):
        if tag != "span":
            return
        a = dict(attrs)
        cls = a.get("class", "")
        title = a.get("title", "")
        self._stack.append(cls)
        if cls in ("ocr_line", "ocr_textfloat", "ocr_header", "ocr_caption"):
            self.lines.append([])
            self.boxes.append(_bbox(title, "bbox"))
        elif cls == "ocrx_word":
            box = _bbox(title, "bbox")
            self._word = (box, _num(title, "x_wconf")) if box else None
            self._word_has_chars = False
            self._word_text = ""
            self._first = True
        elif cls == "ocrx_cinfo":
            box = _bbox(title, "x_bboxes")
            self._char = (box, _num(title, "x_conf")) if box else None
            self._char_text = ""
            self._word_has_chars = True

    def handle_data(self, data):
        if not self._stack:
            return
        if self._stack[-1] == "ocrx_cinfo":
            self._char_text += data
        elif self._stack[-1] == "ocrx_word":
            self._word_text += data

    def handle_endtag(self, tag):
        if tag != "span" or not self._stack:
            return
        cls = self._stack.pop()
        if not self.lines:
            return
        if cls == "ocrx_cinfo" and self._char is not None:
            (x0, y0, x1, y1), conf = self._char
            if self._char_text:
                self.lines[-1].append(OcrWord(self._char_text, x0, y0, x1 - x0, y1 - y0, conf, self._first))
                self._first = False
            self._char = None
        elif cls == "ocrx_word" and self._word is not None:
            if not self._word_has_chars and self._word_text.strip():
                (x0, y0, x1, y1), conf = self._word
                self.lines[-1].append(OcrWord(self._word_text.strip(), x0, y0, x1 - x0, y1 - y0, conf, True))
            self._word = None
