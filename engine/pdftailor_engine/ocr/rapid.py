"""PP-OCR through RapidOCR on onnxruntime — Apache-2.0, fully offline.

Tesseract stops reading Chinese once the characters are about ten pixels tall (a 7 pt
contract scanned at 100 dpi comes out as rows of capital letters). The PP-OCR models read
such pages, and one recognition model covers Traditional and Simplified Chinese, English
and Japanese, so there is no per-language choice to get wrong.

Two sizes of PP-OCRv6 work together. The *small* models find and read every line; they
are some thirty times faster than the *medium* ones and read clean print just as well.
Lines the small model is not sure of (confidence under 95%) are read again by the
*medium* recognition model, and the surer reading is kept — so the heavy model only runs
where it can make a difference. With ``max_quality`` set (the "highest accuracy" box in
the app, or ``PDFTAILOR_OCR_QUALITY=max``) everything is read with the medium models
instead, which takes several times longer. The text-direction classifier is left out (scanned office pages
are upright).

The models are looked for, in this order, in ``PDFTAILOR_OCR_MODELS``, a ``models``
folder next to the engine, and ``vendor/ocr-models`` of the source tree. RapidOCR
downloads them once on a machine that has internet access:
``python -m pdftailor_engine.ocr.rapid --fetch <folder>``.

RapidOCR returns text *segments* (it splits a line at wide gaps). Segments on one row
that sit close together are joined back into a line, and every line gets approximate
character boxes so the analyser can cut it at table rules.
"""

from __future__ import annotations

import os
import sys
import unicodedata

import numpy as np

from .base import OcrEngine, OcrLine, OcrUnavailable, OcrWord

PAGE_SIDE = 3600  # a 300 dpi A4 page is 3508 px long: lines are cut out of the full-size page
DET_SIDE = 2496  # longest side the detector looks at; finding lines needs no more, and its cost grows with area
SURE = 0.95  # below this the small model's reading is checked by the medium model
MIN_SCORE = 0.2  # RapidOCR drops lines below this; kept low so faint lines show up (flagged) instead of vanishing


