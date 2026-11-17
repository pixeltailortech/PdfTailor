# PdfTailor 第三方軟體授權聲明

Copyright © 2026 LouisWu（吳秉儒）. All rights reserved.

PdfTailor 本身的著作權屬上列作者。程式內含或隨附下列第三方開源軟體，各自依其授權條款使用與散布，著作權屬各原作者；本程式未修改這些元件的原始碼（PP-OCR 模型的格式轉換由 RapidOCR 完成，見第 3 節）。
打包後的 `publish\licenses\` 資料夾會附上各元件授權原文（由 `release.cmd publish` 產生）。

本檔由 `tools/notices.py` 依實際打包內容產生，請勿手動修改。

## 1. 轉換引擎（pdftailor-engine.exe）內含的 Python 套件

| 套件 | 版本 | 授權 | 授權原文 |
|---|---|---|---|
| certifi | 2026.10.23 | MPL-2.0 | LICENSE |
| charset-normalizer | 3.5.2 | MIT | LICENSE |
| colorama | 0.4.6 | BSD-3-Clause | LICENSE.txt |
| colorlog | 6.12.0 | MIT | LICENSE |
| flatbuffers | 25.12.19 | Apache-2.0 | Apache-2.0 原文 |
| fonttools | 4.68.0 | MIT | LICENSE、LICENSE.external |
| idna | 3.20 | BSD-3-Clause | LICENSE.md |
| lxml | 6.1.3 | BSD-3-Clause | LICENSE.txt、LICENSES.txt |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | LICENSE.txt、LICENSE.md、LICENSE.txt、LICENSE、LICENSE.md、LICENSE.md、LICENSE.md、LICENSE.md、LICENSE.md、LICENSE.md、LICENSE.md、LICENSE.txt、COPYING、LICENSE、dragon4_LICENSE.txt、LICENSE.md、LICENSE |
| omegaconf | 2.4.0 | BSD-3-Clause | LICENSE、LICENSE-antlr4 |
| onnxruntime | 1.31.1 | MIT | LICENSE、ThirdPartyNotices.txt |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 | LICENSE-3RD-PARTY.txt、LICENSE.txt、LICENSE-3RD-PARTY.txt、LICENSE.txt |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | LICENSE、LICENSE.APACHE、LICENSE.BSD |
| pikepdf | 10.17.0 | MPL-2.0 | LICENSE.txt、CC-BY-4.0-veraPDF.txt、GCC-exception-3.1.txt、GPL-3.0.txt、libjpeg-turbo-README.ijg.txt、libjpeg-turbo.txt、microsoft-visual-cpp-runtime.txt、openssl.txt、pdfminer.six.txt、qpdf.txt、README.md、sRGB-icc-profile.txt、zlib.txt |
| pillow | 13.0.0 | MIT-CMU | LICENSE |
| pyclipper | 1.4.0 | MIT | LICENSE |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0, dependency licenses | abseil.txt、agg23.txt、fast_float.txt、freetype.txt、icu.txt、lcms.txt、libjpeg_turbo.ijg、libjpeg_turbo.md、libopenjpeg.txt、libpng.txt、llvm-libc.txt、pdfium-binaries.txt、pdfium.txt、simdutf.txt、zlib.txt、Apache-2.0.txt、BSD-3-Clause.txt、CC-BY-4.0.txt |
| python-docx | 1.2.0 | MIT | LICENSE |
| PyYAML | 6.0.3 | MIT | LICENSE |
| rapidocr | 3.10.0 | Apache-2.0 | LICENSE、MODEL_LICENSES.md |
| reportlab | 5.1.0 | BSD-3-Clause | LICENSE、license.txt |
| requests | 2.34.2 | Apache-2.0 | LICENSE、NOTICE |
| shapely | 2.2.0 | BSD-3-Clause | LICENSE.txt、LICENSE_GEOS、LICENSE_win32 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | LICENCE |
| typing_extensions | 4.16.0 | PSF-2.0 | LICENSE |
| urllib3 | 2.8.0 | MIT | LICENSE.txt |

## 2. 執行環境

| 元件 | 版本 | 授權 | 授權原文 |
|---|---|---|---|
| Python 執行環境 | 3.11.9 | PSF-2.0 | LICENSE.txt |
| PyInstaller 啟動程式（bootloader） | 6.22.3 | GPL-2.0-or-later，附 bootloader 例外條款（允許用於任何程式，不影響本程式授權） | COPYING.txt |

## 3. 文字辨識模型（PP-OCR）

- 模型：`PP-OCRv6_det_medium.onnx`、`PP-OCRv6_rec_medium.onnx`
- 來源：PaddleOCR（https://github.com/PaddlePaddle/PaddleOCR），經 RapidOCR（https://github.com/RapidAI/RapidOCR）轉為 ONNX 格式。
- 著作權：模型權重 © Baidu 及 PaddleOCR 權利人；轉換腳本 © RapidOCR Authors。
- 授權：Apache-2.0（授權原文；RapidOCR 的模型授權說明見上表 rapidocr 的 MODEL_LICENSES.md）。
- 修改聲明：Converted from an official PaddleOCR model to the ONNX inference format and packaged for use with RapidOCR. The upstream model weights are copyright Baidu and/or the applicable PaddleOCR rights holders and are licensed under the Apache License, Version 2.0.
- PaddlePaddle、PaddleOCR、RapidOCR 等名稱僅用於說明來源，不代表其背書。

## 4. Tesseract OCR（備用辨識引擎，位於 engine\tesseract）

- tesseract.exe 與 libtesseract：Apache-2.0（https://github.com/tesseract-ocr/tesseract）
- 語言資料 `chi_tra.traineddata`、`eng.traineddata`、`osd.traineddata`：Apache-2.0（https://github.com/tesseract-ocr/tessdata_fast）
- 以下 DLL 來自 Tesseract 的 Windows 建置（UB Mannheim／MSYS2），未隨附各自的授權檔；授權依各上游專案整理，通用授權原文放在 `licenses/common/`。其中 LGPL 元件以獨立 DLL 動態連結，可自行替換；JBIG-KIT 為 GPL-2.0-or-later。若要散布到公司以外，請先核對並依授權提供原始碼取得方式。

| DLL | 專案 | 授權 |
|---|---|---|
| libLerc.dll | LERC | Apache-2.0 |
| libarchive-13.dll | libarchive | BSD-2-Clause |
| libb2-1.dll | BLAKE2 | CC0-1.0 |
| libbrotlicommon.dll | Brotli | MIT |
| libbrotlidec.dll | Brotli | MIT |
| libbz2-1.dll | bzip2 | bzip2-1.0.6 |
| libcairo-2.dll | cairo | LGPL-2.1-only OR MPL-1.1 |
| libcurl-4.dll | curl | curl |
| libdatrie-1.dll | libdatrie | LGPL-2.1-or-later |
| libdeflate.dll | libdeflate | MIT |
| libexpat-1.dll | Expat | MIT |
| libffi-8.dll | libffi | MIT |
| libfontconfig-1.dll | Fontconfig | HPND (MIT-style) |
| libfreetype-6.dll | FreeType | FTL OR GPL-2.0-or-later |
| libfribidi-0.dll | GNU FriBidi | LGPL-2.1-or-later |
| libgcc_s_seh-1.dll | GCC runtime | GPL-3.0-or-later WITH GCC-exception-3.1 |
| libgif-7.dll | giflib | MIT |
| libgio-2.0-0.dll | GLib | LGPL-2.1-or-later |
| libglib-2.0-0.dll | GLib | LGPL-2.1-or-later |
| libgmodule-2.0-0.dll | GLib | LGPL-2.1-or-later |
| libgobject-2.0-0.dll | GLib | LGPL-2.1-or-later |
| libgraphite2.dll | Graphite2 | LGPL-2.1-or-later OR MPL-2.0 OR GPL-2.0-or-later |
| libharfbuzz-0.dll | HarfBuzz | MIT |
| libiconv-2.dll | GNU libiconv | LGPL-2.1-or-later |
| libicudt78.dll | ICU | Unicode-3.0 |
| libicuin78.dll | ICU | Unicode-3.0 |
| libicuuc78.dll | ICU | Unicode-3.0 |
| libidn2-0.dll | GNU Libidn2 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libintl-8.dll | GNU gettext runtime | LGPL-2.1-or-later |
| libjbig-0.dll | JBIG-KIT | GPL-2.0-or-later |
| libjpeg-8.dll | libjpeg-turbo | IJG AND BSD-3-Clause AND Zlib |
| libleptonica-6.dll | Leptonica | BSD-2-Clause (Leptonica) |
| liblz4.dll | LZ4 | BSD-2-Clause |
| liblzma-5.dll | XZ Utils (liblzma) | 0BSD |
| libopenjp2-7.dll | OpenJPEG | BSD-2-Clause |
| libpango-1.0-0.dll | Pango | LGPL-2.1-or-later |
| libpangocairo-1.0-0.dll | Pango | LGPL-2.1-or-later |
| libpangoft2-1.0-0.dll | Pango | LGPL-2.1-or-later |
| libpangowin32-1.0-0.dll | Pango | LGPL-2.1-or-later |
| libpcre2-8-0.dll | PCRE2 | BSD-3-Clause WITH PCRE2-exception |
| libpixman-1-0.dll | pixman | MIT |
| libpng16-16.dll | libpng | libpng-2.0 |
| libpsl-5.dll | libpsl | MIT |
| libsharpyuv-0.dll | libwebp | BSD-3-Clause |
| libssh2-1.dll | libssh2 | BSD-3-Clause |
| libstdc++-6.dll | GCC C++ runtime | GPL-3.0-or-later WITH GCC-exception-3.1 |
| libtesseract-5.dll | Tesseract OCR | Apache-2.0 |
| libthai-0.dll | libthai | LGPL-2.1-or-later |
| libtiff-6.dll | LibTIFF | libtiff |
| libunistring-5.dll | GNU libunistring | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libwebp-7.dll | libwebp | BSD-3-Clause |
| libwebpmux-3.dll | libwebp | BSD-3-Clause |
| libwinpthread-1.dll | mingw-w64 winpthreads | MIT AND BSD-3-Clause |
| libzstd.dll | Zstandard | BSD-3-Clause OR GPL-2.0-only |
| zlib1.dll | zlib | Zlib |

## 5. 主程式（PdfTailor.exe）

| 元件 | 版本 | 授權 | 授權原文 |
|---|---|---|---|
| .NET 執行環境與 WPF（© .NET Foundation and Contributors） | 9 | MIT | LICENSE.txt |
| PdfSharpCore（© PdfSharpCore contributors） | 1.3.67 | MIT | MIT 原文 |
| SharpZipLib（© ICSharpCode SharpZipLib contributors） | 1.4.2 | MIT | MIT 原文 |
| SixLabors.Fonts（© Six Labors） | 1.0.0-beta17 | Apache-2.0 | Apache-2.0 原文 |
| SixLabors.ImageSharp（© Six Labors） | 1.0.4 | Apache-2.0 | Apache-2.0 原文 |
