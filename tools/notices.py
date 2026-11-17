"""Writes THIRD_PARTY_NOTICES.md and a licenses/ folder next to a PdfTailor build.

    python tools/notices.py publish            # after "release.cmd publish"
    python tools/notices.py . --md-only        # refresh the copy kept in the repo

Run it with the engine's build Python (engine/.venv-build) so it sees exactly the
packages PyInstaller bundled. What goes in:
  * Python packages that PyInstaller actually put in the engine (read from
    engine/build/pdftailor-engine/PYZ-00.toc and dist/_internal), with every licence
    file their wheels ship;
  * the Python runtime and the PyInstaller bootloader;
  * the PP-OCR model files and the Tesseract folder, when they are in the build;
  * the .NET runtime and the NuGet packages of the WPF shell.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import sysconfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTS = os.path.join(ROOT, "tools", "license-texts")
ENGINE = os.path.join(ROOT, "engine")

PRODUCT = "PdfTailor"
COPYRIGHT = "Copyright © 2026 LouisWu（吳秉儒）. All rights reserved."

# Cleaner labels where a wheel's metadata is a sentence rather than an SPDX id.
LABELS = {
    "reportlab": "BSD-3-Clause",
    "pyinstaller": "GPL-2.0-or-later WITH Bootloader-exception",
    "omegaconf": "BSD-3-Clause",
    "colorama": "BSD-3-Clause",
    "pyyaml": "MIT",
    "charset-normalizer": "MIT",
    "fonttools": "MIT",
    "protobuf": "BSD-3-Clause",
    "onnxruntime": "MIT",
    "opencv-python-headless": "Apache-2.0",
    "flatbuffers": "Apache-2.0",
    "colorlog": "MIT",
    "python-docx": "MIT",
}

# Build-time packages: they never ship even when a name matches.
BUILD_ONLY = {"pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib", "pytest",
              "pluggy", "iniconfig", "pygments", "altgraph", "pefile", "pywin32-ctypes"}

# The Tesseract Windows build (UB Mannheim / MSYS2) ships these DLLs without licence files.
# Licences are the upstream projects' own; check them before distributing outside the company.
TESS_DLLS = [
    (r"libtesseract", "Tesseract OCR", "Apache-2.0"),
    (r"libleptonica", "Leptonica", "BSD-2-Clause (Leptonica)"),
    (r"libLerc", "LERC", "Apache-2.0"),
    (r"libarchive", "libarchive", "BSD-2-Clause"),
    (r"libb2", "BLAKE2", "CC0-1.0"),
    (r"libbrotli", "Brotli", "MIT"),
    (r"libbz2", "bzip2", "bzip2-1.0.6"),
    (r"libcairo", "cairo", "LGPL-2.1-only OR MPL-1.1"),
    (r"libcurl", "curl", "curl"),
    (r"libdatrie", "libdatrie", "LGPL-2.1-or-later"),
    (r"libdeflate", "libdeflate", "MIT"),
    (r"libexpat", "Expat", "MIT"),
    (r"libffi", "libffi", "MIT"),
    (r"libfontconfig", "Fontconfig", "HPND (MIT-style)"),
    (r"libfreetype", "FreeType", "FTL OR GPL-2.0-or-later"),
    (r"libfribidi", "GNU FriBidi", "LGPL-2.1-or-later"),
    (r"libgcc_s", "GCC runtime", "GPL-3.0-or-later WITH GCC-exception-3.1"),
    (r"libstdc\+\+", "GCC C++ runtime", "GPL-3.0-or-later WITH GCC-exception-3.1"),
    (r"libgif", "giflib", "MIT"),
    (r"lib(glib|gio|gmodule|gobject)", "GLib", "LGPL-2.1-or-later"),
    (r"libgraphite2", "Graphite2", "LGPL-2.1-or-later OR MPL-2.0 OR GPL-2.0-or-later"),
    (r"libharfbuzz", "HarfBuzz", "MIT"),
    (r"libiconv", "GNU libiconv", "LGPL-2.1-or-later"),
    (r"libicu", "ICU", "Unicode-3.0"),
    (r"libidn2", "GNU Libidn2", "LGPL-3.0-or-later OR GPL-2.0-or-later"),
    (r"libintl", "GNU gettext runtime", "LGPL-2.1-or-later"),
    (r"libjbig", "JBIG-KIT", "GPL-2.0-or-later"),
    (r"libjpeg", "libjpeg-turbo", "IJG AND BSD-3-Clause AND Zlib"),
    (r"liblz4", "LZ4", "BSD-2-Clause"),
    (r"liblzma", "XZ Utils (liblzma)", "0BSD"),
    (r"libopenjp2", "OpenJPEG", "BSD-2-Clause"),
    (r"libpango", "Pango", "LGPL-2.1-or-later"),
    (r"libpcre2", "PCRE2", "BSD-3-Clause WITH PCRE2-exception"),
    (r"libpixman", "pixman", "MIT"),
    (r"libpng", "libpng", "libpng-2.0"),
    (r"libpsl", "libpsl", "MIT"),
    (r"lib(sharpyuv|webp|webpmux)", "libwebp", "BSD-3-Clause"),
    (r"libssh2", "libssh2", "BSD-3-Clause"),
    (r"libthai", "libthai", "LGPL-2.1-or-later"),
    (r"libtiff", "LibTIFF", "libtiff"),
    (r"libunistring", "GNU libunistring", "LGPL-3.0-or-later OR GPL-2.0-or-later"),
    (r"libwinpthread", "mingw-w64 winpthreads", "MIT AND BSD-3-Clause"),
    (r"libzstd", "Zstandard", "BSD-3-Clause OR GPL-2.0-only"),
    (r"zlib", "zlib", "Zlib"),
]

# NuGet packages of the WPF shell when their own licence file is not in the local cache.
NUGET = {
    "pdfsharpcore": ("MIT", "PdfSharpCore contributors"),
    "sharpziplib": ("MIT", "ICSharpCode SharpZipLib contributors"),
    "sixlabors.fonts": ("Apache-2.0", "Six Labors"),
    "sixlabors.imagesharp": ("Apache-2.0", "Six Labors"),
}
NUGET_BUILD_ONLY = {"microsoft.net.illink.tasks"}

LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|thirdpartynotices|authors)", re.I)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def headers(path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                break
            if ":" in line and not line[0].isspace():
                k, v = line.split(":", 1)
                out.setdefault(k.strip(), []).append(v.strip())
    return out


class Dist:
    def __init__(self, site: str, info: str):
        self.site, self.info = site, info
        h = headers(os.path.join(info, "METADATA"))
        self.name = (h.get("Name") or [os.path.basename(info).split("-")[0]])[0]
        self.version = (h.get("Version") or [""])[0]
        key = norm(self.name)
        lic = (h.get("License-Expression") or [""])[0]
        if not lic:
            raw = (h.get("License") or [""])[0]
            lic = raw if raw and len(raw) < 50 else ""
        if not lic:
            cls = [c.split("::")[-1].strip() for c in h.get("Classifier", []) if c.startswith("License ::")]
            lic = " / ".join(c for c in cls if c != "OSI Approved")
        self.license = LABELS.get(key) or lic or "見授權檔"
        self.tops = self._tops()

    def _tops(self) -> set[str]:
        tops: set[str] = set()
        tl = os.path.join(self.info, "top_level.txt")
        if os.path.exists(tl):
            tops |= {l.strip() for l in open(tl, encoding="utf-8", errors="replace") if l.strip()}
        rec = os.path.join(self.info, "RECORD")
        if os.path.exists(rec):
            for line in open(rec, encoding="utf-8", errors="replace"):
                first = line.split(",", 1)[0].replace("\\", "/").split("/")[0]
                if not first or first.startswith("..") or first.endswith((".dist-info", ".data")) or first == "__pycache__":
                    continue
                tops.add(re.sub(r"\.(py|pyd|so)$", "", first.split(".cp")[0]) if "." in first else first)
        return {t.removesuffix(".libs") for t in tops}

    def license_files(self) -> list[tuple[str, str]]:
        """(source, path relative to the dist's own folder)"""
        out = []
        lic_dir = os.path.join(self.info, "licenses")
        for d, _dirs, names in os.walk(self.info):
            for n in names:
                p = os.path.join(d, n)
                if p.startswith(lic_dir + os.sep) or LICENSE_FILE.match(n):
                    out.append((p, os.path.relpath(p, self.info)))
        for t in sorted(self.tops):
            pkg = os.path.join(self.site, t)
            if os.path.isdir(pkg):
                for n in sorted(os.listdir(pkg)):
                    p = os.path.join(pkg, n)
                    if os.path.isfile(p) and LICENSE_FILE.match(n):
                        out.append((p, os.path.join(t, n)))
        seen, uniq = set(), []
        for src, rel in out:
            if rel.lower() not in seen:
                seen.add(rel.lower())
                uniq.append((src, rel))
        return uniq


def bundled_names() -> set[str] | None:
    toc = os.path.join(ENGINE, "build", "pdftailor-engine", "PYZ-00.toc")
    internal = os.path.join(ENGINE, "dist", "pdftailor-engine", "_internal")
    if not os.path.exists(toc) and not os.path.isdir(internal):
        return None
    names: set[str] = set()
    if os.path.exists(toc):
        text = open(toc, encoding="utf-8", errors="replace").read()
        names |= {m.split(".")[0] for m in re.findall(r"\('([A-Za-z_][\w.]*)',", text)}
    if os.path.isdir(internal):
        for n in os.listdir(internal):
            names.add(n.split(".")[0] if not n.endswith(".dist-info") else n)
            names.add(n.removesuffix(".libs"))
    return names


def safe(name: str) -> str:
    return re.sub(r"[^\w.+-]+", "_", name)


class Writer:
    def __init__(self, out: str, md_only: bool):
        self.out, self.md_only = out, md_only
        self.lic = os.path.join(out, "licenses")
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def copy(self, src: str, rel: str) -> str | None:
        if self.md_only or not os.path.isfile(src):
            return None
        dst = os.path.join(self.lic, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        return "licenses/" + rel.replace(os.sep, "/")

    def link(self, label: str, path: str | None) -> str:
        return f"[{label}]({path})" if path else label

    def common(self, name: str) -> str | None:
        return self.copy(os.path.join(TEXTS, name), os.path.join("common", name))


def python_section(w: Writer, site: str) -> None:
    dists = [Dist(site, d) for d in sorted(glob.glob(os.path.join(site, "*.dist-info")))]
    names = bundled_names()
    if names is None:
        w.warnings.append("找不到 engine/build 與 engine/dist：無法確認打包內容，列出建置環境內所有非建置用套件。")
    ship = [d for d in dists if norm(d.name) not in BUILD_ONLY and (names is None or d.tops & names)]
    w.lines += ["## 1. 轉換引擎（pdftailor-engine.exe）內含的 Python 套件", "",
                "| 套件 | 版本 | 授權 | 授權原文 |", "|---|---|---|---|"]
    for d in sorted(ship, key=lambda d: d.name.lower()):
        links = []
        for src, rel in d.license_files():
            p = w.copy(src, os.path.join("python", safe(d.name), rel))
            links.append(w.link(os.path.basename(rel), p))
        if not links and d.license == "Apache-2.0":
            links.append(w.link("Apache-2.0 原文", w.common("Apache-2.0.txt")))
        elif not links:
            w.warnings.append(f"{d.name} {d.version} 沒有附授權檔，僅依中繼資料標示 {d.license}。")
        w.lines.append(f"| {d.name} | {d.version} | {d.license} | {'、'.join(links) or '—'} |")
    w.lines.append("")

    # Python runtime and PyInstaller bootloader
    cfg = os.path.join(os.path.dirname(os.path.dirname(site)), "pyvenv.cfg")
    home, ver = sys.base_prefix, sys.version.split()[0]
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8", errors="replace"):
            k, _, v = line.partition("=")
            if k.strip() == "home":
                home = v.strip()
            elif k.strip() == "version":
                ver = v.strip()
    py_lic = w.copy(os.path.join(home, "LICENSE.txt"), os.path.join("python", "Python", "LICENSE.txt"))
    if not py_lic and not w.md_only:
        w.warnings.append(f"找不到 Python 授權檔 {os.path.join(home, 'LICENSE.txt')}。")
    pyi = next((d for d in dists if norm(d.name) == "pyinstaller"), None)
    pyi_links = []
    if pyi:
        for src, rel in pyi.license_files():
            pyi_links.append(w.link(os.path.basename(rel), w.copy(src, os.path.join("python", "PyInstaller", rel))))
    w.lines += ["## 2. 執行環境", "",
                "| 元件 | 版本 | 授權 | 授權原文 |", "|---|---|---|---|",
                f"| Python 執行環境 | {ver} | PSF-2.0 | {w.link('LICENSE.txt', py_lic)} |",
                f"| PyInstaller 啟動程式（bootloader） | {pyi.version if pyi else ''} | GPL-2.0-or-later，附 bootloader 例外條款（允許用於任何程式，不影響本程式授權） | {'、'.join(pyi_links) or '—'} |",
                ""]


def models_section(w: Writer, out: str) -> None:
    found = []
    for d in (os.path.join(out, "engine", "models"), os.path.join(out, "engine", "_internal", "rapidocr", "models"),
              os.path.join(ROOT, "vendor", "ocr-models")):
        found += sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.onnx")))
    found = sorted(set(found))
    apache = w.common("Apache-2.0.txt")
    w.lines += ["## 3. 文字辨識模型（PP-OCR）", "",
                "- 模型：" + ("、".join(f"`{n}`" for n in found) if found else "PP-OCRv6（隨 RapidOCR 套件）"),
                "- 來源：PaddleOCR（https://github.com/PaddlePaddle/PaddleOCR），經 RapidOCR（https://github.com/RapidAI/RapidOCR）轉為 ONNX 格式。",
                "- 著作權：模型權重 © Baidu 及 PaddleOCR 權利人；轉換腳本 © RapidOCR Authors。",
                f"- 授權：Apache-2.0（{w.link('授權原文', apache)}；RapidOCR 的模型授權說明見上表 rapidocr 的 MODEL_LICENSES.md）。",
                "- 修改聲明：Converted from an official PaddleOCR model to the ONNX inference format and packaged for use with RapidOCR. "
                "The upstream model weights are copyright Baidu and/or the applicable PaddleOCR rights holders and are licensed under the Apache License, Version 2.0.",
                "- PaddlePaddle、PaddleOCR、RapidOCR 等名稱僅用於說明來源，不代表其背書。", ""]


def tesseract_section(w: Writer, out: str) -> None:
    tess = os.path.join(out, "engine", "tesseract")
    if not os.path.isdir(tess):
        tess = os.path.join(ROOT, "vendor", "tesseract")
    if not os.path.isdir(tess):
        return
    dlls = sorted(n for n in os.listdir(tess) if n.lower().endswith(".dll"))
    data = sorted(n for n in os.listdir(os.path.join(tess, "tessdata")) if n.endswith(".traineddata")) \
        if os.path.isdir(os.path.join(tess, "tessdata")) else []
    for n in ("Apache-2.0.txt", "LGPL-2.1.txt", "LGPL-3.0.txt", "GPL-2.0.txt", "GPL-3.0.txt",
              "GCC-exception-3.1.txt", "MPL-1.1.txt", "MPL-2.0.txt"):
        w.common(n)
    w.lines += ["## 4. Tesseract OCR（備用辨識引擎，位於 engine\\tesseract）", "",
                "- tesseract.exe 與 libtesseract：Apache-2.0（https://github.com/tesseract-ocr/tesseract）",
                "- 語言資料 " + "、".join(f"`{n}`" for n in data) + "：Apache-2.0（https://github.com/tesseract-ocr/tessdata_fast）",
                "- 以下 DLL 來自 Tesseract 的 Windows 建置（UB Mannheim／MSYS2），未隨附各自的授權檔；授權依各上游專案整理，"
                "通用授權原文放在 `licenses/common/`。其中 LGPL 元件以獨立 DLL 動態連結，可自行替換；"
                "JBIG-KIT 為 GPL-2.0-or-later。若要散布到公司以外，請先核對並依授權提供原始碼取得方式。", "",
                "| DLL | 專案 | 授權 |", "|---|---|---|"]
    for n in dlls:
        hit = next(((p, l) for pat, p, l in TESS_DLLS if re.match(pat, n, re.I)), None)
        if not hit:
            w.warnings.append(f"Tesseract 資料夾內 {n} 的授權未知，請核對。")
            hit = ("（未知）", "請核對")
        w.lines.append(f"| {n} | {hit[0]} | {hit[1]} |")
    w.lines.append("")


def dotnet_section(w: Writer) -> None:
    w.lines += ["## 5. 主程式（PdfTailor.exe）", "", "| 元件 | 版本 | 授權 | 授權原文 |", "|---|---|---|---|"]
    dotnet = shutil.which("dotnet")
    droot = os.path.dirname(os.path.realpath(dotnet)) if dotnet else ""
    lic = w.copy(os.path.join(droot, "LICENSE.txt"), os.path.join("dotnet", "LICENSE.txt")) if droot else None
    tpn = w.copy(os.path.join(droot, "ThirdPartyNotices.txt"), os.path.join("dotnet", "ThirdPartyNotices.txt")) if droot else None
    if not lic:
        lic = w.copy(os.path.join(TEXTS, "MIT-dotnet.txt"), os.path.join("dotnet", "LICENSE.txt"))
    links = [w.link("LICENSE.txt", lic)] + ([w.link("ThirdPartyNotices.txt", tpn)] if tpn else [])
    w.lines.append(f"| .NET 執行環境與 WPF（© .NET Foundation and Contributors） | 9 | MIT | {'、'.join(links)} |")

    assets = os.path.join(ROOT, "app", "PdfTailor", "obj", "project.assets.json")
    pkgs: list[tuple[str, str]] = []
    folders: list[str] = []
    if os.path.exists(assets):
        data = json.load(open(assets, encoding="utf-8"))
        folders = list((data.get("packageFolders") or {}).keys())
        for k, v in data.get("libraries", {}).items():
            if v.get("type") == "package":
                name, ver = k.split("/", 1)
                if name.lower() not in NUGET_BUILD_ONLY:
                    pkgs.append((name, ver))
    else:
        w.warnings.append("找不到 app/PdfTailor/obj/project.assets.json，NuGet 套件依預設清單列出。")
        pkgs = [("PdfSharpCore", "1.3.67"), ("SharpZipLib", "1.4.2"), ("SixLabors.Fonts", "1.0.0-beta17"),
                ("SixLabors.ImageSharp", "1.0.4")]
    for name, ver in sorted(pkgs, key=lambda p: p[0].lower()):
        spdx, owner = NUGET.get(name.lower(), ("", ""))
        links = []
        for f in folders:
            d = os.path.join(f, name.lower(), ver.lower())
            if not os.path.isdir(d):
                continue
            for n in sorted(os.listdir(d)):
                if LICENSE_FILE.match(n) and os.path.isfile(os.path.join(d, n)):
                    links.append(w.link(n, w.copy(os.path.join(d, n), os.path.join("nuget", safe(name), n))))
            nuspec = next(iter(glob.glob(os.path.join(d, "*.nuspec"))), None)
            if nuspec:  # the package's own metadata wins over the table above
                m = re.search(r'<license type="expression">([^<]+)</license>', open(nuspec, encoding="utf-8", errors="replace").read())
                spdx = m.group(1).strip() if m else spdx
        if not spdx:
            w.warnings.append(f"NuGet 套件 {name} {ver} 的授權未知，請核對。")
        if not links and spdx == "Apache-2.0":
            links.append(w.link("Apache-2.0 原文", w.common("Apache-2.0.txt")))
        elif not links and spdx == "MIT":
            links.append(w.link("MIT 原文", w.common("MIT.txt")))
        w.lines.append(f"| {name}{'（© ' + owner + '）' if owner else ''} | {ver} | {spdx or '請核對'} | {'、'.join(links) or '—'} |")
    w.lines.append("")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", help="publish folder (or the repo root with --md-only)")
    ap.add_argument("--site", help="site-packages of the engine build venv (default: this Python's)")
    ap.add_argument("--md-only", action="store_true", help="write only THIRD_PARTY_NOTICES.md, no licence copies")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    site = a.site or sysconfig.get_paths()["purelib"]
    w = Writer(out, a.md_only)
    if not a.md_only and os.path.isdir(w.lic):
        shutil.rmtree(w.lic)

    python_section(w, site)
    models_section(w, out)
    tesseract_section(w, out)
    dotnet_section(w)

    head = [f"# {PRODUCT} 第三方軟體授權聲明", "", COPYRIGHT, "",
            f"{PRODUCT} 本身的著作權屬上列作者。程式內含或隨附下列第三方開源軟體，"
            "各自依其授權條款使用與散布，著作權屬各原作者；本程式未修改這些元件的原始碼"
            "（PP-OCR 模型的格式轉換由 RapidOCR 完成，見第 3 節）。",
            "各元件的授權原文放在本檔旁的 `licenses/` 資料夾。" if not a.md_only else
            "打包後的 `publish\\licenses\\` 資料夾會附上各元件授權原文（由 `release.cmd publish` 產生）。",
            "", "本檔由 `tools/notices.py` 依實際打包內容產生，請勿手動修改。", ""]
    tail = []
    if w.warnings:
        tail = ["## 待核對事項", ""] + [f"- {x}" for x in w.warnings] + [""]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "THIRD_PARTY_NOTICES.md"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(head + w.lines + tail))
    print(f"THIRD_PARTY_NOTICES.md written to {out}" + ("" if a.md_only else " (licenses\\ copied)"))
    for x in w.warnings:
        print("NOTE:", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