# Simplified characters that have exactly one traditional form and are not themselves used
# in traditional text. The recognition model sometimes answers a soft 組 with 组; in a page
# that is otherwise traditional such a character is a slip of the model, never the print.
# Characters with two traditional readings (后 里 干 历 并 范 …) are left out on purpose.
_SIMP = "与业东两个为么义习乡书买亏产亲仅从仓们众优会传伤伦伪体佣侠侦侧俭债倾储儿兑党兰关兴养兽内册写军农决况冻净减凑击则刚创别剂剧劝办务动劳势勋区医华协单卖卫厂厅压厌县参双发变叙叶号吗听员响哑唤啸喷团园围图圆圣场坏块坚垄垒垦处备头学实对帐开异当总户执扩护报担拟拥择挂挤挥捞损换据掷掺搁摄摆摇撑敌数断无旧时显晓暂术机杀权条来极构标样档桥检楼榄横毁毕气汇汉沟没沪泽洁浅浊测济浓润涨渐渔湾湿溃滚满滤灭灯灵灾点烂烦热爱爷牵状犹独狭猎猪环现琐电画畅疗疯痒盐监盖盘矿码础确礼祸离种积称稳窃竞笔筑筹签简类粮紧红约级纪纯纲纳纵纸纹纺线练组细织终经结绕绘给绝统继绩绪续维综缓编缘缩缴网罗罚罢职联聪肃肠肤胁胆胜脉脏脑脚腊舰艰艺节芦苏药获萝营萧蓝蔼虏虑补装见规视览觉计讯访证评识译试诗诚话该详语误说请读课调谅谈谊谋谓谢谱财货质贫购贯贰贵贷贸费贺赁资赋赌赎赏赐赔赖赚赛赞趋跃践车转轮软轰载辆辈辉辑输辩边辽达迁过迈运还这进连迟适选逊递逻遗邮邻郑释钢钥钩钮钱钻铁铃铜铝银链销锁锅错锦键镇镜长门闪闭问闯闲间闷闹闻阀阅队阳阴阵阶际陆陈陕险随隐难雾静顶项顺须顾顿预领频颗题额风飞饥饭饮饰饱馆驱驳驶驻验骗鱼鲜鸟鸡鸣麦黄齐齿龙龟"
_TRAD = "與業東兩個為麼義習鄉書買虧產親僅從倉們眾優會傳傷倫偽體傭俠偵側儉債傾儲兒兌黨蘭關興養獸內冊寫軍農決況凍淨減湊擊則剛創別劑劇勸辦務動勞勢勳區醫華協單賣衛廠廳壓厭縣參雙發變敘葉號嗎聽員響啞喚嘯噴團園圍圖圓聖場壞塊堅壟壘墾處備頭學實對帳開異當總戶執擴護報擔擬擁擇掛擠揮撈損換據擲摻擱攝擺搖撐敵數斷無舊時顯曉暫術機殺權條來極構標樣檔橋檢樓欖橫毀畢氣匯漢溝沒滬澤潔淺濁測濟濃潤漲漸漁灣濕潰滾滿濾滅燈靈災點爛煩熱愛爺牽狀猶獨狹獵豬環現瑣電畫暢療瘋癢鹽監蓋盤礦碼礎確禮禍離種積稱穩竊競筆築籌簽簡類糧緊紅約級紀純綱納縱紙紋紡線練組細織終經結繞繪給絕統繼績緒續維綜緩編緣縮繳網羅罰罷職聯聰肅腸膚脅膽勝脈臟腦腳臘艦艱藝節蘆蘇藥獲蘿營蕭藍藹虜慮補裝見規視覽覺計訊訪證評識譯試詩誠話該詳語誤說請讀課調諒談誼謀謂謝譜財貨質貧購貫貳貴貸貿費賀賃資賦賭贖賞賜賠賴賺賽贊趨躍踐車轉輪軟轟載輛輩輝輯輸辯邊遼達遷過邁運還這進連遲適選遜遞邏遺郵鄰鄭釋鋼鑰鉤鈕錢鑽鐵鈴銅鋁銀鏈銷鎖鍋錯錦鍵鎮鏡長門閃閉問闖閒間悶鬧聞閥閱隊陽陰陣階際陸陳陝險隨隱難霧靜頂項順須顧頓預領頻顆題額風飛飢飯飲飾飽館驅駁駛駐驗騙魚鮮鳥雞鳴麥黃齊齒龍龜"
_S2T = str.maketrans(_SIMP, _TRAD)
_SIMP_SET, _TRAD_SET = frozenset(_SIMP), frozenset(_TRAD)


def is_traditional_page(texts: list[str]) -> bool:
    """True when the traditional-only characters clearly outnumber the simplified-only ones."""
    trad = sum(1 for t in texts for c in t if c in _TRAD_SET)
    simp = sum(1 for t in texts for c in t if c in _SIMP_SET)
    return trad >= 8 and trad >= 2 * simp


def to_traditional(text: str) -> str:
    return text.translate(_S2T)


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in "WF"


def model_dirs() -> list[str]:
    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    out = []
    env = os.environ.get("PDFTAILOR_OCR_MODELS")
    if env:
        out.append(env)
    for root in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here)), os.path.dirname(os.path.dirname(os.path.dirname(here)))):
        out.append(os.path.join(root, "models"))
        out.append(os.path.join(root, "vendor", "ocr-models"))
    return out


def _rank(name: str) -> int:
    """Lower is better: PP-OCRv6 medium, PP-OCRv5 server, then the lighter models."""
    low = name.lower()
    order = [("v6", "medium"), ("v5", "server"), ("v6", "small"), ("v5", "mobile"), ("v6", "tiny")]
    for i, (ver, size) in enumerate(order):
        if ver in low and size in low:
            return i
    return len(order)


def describe(path: str) -> tuple[str, str]:
    """(version, size) of a model file, from its name: ("PP-OCRv6", "medium")."""
    low = os.path.basename(path).lower()
    ver = "PP-OCRv6" if "v6" in low else "PP-OCRv5" if "v5" in low else "PP-OCRv4"
    for size in ("medium", "server", "small", "mobile", "tiny"):
        if size in low:
            return ver, size
    return ver, "mobile"


def _folders() -> list[str]:
    """Model folders, then the models RapidOCR ships inside its own package (the small ones)."""
    out = [d for d in model_dirs() if os.path.isdir(d)]
    try:
        import rapidocr

        pkg = os.path.join(os.path.dirname(rapidocr.__file__), "models")
        if os.path.isdir(pkg):
            out.append(pkg)
    except Exception:
        pass
    return out


def find_model(kind: str, size: str | None = None) -> str | None:
    """Best ``det``/``rec`` model on disk, or the best one of ``size`` ("small", "medium")."""
    best: tuple[int, str] | None = None
    for d in _folders():
        for name in os.listdir(d):
            low = name.lower()
            if not low.endswith(".onnx") or "cls" in low or kind not in low:
                continue
            if size is not None and size not in low:
                continue
            cand = (_rank(name), os.path.join(d, name))
            if best is None or cand[0] < best[0]:
                best = cand
    return best[1] if best else None


def find_models() -> dict[str, str]:
    """``{"det": path, "rec": path}`` of the most accurate models on disk; {} unless both exist."""
    det, rec = find_model("det"), find_model("rec")
    return {"det": det, "rec": rec} if det and rec else {}


def _char_words(text: str, x: float, y: float, w: float, h: float, conf: float) -> list[OcrWord]:
    """Approximate box per character: full-width characters one unit, others about half."""
    units = [(1.0 if _is_wide(c) else (0.3 if c.isspace() else 0.55)) for c in text]
    total = sum(units) or 1.0
    out: list[OcrWord] = []
    pen = x
    start = True
    for c, u in zip(text, units):
        cw = w * u / total
        if c.isspace():
            start = True
        else:
            out.append(OcrWord(c, pen, y, cw, h, conf, start or _is_wide(c)))
            start = False
        pen += cw
    return out


def _join(a: str, b: str) -> str:
    if not a or not b:
        return a + b
    return a + b if (_is_wide(a[-1]) or _is_wide(b[0])) else a + " " + b


def merge_segments(segs: list[tuple[str, float, float, float, float, float]]) -> list[OcrLine]:
    """Joins segments of one printed line. ``segs``: (text, x, y, w, h, confidence)."""
    segs = sorted(segs, key=lambda s: (s[2] + s[4] / 2, s[1]))
    rows: list[list[tuple]] = []
    for s in segs:
        cy = s[2] + s[4] / 2
        for row in rows:
            r = row[-1]
            if abs((r[2] + r[4] / 2) - cy) <= 0.45 * min(r[4], s[4]):
                row.append(s)
                break
        else:
            rows.append([s])
    lines: list[OcrLine] = []
    for row in rows:
        row.sort(key=lambda s: s[1])
        group = [row[0]]
        groups = [group]
        for s in row[1:]:
            p = group[-1]
            gap = s[1] - (p[1] + p[3])
            # a gap wider than about a character and a half is a column or a tab stop
            if gap > 1.6 * max(p[4], s[4]) or abs(p[4] - s[4]) > 0.45 * max(p[4], s[4]):
                group = [s]
                groups.append(group)
            else:
                group.append(s)
        for g in groups:
            text = ""
            words: list[OcrWord] = []
            for t, x, y, w, h, c in g:
                text = _join(text, t)
                words.extend(_char_words(t, x, y, w, h, c))
            x0 = min(s[1] for s in g)
            y0 = min(s[2] for s in g)
            x1 = max(s[1] + s[3] for s in g)
            y1 = max(s[2] + s[4] for s in g)
            n = sum(len(s[0]) for s in g) or 1
            conf = sum(s[5] * len(s[0]) for s in g) / n
            lines.append(OcrLine(text, x0, y0, x1 - x0, y1 - y0, conf, words))
    lines.sort(key=lambda l: (l.y + l.h / 2, l.x))
    return lines


class RapidOcr(OcrEngine):
    can_recrop = True

    def __init__(self, download_to: str | None = None):
        try:
            from rapidocr import RapidOCR  # noqa: F401
        except Exception as ex:  # not installed in this build
            raise OcrUnavailable(f"RapidOCR 未安裝：{ex}") from ex
        self._fast = None  # small detection + small recognition
        self._best = None  # medium models
        self._traditional = False  # the page last read was in traditional Chinese
        if download_to:
            self._best = self._make(None, None, download_to)
            self.max_quality = True
            self._label = ("PP-OCRv6", "medium")
            return
        best = find_models()
        if not best:
            raise OcrUnavailable("找不到 PP-OCR 模型檔（models 資料夾裡要有偵測 det 與辨識 rec 兩個 .onnx）。")
        fast = {"det": find_model("det", "small"), "rec": find_model("rec", "small")}
        self.max_quality = os.environ.get("PDFTAILOR_OCR_QUALITY", "").strip().lower() == "max"
        self._best = self._make(best["det"], best["rec"], None)
        self._label = describe(best["rec"])
        if all(fast.values()) and fast["rec"] != best["rec"]:
            # models are only loaded when first used, so keeping both ready costs nothing
            self._fast = self._make(fast["det"], fast["rec"], None)

    @property
    def name(self) -> str:
        ver, size = self._label
        return f"{ver} small + {size}" if self._quick() is not None else f"{ver} {size}"

    def _quick(self):
        """The small models, unless everything is to be read with the medium ones."""
        return None if self.max_quality else self._fast

    @staticmethod
    def _make(det: str | None, rec: str | None, download_to: str | None):
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

        params: dict = {
            "Global.use_cls": False,
            "Global.max_side_len": PAGE_SIDE,
            "Global.text_score": MIN_SCORE,
            "Global.log_level": "info" if download_to else "warning",
            "Det.limit_type": "max",
            "Det.limit_side_len": DET_SIDE,
        }
        versions = {"PP-OCRv6": OCRVersion.PPOCRV6, "PP-OCRv5": OCRVersion.PPOCRV5, "PP-OCRv4": OCRVersion.PPOCRV4}
        sizes = {"medium": ModelType.MEDIUM, "server": ModelType.SERVER, "small": ModelType.SMALL, "mobile": ModelType.MOBILE, "tiny": ModelType.TINY}
        for stage, path in (("Det", det), ("Rec", rec)):
            ver, size = describe(path) if path else ("PP-OCRv6", "medium")
            params[f"{stage}.engine_type"] = EngineType.ONNXRUNTIME
            params[f"{stage}.ocr_version"] = versions[ver]
            params[f"{stage}.model_type"] = sizes[size]
            if path:
                params[f"{stage}.model_path"] = path
        if download_to:
            params["Global.model_root_dir"] = download_to
        try:
            return RapidOCR(params=params)
        except Exception as ex:
            raise OcrUnavailable(f"PP-OCR 無法啟動：{type(ex).__name__}: {ex}") from ex

    @staticmethod
    def _bgr(rgb: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(rgb[:, :, ::-1])  # RapidOCR follows OpenCV's channel order

    @staticmethod
    def _read_strip(engine, bgr: np.ndarray) -> tuple[str, float]:
        """One cut-out line through the recognition model only. Confidence 0..1."""
        res = engine(bgr, use_det=False, use_cls=False, use_rec=True)
        txts = getattr(res, "txts", None)
        if txts is None or len(txts) == 0:
            return "", 0.0
        scores = getattr(res, "scores", None)
        text = "".join(t or "" for t in txts).strip()
        conf = float(np.mean([float(v) for v in scores])) if scores is not None and len(scores) else 0.0
        return text, conf

    def recognize_page(self, rgb: np.ndarray) -> list[OcrLine]:
        bgr = self._bgr(rgb)
        quick = self._quick()
        first = quick or self._best
        res = first(bgr, use_det=True, use_cls=False, use_rec=True)
        boxes = getattr(res, "boxes", None)
        txts = getattr(res, "txts", None)
        scores = getattr(res, "scores", None)
        if boxes is None or txts is None or len(txts) == 0:
            return []
        H, W = bgr.shape[:2]
        segs = []
        for i, text in enumerate(txts):
            text = (text or "").strip()
            b = np.asarray(boxes[i], dtype=float).reshape(-1, 2)
            x0, y0 = float(b[:, 0].min()), float(b[:, 1].min())
            x1, y1 = float(b[:, 0].max()), float(b[:, 1].max())
            conf = float(scores[i]) if scores is not None and i < len(scores) else 0.0
            if quick is not None and conf < SURE and x1 - x0 >= 8 and y1 - y0 >= 8:
                # second opinion from the medium model; the surer of the two readings stays
                strip = bgr[max(0, int(y0)) : min(H, int(np.ceil(y1)) + 1), max(0, int(x0)) : min(W, int(np.ceil(x1)) + 1)]
                try:
                    t2, c2 = self._read_strip(self._best, np.ascontiguousarray(strip))
                except Exception:
                    t2, c2 = "", 0.0
                if t2 and c2 >= conf:
                    text, conf = t2, c2
            if text:
                segs.append((text, x0, y0, x1 - x0, y1 - y0, conf * 100.0))
        self._traditional = is_traditional_page([t[0] for t in segs])
        if self._traditional:
            segs = [(to_traditional(t[0]),) + t[1:] for t in segs]
        return merge_segments(segs)

    def recognize_crop(self, rgb: np.ndarray) -> tuple[str, float]:
        bgr = self._bgr(rgb)
        quick = self._quick()
        text, conf = self._read_strip(quick or self._best, bgr)
        if quick is not None and conf < SURE:
            t2, c2 = self._read_strip(self._best, bgr)
            if t2 and c2 >= conf:
                text, conf = t2, c2
        if self._traditional:
            text = to_traditional(text)
        return text, (conf * 100.0 if text else -1.0)


# --------------------------------------------------------------------------------------
# command line: fetch the models, or compare the engines on a page
# --------------------------------------------------------------------------------------
def _sample() -> np.ndarray:
    """A small page with text on it: the recognition model is only loaded (and downloaded)
    once the detector has found something to read."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 220), (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    d.text((40, 30), "PdfTailor OCR 2026", fill=(0, 0, 0), font=font)
    d.text((40, 120), "Sample text 12345", fill=(0, 0, 0), font=font)
    return np.asarray(img)


def _fetch(dest: str) -> int:
    """Downloads the PP-OCRv6 medium models straight into ``dest`` (once)."""
    import rapidocr

    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    eng = RapidOcr(download_to=dest)
    lines = eng.recognize_page(_sample())  # medium detection + recognition: both get downloaded
    print("sample read as:", " | ".join(l.text for l in lines) or "(nothing)")
    # Large models an earlier run left inside the package would be bundled a second time.
    pkg = os.path.join(os.path.dirname(rapidocr.__file__), "models")
    if os.path.isdir(pkg) and os.path.abspath(pkg) != dest:
        for f in os.listdir(pkg):
            if f.lower().endswith(".onnx") and ("server" in f.lower() or "medium" in f.lower()):
                try:
                    os.remove(os.path.join(pkg, f))
                except OSError:
                    pass
    names = sorted(f for f in os.listdir(dest) if f.lower().endswith(".onnx"))
    print("models in", dest, ":", ", ".join(names) or "(none)")
    ok = any("det" in n.lower() for n in names) and any("rec" in n.lower() for n in names)
    if not ok:
        print("ERROR: detection and recognition models are both needed; see the messages above.")
    return 0 if ok else 1


def _compare(pdf: str, page: int) -> int:
    from ..pdfio import PdfDoc
    from .tesseract import TesseractOcr

    rgb = PdfDoc(pdf).render(page, 300 / 72.0)
    for make in (RapidOcr, TesseractOcr):
        try:
            eng = make()
            eng.dpi = 300.0
            lines = eng.recognize_page(rgb)
        except Exception as ex:
            print(f"== {make.__name__}: unavailable ({ex})")
            continue
        print(f"== {eng.name}: {len(lines)} lines")
        for l in lines:
            print(f"  [{l.confidence:5.1f}] {l.text}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
    if len(sys.argv) >= 3 and sys.argv[1] == "--fetch":
        raise SystemExit(_fetch(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1].lower().endswith(".pdf"):
        raise SystemExit(_compare(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1))
    print("usage: python -m pdftailor_engine.ocr.rapid --fetch <folder> | <file.pdf> [page]")
