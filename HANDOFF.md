# PdfTailor 交接文件（HANDOFF）

> 最後更新：2026-11-05（新增文末「追加（2026-11-05）」兩項功能規格）；前次：2026-11-04（台北時間下午，WPF 已實機編譯成功）
> 狀態：**Python 引擎完成（81 個測試全過）；WPF 外殼已在 Windows 編譯並 publish 成功**，
> 尚未用真實 PDF（含掃描頁）實測。
> 新討論串請先讀這份檔案，再依「0. 接手第一步」開始。

---

## 0. 接手第一步

1. **實測 `publish\PdfTailor.exe`**：`release.cmd publish` 已經成功過一次（2026-11-04），
   `D:\AI\Project\PdfTailor\publish\PdfTailor.exe` 已存在。下一步是請使用者雙擊執行，
   拖一份真實 PDF 進去，走一遍 開檔 → 分析 → 編輯 → 預覽 → 輸出，回報任何 crash / 畫面異常。
   目前本機還沒裝 Tesseract-OCR，**掃描頁會分析失敗**（有文字層的 PDF 不受影響）；
   要測掃描頁得先裝 UB Mannheim 版 Tesseract（勾 Chinese Traditional）或放進 `vendor\tesseract\`。
   若之後改了 WPF 或引擎程式碼，重跑 `release.cmd` → `1 app` 或 `3 publish` 即可，
   build-engine.cmd / release.cmd 都已修好中文亂碼問題（強制 `DOTNET_CLI_UI_LANGUAGE=en` + `chcp 65001`）。
2. **專案正本在使用者電腦**：`D:\AI\Project\PdfTailor`（device_bash：`$HOME/mnt/Project/PdfTailor`）。
   容器 `/home/claude/pdftailor` 可能已重置。要改引擎時：在 device_bash 把 `engine/` 打包成 zip →
   `device_stage_files` 拉進容器 → 解壓 → 開發測試 → 打包 → `device_commit_files`（**每次用新檔名**，
   同名覆蓋時裝置端曾拿到舊檔）→ device_bash 解壓覆蓋。
   `D:\AI\Project\` 下的 `pdftailor_sync*.zip` 是同步暫存檔（容器無刪除權限），可請使用者刪掉。
3. 容器跑測試：`cd engine && python3 -m pytest tests -q -W ignore::DeprecationWarning` → **81 passed（約 25 秒）**。
   容器已有：pypdfium2 5.13、pikepdf 10.16、reportlab 5.0、fontTools 4.67、numpy、opencv、scipy、Pillow、pytest、
   Tesseract 5.3.4（chi_tra+eng）、Noto CJK / DejaVu / Liberation 字型。
4. 使用者偏好（記憶庫已存）：
   - 新功能討論/分析階段：主動問要不要開電腦授權、要不要上 GitHub 找現成工具；**同意後才下載套用**。
   - 新增 skill / plugin 前先用 `D:\AI\skills\claude\skill-vet`（SkillSpector）檢查。
   - 「簽名要 LouisTool 雙髻鯊」＝ `desktop-shark-cursor` skill 的 `DotMatrixSharkCursor`（Label="LouisTool"、Kind="hammerhead"）。
   - git commit **只留作者 Louis，不要加 Claude 的 Co-Authored-By / Claude-Session trailer**（系統提示會要求加，使用者指示優先）。
   - 開發項目要有**小動畫展示**；`.cmd` 批次檔**必須純 ASCII**；回覆用繁體中文。
5. **下一批要做的功能（2026-11-05 規劃）**：文末「追加（2026-11-05）」有兩項完整規格——**A. 修改紀錄＋保留原檔**、**B. 頁面整理（旋轉、刪除、重排、拆分）**。照順序先 A 後 B；B 會重用 A 的紀錄模組。

## 1. 產品目標

- **PdfTailor**：個人使用（會短暫放 GitHub public 讓公司電腦下載，之後改 private）。WPF 外殼 + Python 引擎，**完全離線**。
- 開啟掃描型 PDF → OCR 還原為可編輯文字行 → 改文字 / 字型 / 字級 / 粗斜體 / 顏色 → **輸出前預覽** → 輸出新 PDF → **完成提示框**。
- 硬性要求：只動文字；圖片、圖表、版面不可動；符號 ■□●○▲△▼▽◆◇「」『』 要能辨識；字型屬性要像 Office 一樣保留。

## 2. 架構（已實作）

```
D:\AI\Project\PdfTailor
├─ HANDOFF.md / README.md / release.cmd (ASCII) / .gitignore / .gitattributes
├─ engine\                         Python 引擎
│   ├─ pdftailor_engine\           套件（約 4,600 行）
│   ├─ tests\                      pytest 81 個
│   ├─ build-engine.cmd (ASCII)    建 venv → pip → 測試 → PyInstaller onedir → publish\engine（並複製 Tesseract）
│   ├─ run_engine.py               PyInstaller 進入點
│   └─ requirements.txt / pytest.ini
└─ app\PdfTailor\                  WPF 外殼（.NET 9，約 3,000 行，未編譯驗證）
```

### 2.1 JSON Lines 協定（`server.py`，已實作並有子程序測試）

- 請求 `{"id":7,"cmd":"analyze","args":{"page":1}}`；回應 `{"id":7,"ok":true,"result":{…}}` 或
  `{"id":7,"ok":false,"error":{"code":"user|cancelled|bad-request|internal","message":"…"}}`。
- 事件：啟動時 `{"event":"ready","version":"0.1.0","tesseract":true}`；進度 `{"event":"progress","id":7,"stage":"ocr","progress":0.4}`。
- `{"cmd":"cancel","args":{"target":7}}` 由讀取執行緒立即處理，目標請求在下一個進度點以 `cancelled` 結束。
- 指令：`ping`、`version`、`open{path}`、`render{page,dpi,path?}`、`analyze{page,ocr?,ocrLines?,forceOcr?}`、
  `preview{edits,pages?,dpi?}`、`export{edits,out,verify?}`、`fonts`、`close`、`shutdown`。
- stdout 只放協定（`sys.stdout` 被導到 stderr），log 走 stderr。stdin 關閉 → 引擎自行結束（外殼當掉也不會留孤兒程序）。
- 影像以暫存 PNG 路徑交換（Session 的 temp 資料夾，`shutdown` 時刪除）。

### 2.2 引擎模組

| 模組 | 內容 |
|---|---|
| `geometry.py` | 顯示座標（pt、左上原點、已套 Rotate/CropBox）↔ user space |
| `models.py` | `TextStyle`、`Run`、`Block`、`Edit`（含 `Edit.from_block(block, **changes)`） |
| `pdfio.py` | `PdfDoc`：頁框繼承、render |
| `textlayer.py` | 讀文字層 → Block（ink 框、字型名/粗細/斜角/顏色、不可見 OCR 層判斷） |
| `contentstream.py` / `redact.py` | 位元組級 tokenizer；清空區域內的文字字串；Form XObject copy-on-write；`flate_stream()` |
| `analysis/` | 影像墨跡、符號幾何分類、行掃描（em/基線）、粗斜體與字級估計 |
| `ocr/` | `OcrEngine`（含 `dpi` 屬性）、`TesseractOcr`（傳真實 DPI；同目錄 tessdata 優先）、`ExternalOcr` |
| `scanpage.py` | 整頁 OCR 管線（見 3.2） |
| **`fonts.py`** | 字型索引（name table 所有語言別名、weight、italic、width、CFF、可嵌入；JSON 快取在 `%LOCALAPPDATA%\PdfTailor\fontindex.json`）；**Office 式字型分流**（CJK 與「寬度隨語系」符號 ■□「」※ 走東亞字型，其餘走西文字型，缺字走符號備援鏈）；`Embedder`（子集化 → CFF 轉 TrueType → 註冊 reportlab；`glyph_bounds` 取墨跡框） |
| **`fontmatch.py`** | 掃描行**字型辨識**：用 OCR 文字在每個候選字型渲染，與掃描墨跡比對（模糊遮罩相關係數 − 寬高比誤差），並依該字型的墨跡高度**反推字級** |
| **`writer.py`** | 寫回 PDF（見 3.3） |
| **`service.py`** | `Session`：open / render / analyze（含字型辨識 + 字級貼齊 Office 常用級數）/ preview / export / `verify_output` |
| **`server.py`** | JSON Lines 伺服器 |

## 3. 關鍵設計與實測結果（合成資料）

### 3.1 字型

- 無家族名的掃描行：`fontmatch` 在已安裝候選（Windows：Calibri、Arial、Times New Roman、Cambria、Verdana、Tahoma、Segoe UI、Georgia…；
  CJK：微軟正黑體、新細明體、標楷體…）中挑最像的；短行沿用同頁同語系多數決的字型。**實測 48/48 正確，字級中位誤差 1.3%**。
- 字級再「貼齊」Office 常用級數（誤差 ≤3.5% 才貼）。
- 粗/斜體優先用真的 Bold/Italic 字型檔，沒有才合成（描邊 3.5% 字級、剪切 12°）；缺字型/缺字元會在報告中提示。
- 嵌入：每次輸出把用到的字元子集化；Noto/思源等 CFF 字型會轉成 TrueType 曲線（reportlab 只吃 TrueType）。

### 3.2 OCR 管線強化（本次新增）

- **孤兒墨跡回收**：Tesseract 版面分析常漏掉孤立短詞（表頭「品名」「數量」）。把沒被任何辨識行涵蓋、像文字的墨跡
  （以 union-find 依「同列並排」或「同欄上下疊」分組，所以 品 的三個口會合在一起）逐塊裁切重新辨識（信心 ≥55 才收）。
- **弱行不佔位**：信心 <70 且 ≤3 字的行不阻擋孤兒搜尋；若孤兒結果覆蓋它就被取代（曾把「品名」的一半讀成「於」）。
- **低信心行重讀**：信心 <80 或 ≤4 字的整行，以單行模式（psm 7）、正規化到約 48px em 重讀，信心更高才採用；超大字（em>80px）會先縮小。
- **重疊行去重**、**傳給 Tesseract 真實 DPI**（原本寫死 240）。
- 結果：同一份 200dpi 掃描，以 200 / 240 / 300 / 400 DPI 分析，文字、表頭、粗體全部正確。

### 3.3 樣式量測（本次改進）

- 筆畫寬度改在**半對比度門檻**量測（模糊邊緣在半對比處正好是原邊緣），不再用固定像素補償 → 不受掃描模糊或放大倍率影響。
- 粗體基準改為**依字數加權的中位數**（避免粗體標題/表頭在短頁拉高基準）；門檻 `BOLD_RATIO=1.24`（一般行最高 1.17、粗體最低 1.31）。
- 實測：粗體 36/36、0 誤判；斜體 18/18、0 誤判；字級中位誤差 1.4%。

### 3.4 寫回 PDF（`writer.py`）

1. **移除舊字**：文字層 → 清空 content stream 中該區的字串；掃描頁 → 不碰原圖，改貼一塊**修補圖**：渲染 300dpi，
   遮罩舊字墨跡（含掃描模糊的淡邊，只取「貼著筆畫」的部分），**表格線等長細線不納入遮罩**，再用 OpenCV Telea inpaint
   以周圍紙張補回（保留紙紋與雜訊，不是白框）。同區的隱藏 OCR 文字層一併清除。
2. **畫新字**：reportlab overlay，**以墨跡對位**——新字的墨跡框落在舊字墨跡框的位置（用實際字型的字形邊界算基線；
   實測位置誤差 ≤0.6pt）。旋轉 0/90/180/270、CropBox 位移皆正確（測試涵蓋）。
3. **較長的新字**：先看右邊到下一欄/頁邊的空間，放得下就自然延伸（像 Word）；放不下才水平壓縮（最多到 85%），仍不夠就在報告提示。
4. **合併**：overlay 轉 Form XObject，以平移矩陣貼上（**不用 pikepdf add_overlay**，它會再補償一次 /Rotate 而轉錯）；
   Resources 若是繼承或共用，先做私有淺複本。
5. **存檔**：`compress_streams=False` + `stream_decode_level=none`（`compress_streams=True` 會讓 qpdf 去掉 ASCII85、重壓未修改的串流）；
   新串流自行 Flate 壓縮。→ **掃描原圖與未修改頁面逐位元組相同**（測試涵蓋）。
6. **驗證**：`export` 後重新渲染比對，修改範圍外有像素變化就回報（外殼完成框會顯示黃色警告）。

## 4. WPF 外殼（`app\PdfTailor`，程式碼已寫、未編譯）

| 檔案 | 內容 |
|---|---|
| `PdfTailor.csproj` | net9.0-windows10.0.19041.0、單檔自含 win-x64、無 NuGet 套件 |
| `App.xaml` | 直接複製 DocumentTailor 的深色主題（含看得清楚的下拉選單），加上 StyleToggle、ChipButton、FieldBox、ProgressBar、ToolTip |
| `Engine/EngineClient.cs` | 啟動引擎（順序：`PDFTAILOR_ENGINE` → `engine\pdftailor-engine.exe` → 開發時往上找 `engine\pdftailor_engine`，用 `.venv-build\Scripts\python.exe` 或 `py -3 -m pdftailor_engine`）、JSON Lines、id 配對、進度事件、CancellationToken → cancel、stderr 尾端附在錯誤訊息 |
| `Engine/BlockVm.cs` | 一行文字的 VM（原值 + 修改值），`ToEditJson()` 產生引擎要的 edit |
| `MainWindow.*` | 頁面檢視（PNG + 文字框疊層：藍=可編輯、黃虛線=低信心、綠=已修改、青=選取；已修改行會即時以近似字型顯示新字）、右側屬性面板（文字、原文、符號列 ■□●○▲△▼▽◆◇※「」『』☑☐✓→、字型、字級 ± 依 Office 級數、B/I、顏色色票+HEX、對齊、舊字背景：自動修補/純色/不覆蓋、提示）、縮放、換頁、重新辨識此頁、拖放開檔、Esc 取消、Tab 換行、Ctrl+B/I、分析時頁面上的掃描光束動畫、未輸出修改的關閉提醒 |
| `PreviewWindow.*` | **輸出前預覽**：原始/修改後左右並排（或只看其一），列出字型替代/缺字/超寬提示；「確定輸出」才存檔 |
| `DoneDialog.*` | **輸出完成**提示框（開啟檔案 / 開啟資料夾 / 完成；驗證失敗顯示黃色警告） |
| `Controls/ScanDemo.cs` | 空狀態英雄動畫：掃描光束掃過文件、每行變成可編輯框、標題被「改寫」；關閉系統動畫時靜態顯示；隱藏時暫停 |
| `Controls/DotMatrixSharkCursor.cs` | 從 `D:\AI\skills\dsh\desktop-shark-cursor\src` 複製，只改 namespace 為 `PdfTailor.Controls`；XAML 最後一個子元素、RowSpan=3、Loaded 時 `Attach(this)` |

## 5. 已知問題 / 下一步（建議順序）

1. **編譯並修正 WPF**（見 0.1）。然後實機走一遍：開檔 → 分析 → 編輯 → 預覽 → 輸出 → 完成框。
2. **真實文件驗證**（最大風險）：目前全部是合成掃描。至少試 5 份：乾淨掃描、手機拍攝、傳真品質、含表格、含圖表；
   特別觀察 OCR 錯字率、字型辨識在 Windows 大量候選字型下的表現（Arial vs Calibri vs Segoe UI 較難分）、修補痕跡。
3. **Windows OCR 整合**：外殼用 `Windows.Media.Ocr` 取行，經 `analyze{ocrLines:[{text,x,y,w,h,confidence}]}`（顯示座標 pt）
   傳給引擎（`ExternalOcr`，不能重裁切）。DocumentTailor 已有 Windows OCR 程式可參考。之後做 benchmark 決定預設引擎。
4. Tesseract 繁中錯字仍多（「個資法」→「個次法」等）→ 易混字表 `CONFUSABLE`、雙引擎投票、UI 標示低信心（已做）。
5. 掃描頁**傾斜校正**、多欄/表格版面、直書文字尚未處理。
6. PyInstaller 建置尚未在 Windows 實測：可能需要補 `--hidden-import` 或 `--collect-*`（看 `engine\build-engine-log.txt`）。
7. 可選：輸出時為掃描頁加整頁不可見文字層（可搜尋）。
8. 外殼目前一次只支援單行（Block 為一行）；多行段落重排尚未支援。
9. **公司電腦部署**：使用者要把這套工具放到有防火牆限制的公司電腦上。`publish\`
   資料夾本身執行不需網路（兩個 exe 都自含執行環境），只有「建置」需要連
   nuget.org / PyPI / Tesseract 官網。已加好 `vendor\tesseract\`（build-engine.cmd
   會優先用這裡的離線 Tesseract，見 `vendor/README.md`）。推薦做法仍是：在有網路
   的電腦建置完 `publish\`，壓縮上傳成 GitHub Release，公司電腦只下載解壓即可跑，
   完全不需要重新建置。細節見 `README.md` 的「部署到有防火牆限制的電腦」一節。

## 6. 踩過的坑（別再踩）

- 座標一律經 `PageGeometry`；reportlab `setPageRotation` 會連 MediaBox 一起換（測試用 pikepdf 設 Rotate）。
- `getbbox` 是 advance box 不是墨跡；對位要用字形邊界（fontTools BoundsPen）或實際渲染。
- pikepdf `add_overlay` 會補償 /Rotate → 自己貼 Form XObject。
- `pdf.save(compress_streams=True)` 會改動未修改的串流位元組。
- `redact.page_content_bytes` 不可 `contents_coalesce()`（會改寫頁面）；陣列內容要自己串接。
- Tesseract：`--dpi` 要傳真實值；CJK 逐字框不可靠（所以在符號處切行重讀）；版面分析會漏孤立短詞（孤兒回收）。
- 粗體判斷不能用固定像素模糊補償、不能用未加權中位數。
- 字型：Noto CJK 是 CFF（要轉 TrueType）；`.ttc` 要帶 face index；fsType 限制嵌入的字型要跳過。
- `device_commit_files` 同名覆蓋可能拿到舊檔 → 用新檔名。
- cmd.exe 批次檔必須 ASCII；git 在使用者碟上要使用者自己操作（容器無法刪 `.lock`）。

## 7. 授權

- 不用 PyMuPDF（AGPL-3.0）。依賴：pypdfium2（Apache/BSD）、pikepdf（MPL-2.0）、reportlab（BSD）、fontTools（MIT）、
  OpenCV（Apache-2.0）、Tesseract（Apache-2.0）。字型一律讀使用者本機，不隨程式散布。

## 8. 相關專案

- DocumentTailor：`D:\Codex\projects\DocumentTailor`（C#，已有 PDF 編輯 P1–P3）。⚠ 使用者仍需自己執行
  `git push --force-with-lease origin main`（先前 amend 移除了 Claude trailer）。
- Skills：`D:\AI\skills\dsh\`（ascii-sharks、desktop-shark-cursor）、`D:\AI\skills\claude\skill-vet`。

## 9. 驗收清單

- [x] 引擎測試全過（81）
- [x] 未修改區域像素差 0、掃描原圖位元組不變、未修改頁面位元組不變（測試）
- [x] 預覽與輸出同一流程
- [x] 符號辨識（■□●○▲△▼▽◆◇「」『』）與輸出字型分流
- [x] 粗/斜體/字級/顏色/字型保留（合成資料）
- [x] 輸出完成提示框、鯊魚游標、掃描動畫（程式碼）
- [x] **WPF 在 Windows 編譯通過**（2026-11-04 `release.cmd 1 app` 實測，先前兩個缺 `using System.IO;` 的錯誤已修正）
- [x] `release.cmd publish` 產出可執行的 `publish\PdfTailor.exe`（2026-11-04 實測成功，`OK: publish`）
- [ ] 實際執行 `publish\PdfTailor.exe`，開啟一份真實 PDF 跑一遍開檔→分析→編輯→預覽→輸出
- [ ] 真實掃描 PDF 驗證（乾淨掃描／手機拍／傳真品質／含表格／含圖表）
- [ ] 安裝或 vendor 好 Tesseract-OCR（含 chi_tra），目前本機還沒有，掃描頁無法 OCR
- [ ] 斷網實測完全離線
- [ ] git 初始化並推上 GitHub（作者僅 Louis）

---

## 追加（2026-11-04 下午）：側欄 / PDF 合併 / 鯊魚游標按鈕

> **這批 WPF 修改尚未編譯驗證**（寫碼環境沒有 dotnet / NuGet）。下一步：`release.cmd` → `1 app`，有編譯錯誤貼 `release-log.txt`。

- 左側功能欄 `NavRail`（編輯 PDF / PDF 合併），☰ 或 Ctrl+M 收合；狀態存 `%LOCALAPPDATA%\PdfTailor\ui.txt`。
- PDF 合併：`Services/PdfMergeService.cs`（PdfSharpCore 1.3.67，自 DocumentTailor 移植；新增 NuGet 相依）＋ `MainWindow.Merge.cs` ＋ MainWindow.xaml 的 `MergeView`。
  拖放加入 / 拖曳排序 / 上下移 / 先寫暫存檔再取代，輸出完成用 `DoneDialog(path, detail)` 新建構式。
- 狀態列右下角 `SharkToggle`：按下 → `SharkCursor.Detach()` 恢復一般游標，再按重新啟用。`DotMatrixSharkCursor` 新增 `IsAttached`，Detach 會清掉殘留畫格。
- 圖示：使用者選了 A「針線縫補」。`app/PdfTailor/Assets/PdfTailor.ico`（16–256px 共 7 尺寸；48px 以下用去掉「PDF」字樣的加粗版）、PdfTailor.png（512）、PdfTailor.svg（原稿）。
  csproj 已設 `ApplicationIcon` 並以 `Resource` 內嵌，MainWindow 的 `Icon` 也指向它。

## 追加（2026-11-04 晚）：PDF 轉 Word（側欄「W  PDF 轉 Word」）

- **做法**：結構判斷與 .docx 寫入都在 Python 引擎（`python-docx`，MIT，離線），WPF 只負責頁面範圍、存檔、進度。
  為什麼不用 C# OpenXML SDK：本機無法下載 NuGet、容器也不能編譯 C#，引擎端可以完整測試並用 LibreOffice 轉回 PDF 目視比對。
- 引擎新增：`tables.py`（框線表格：形態學抓線→格線→缺線=合併儲存格→底色格也算邊）、`docstruct.py`（行→段落、標題、清單、
  無框線對齊表格、對齊/縮排/間距）、`docxout.py`（python-docx 寫檔）、`Session.export_docx`、server 指令 `docx`
  （參數 `out`、`pages`[可省略=全部]、`edits`、`ocr`；有進度與取消）。
- 規則重點：折行以「上一行是否填滿到右緣（下一個字放得進去就不算滿）」判斷；首行縮排、凸排清單、中文不插空白（會濾掉引擎的字框空白）；
  標題依字級排名＋粗體＋編號深度對應 Heading 1–4；清單保留原符號並加 TAB（不是 Word 自動編號）；
  多欄文字行（頁首的「左 … 右日期」）用右對齊定位點；每個 PDF 頁一個 Word 區段，沿用頁面大小/邊界。
- WPF：`MainWindow.Word.cs`（新）、`Services/PageRange.cs`（新，解析 `1-3, 5, 8-`）、`MainWindow.xaml` 加 `NavWord`/`WordView`、
  `MainWindow.Merge.cs` 的 `Mode.Word`、`DoneDialog(path, detail, heading)`。**尚未在 Windows 編譯**。
- 打包：`pdftailor-engine.spec` 與 `build-engine.cmd` 已加入 `docx`（含預設範本資料檔）與 `lxml`。**引擎需重建**（`release.cmd` → 2 或 3）。
- 測試：`tests/test_docx.py`（20 項，含掃描頁 OCR、底色標題列）、`tests/docfix.py`（造 PDF 用，內嵌 TrueType 字型：
  reportlab 內建 Helvetica 不嵌入，pdfium 會把數字拆成「1 2」）。引擎測試共 101 項全過。
- 已知限制：不轉圖片、不轉頁首頁尾；只有橫線沒有直線的表格（三線表）會當成無框線表格；跨頁段落不會接起來；
  兩欄排版的文章會變成兩欄表格；Word 以自己的字型重新排字，換行位置可能與 PDF 不同。

---

## 追加（2026-11-05）：下一批功能規格——A. 修改紀錄＋保留原檔、B. 頁面整理

> **來源**：2026-11-05 的 User Story 練習（金融業頻繁整理掃描文件）。故事原文在
> `D:\AI\Document\20261105AI敏捷式開發\跨部門的需求共識.md` 的「User Story 練習：PDF 文件編輯器」：
> A 對應 **US-05**，B 對應 **US-04a（旋轉、刪除、重排）與 US-04b 的「拆分」**（合併已經有了）。
> **狀態（2026-11-05）：A、B 的引擎與 WPF 程式都已寫好。** 引擎有 pytest（容器內 128 項全過）；WPF 尚未實際編譯，請在 Windows 執行 `release.cmd` → `1 app`，有錯誤貼 `release-log.txt`。下方驗收清單中，標「（已測）」的是引擎測試已涵蓋，其餘要實機確認。
> 以下寫的是「要做什麼、改哪裡、怎麼驗收」，實作細節請以實際程式碼為準。
>
> **實作與規格的差異（請知悉）：**
> - `arrange-pages` 的 CSV 多一欄「動作類型」（保留／旋轉／刪除），被刪除的頁列在最後。
> - 只有「真的刪了頁」時才移除書籤（/Outlines）、/OpenAction、/StructTreeRoot、/Threads、命名目的地；純重排或只旋轉會保留書籤。指向被刪頁的連結註解與表單欄位會一併移除，最後再做一次掃描，確保沒有任何物件還引用被刪頁。
> - 輸出一律先寫到 `.tmp` 再 `os.replace`，失敗不會留下半成品，也不會碰原檔。
> - 整理頁面只讀原始 PDF，不含尚未輸出的文字修改（UI 上有橘色提示）。

### 0. 通則（兩項都適用）

1. **順序**：先做 A，再做 B。每項都「引擎先做＋pytest 通過，再接 WPF」。
2. **不新增 Python 套件**：只用已有的 pikepdf、pypdfium2、numpy、Pillow 與標準庫（hashlib、json、csv、getpass、socket、datetime）。授權維持寬鬆授權，**不引入 PyMuPDF（AGPL）**，也不引入 OCRmyPDF（需要 Ghostscript）。
3. **沿用既有原則**：完全離線；**絕不覆蓋原檔**；未修改的頁面與影像串流**逐位元組不變**；使用者看到的訊息一律繁體中文；`.cmd` 批次檔必須純 ASCII；git commit 只留作者 Louis（見第 0 節第 4 點）。
4. **協定風格**：新指令照 `server.py` 的 `handle()` 加分支；錯誤用 `EngineError`（顯示給使用者）；長工作支援 `progress` 事件與取消（`Cancelled`）。
5. **WPF 尚無法在容器編譯**：寫完要請使用者在 Windows 執行 `release.cmd` → `1 app`，有錯誤貼 `release-log.txt`。引擎改動後要重建（`release.cmd` → 2 或 3）。
6. 做完後更新本檔的「驗收清單」與 README 的功能說明。

---

### A. 修改紀錄＋保留原檔（US-05）

**要解決的需要**：金融業文件事後可能被稽核。使用者修改掃描文件後，主管或稽核要能看出「改了什麼、誰改的、什麼時候改的」，並確認原始檔沒有被動過。

#### A1. 保留原檔（現況與要補的）

- **現況**：`Session.export()` 與 `export_docx()` 已拒絕覆蓋原始檔（輸出路徑與 `doc.path` 相同就丟 `EngineError("不能覆蓋原始檔，請另存新檔。")`）。WPF 預設檔名為「原檔名_已編輯.pdf」。
- **要補**：
  1. 輸出完成後記錄**原檔與輸出檔的 SHA-256**，讓人能證明原檔沒變。
  2. `DoneDialog` 顯示一行「原始檔未更動」與修改紀錄的路徑（見 A4）。
  3. 之後新增的所有輸出指令（含 B 的 `arrange`）都要套用同一條「不可覆蓋原檔」規則。

#### A2. 紀錄檔

- **何時寫**：每次 `export`（輸出編輯後的 PDF）成功後，在輸出檔旁邊自動寫兩個檔。`preview` 不寫。
- **檔名**：輸出檔去掉副檔名後加 `.audit.json` 與 `.audit.csv`。例如輸出 `合約_已編輯.pdf` → `合約_已編輯.audit.json`、`合約_已編輯.audit.csv`。
- **JSON 是正本**，CSV 給人在 Excel 看。
- **寫檔方式**：先寫 `.tmp` 再 `os.replace`，不留下半成品。
- **JSON 結構**（`schema` 固定為 1；`changes` 只放**有修改**的行；`CollectEdits` 會把整頁的行都送來，要用 `Edit.modified` 過濾）：

```json
{
  "schema": 1,
  "tool": {"name": "PdfTailor", "version": "0.1.0"},
  "createdAt": "2026-11-05T14:30:12+08:00",
  "operator": {"user": "louis", "machine": "DESKTOP-O63APK0"},
  "action": "edit-text",
  "includeText": true,
  "source": {"path": "D:\\scan\\合約.pdf", "sha256": "…64 位十六進位…", "bytes": 1234567, "pageCount": 5},
  "output": {"path": "D:\\scan\\合約_已編輯.pdf", "sha256": "…", "bytes": 1234999, "pageCount": 5},
  "changes": [
    {
      "page": 2, "blockId": 14, "kinds": ["text", "style"],
      "originalText": "金額 100,000", "newText": "金額 150,000",
      "originalLength": 11, "newLength": 11,
      "originalBounds": [72.0, 140.5, 120.3, 12.0], "newBounds": [72.0, 140.5, 120.3, 12.0],
      "style": {"family": "Calibri", "size": 11.0, "bold": false, "italic": false, "color": [17, 17, 17]}
    }
  ],
  "verify": {"ok": true, "problems": []}
}
```

- **`kinds` 的判斷**（一行可有多種）：
  - `"text"`：`e.text != e.original_text` 且 `e.text.strip()` 不是空字串
  - `"delete"`：`e.modified` 且 `e.text.strip()` 是空字串（使用者把整行刪掉）
  - `"style"`：`e.style_dirty` 為真
  - `"move"`：`e.bounds != e.original_bounds`
- **限制要寫進程式註解**：`Edit` 只帶「新樣式」，沒有「原樣式」，所以紀錄只能記新樣式。
- **`bounds`** 是 `[x, y, w, h]`，單位 pt，顯示座標（與 `Block.to_json()` 相同）。
- **`includeText=false` 時**：不輸出 `originalText`、`newText`，只保留 `originalLength`、`newLength`。
- **`operator`**：`getpass.getuser()` 與 `socket.gethostname()`。
- **`createdAt`**：`datetime.now().astimezone().isoformat(timespec="seconds")`。
- **SHA-256**：以 1 MiB 為單位串流讀檔。
- **`verify`**：直接放 `export` 已經算出的 `verify_output()` 結果。
- **CSV**：編碼 UTF-8 含 BOM（`utf-8-sig`），欄位順序固定為
  `序號, 頁碼, 動作類型, 原文, 新文, 原字數, 新字數, 原位置, 新位置, 字型, 字級, 粗體, 斜體, 顏色`。
  - 「動作類型」把 `kinds` 用「、」串起來並翻成中文：text=修改文字、delete=刪除、style=修改樣式、move=移動。
  - 「位置」寫成 `x;y;w;h`。
  - `includeText=false` 時「原文」「新文」欄留空。
  - **防 CSV 公式注入**：任何儲存格內容以 `=`、`+`、`-`、`@` 開頭時，前面加一個單引號 `'`。
- **紀錄檔本身可能含機敏資料**（原文就是客戶資料）。所以 `includeText` 要可以關掉，UI 預設為「包含」，是否預設關閉待合規決定（見最後「待確認」）。

#### A3. 引擎改動

1. 新增 `engine/pdftailor_engine/audit.py`，提供：
   - `sha256_file(path) -> str`
   - `build_edit_audit(src: PdfDoc, out_path: str, edits: list[Edit], verify: dict | None, include_text: bool) -> dict`
   - `write_audit(audit: dict, out_path: str) -> dict`：寫出 `.audit.json` 與 `.audit.csv`，回傳 `{"json": 路徑, "csv": 路徑}`
   - 結構要能同時容納 `action = "edit-text"`（本節）與 `action = "arrange-pages"`（B 節，`changes` 內容不同，見 B2）。
2. 修改 `service.py` 的 `Session.export(edits, out_path, verify=True, audit=True, audit_text=True)`：
   - 寫完 PDF、跑完 `verify_output` 後，若 `audit` 為真就寫紀錄，結果放進 `result["audit"]`。
   - **紀錄寫失敗不要讓整個輸出失敗**：PDF 已經寫好了。改為 `result["auditError"] = "訊息"`，WPF 要顯示黃色警告（合規可能要求有紀錄，使用者必須知道）。
3. 修改 `server.py` 的 `export` 分支：新增選用參數 `audit`（預設 `true`）與 `auditText`（預設 `true`），傳給 `Session.export`。
4. `export_docx`、`preview`、`merge` 本次**不寫紀錄**（寫在「不做」）。

#### A4. WPF 改動

1. `PreviewWindow`（輸出前預覽）底部加兩個核取方塊，預設都勾選：
   - 「同時產生修改紀錄（建議）」
   - 「紀錄中包含文字內容」
   - 取消第一個時，第二個要停用。預覽視窗要把兩個值回傳給 `OnPreviewExport`。**不要把這兩個值寫進 `ui.txt`**（目前 `ui.txt` 的讀法是整檔比對 `nav=0`，多寫一行會讀壞；v1 每次都用預設值）。
2. `MainWindow.xaml.cs` 的 `OnPreviewExport`：呼叫 `export` 時多傳 `["audit"]`、`["auditText"]`。
3. `DoneDialog`：
   - 現有兩個建構式（`(path, modified, verified)` 與 `(path, detail, heading)`）**簽章都保留**（合併與轉 Word 在用）。
   - 新增選用參數 `auditPath`（字串或 null）與 `auditError`（字串或 null）。
   - 有 `auditPath`：在詳細說明加兩行「原始檔未更動。」「修改紀錄：<路徑>」。
   - 有 `auditError`：顯示黃色警告「PDF 已輸出，但修改紀錄寫入失敗：<訊息>」。

#### A5. 測試（新增 `engine/tests/test_audit.py`，仿 `test_writer.py` 與 `test_server.py` 的寫法）

用 `fixtures.make_text_pdf` 造 PDF，用 `Session` 與 `Edit.from_block` 造修改：

1. 輸出後 `.audit.json`、`.audit.csv` 都存在，JSON 可被 `json.load`，`schema == 1`。
2. `source.sha256` 等於實際用 `hashlib` 算出的原檔雜湊；`output.sha256` 等於輸出檔雜湊。
3. **原檔輸出前後雜湊不變**。
4. 只改一行時，`changes` 只有一筆，且 `kinds`、`originalText`、`newText` 正確；刪除一行 → `kinds == ["delete"]`；只改樣式 → `["style"]`；只移動 → `["move"]`。
5. `audit_text=False` 時，JSON 與 CSV 都不含原文與新文，`originalLength`、`newLength` 仍在。
6. CSV 以 `utf-8-sig` 讀得回來；新文為 `=1+1` 時，儲存格內容是 `'=1+1`。
7. 輸出路徑等於原檔路徑 → 丟 `EngineError`，且**不產生任何紀錄檔**。
8. 子程序測試（沿用 `test_server.py` 的 `Client`）：`export` 回傳含 `audit.json`、`audit.csv` 路徑；傳 `audit=False` 時不產生紀錄。
9. 讓紀錄寫入失敗（例如把紀錄檔路徑預先建成同名資料夾）→ PDF 仍輸出成功，結果含 `auditError`。

#### A6. 驗收（做完逐項打勾）

- [x] 引擎測試全過（含新增的 9 類測試）（已測：`tests/test_audit.py`）
- [ ] 實機：修改一行 → 預覽 → 輸出，輸出檔旁邊出現 `.audit.json` 與 `.audit.csv`
- [ ] CSV 用 Excel 開啟，中文正常、沒有亂碼
- [ ] 原檔的雜湊在輸出前後相同
- [ ] 取消「同時產生修改紀錄」→ 不產生紀錄檔
- [ ] `DoneDialog` 顯示紀錄路徑與「原始檔未更動」

#### A7. 本次不做

- **防竄改簽章**：紀錄只是「紀錄」，沒有簽章，**無法證明紀錄本身未被改過**。若合規要求，後續另外規劃（例如雜湊串接或數位簽章）。
- 合併、轉 Word 的紀錄。
- 上傳、集中收集紀錄（維持完全離線）。

---

### B. 頁面整理：旋轉、刪除、重排、拆分（US-04a、US-04b 的拆分）

**要解決的需要**：掃描文件常有掃歪、掃到多餘頁、順序錯誤，或一份檔案要依歸檔規則拆成數份。使用者要能快速整理成可交付的樣子。

**範圍**：旋轉頁面、刪除頁面、拖曳重排、拆分。**合併已存在**（`PdfMergeService`，PdfSharpCore），本次不動。

**這項不修改頁面內容**：只調整頁面的順序、旋轉旗標與去留，掃描原圖不動。

#### B1. 重要決定（照這樣做，不用重新討論）

1. **用 pikepdf 在引擎做**，不要用 PdfSharpCore。理由：已是依賴、能維持位元組不變，且能與 A 的紀錄模組共用。
2. **引擎只新增兩個指令**：`thumbnails`、`arrange`。**拆分由 WPF 對每一組頁面各呼叫一次 `arrange`**，引擎不寫 `split` 指令。
3. **一個原始頁在計畫裡最多出現一次**（v1 不支援複製頁）。理由：同一個頁面物件重複放進頁面清單會共用，旋轉其中一份會影響另一份。
4. **整理頁面是對「原始 PDF 檔」操作**，不含編輯模式中尚未輸出的文字修改。畫面上要明確提示：「此功能依原始 PDF 整理，不含尚未輸出的文字修改。若要兩者並用，請先輸出編輯後的 PDF，再開啟它來整理頁面。」
5. **輸出一律是新檔**，先寫到 `輸出路徑 + ".tmp"`，成功後 `os.replace`，失敗或取消不留下半成品。不可覆蓋原檔（與 A1 同一規則）。

#### B2. 引擎規格

**`thumbnails` 指令**
- 請求：`{"cmd": "thumbnails", "args": {"pages": [1,2,3], "dpi": 36}}`；`pages` 省略代表全部頁，`dpi` 預設 36。
- 回應：`{"images": [{"page": 1, "path": "…png", "width": 214, "height": 277, "dpi": 36}, …]}`。
- 實作：沿用 `PdfDoc.render_png`；每張送一次 `progress` 事件（stage 用 `"頁 N"`），支援取消。

**`arrange` 指令**
- 請求：`{"cmd": "arrange", "args": {"plan": [{"src": 3, "rotate": 90}, {"src": 1, "rotate": 0}], "out": "D:\\scan\\合約_整理.pdf", "audit": true}}`
  - `plan` 的順序就是輸出的頁面順序；**不在 `plan` 裡的原始頁就是被刪除**。
  - `src`：原始頁碼（從 1 起算）。`rotate`：**額外的**順時針旋轉角度，只能是 0、90、180、270。
- 驗證（錯誤都丟 `EngineError`，繁體中文訊息）：
  - `plan` 為空 → 「至少要保留 1 頁。」
  - `src` 超出範圍 → 「頁碼超出範圍：N」
  - `src` 重複 → 「每一頁最多只能出現一次。」
  - `rotate` 不在 0、90、180、270 → 「旋轉角度只能是 0、90、180、270。」
  - 輸出路徑等於原檔 → 「不能覆蓋原始檔，請另存新檔。」
- 回應：`{"path": "…", "pageCount": 2, "sha256": "…", "deleted": [2, 4], "audit": {"json": "…", "csv": "…"}}`（`auditError` 規則同 A3）。
- 新增 `engine/pdftailor_engine/pageops.py`，放 `arrange(src_path, plan, out_path)`，`Session.arrange(...)` 與 `server.py` 只做薄薄的呼叫。
- 實作要點：
  1. 以 `pikepdf.open` 開原檔，依 `plan` 重排 `pdf.pages`（切片指派或 insert、delete，實作後用測試確認行為），再逐頁 `page.rotate(角度, relative=True)`。
  2. 存檔參數與 `writer.py` 相同：`compress_streams=False`、`stream_decode_level=pikepdf.StreamDecodeLevel.none`，才能保持未動串流位元組不變（見第 3.4 節第 5 點）。
  3. **被刪除頁的資料不可留在輸出檔裡**。注意書籤（`/Outlines`）、`/Names` 底下的 `/Dests`、`/OpenAction`、以及指向被刪除頁的連結註解，都會讓 qpdf 把被刪除的頁面物件一起寫出去。處理方式：輸出前移除根物件的 `/Outlines`、`/Names` 裡的 `/Dests`、`/OpenAction`，並移除保留頁中目的地指向被刪除頁的 Link 註解；測試要用書籤造出這種情況來驗證。
  4. **繼承屬性**：頁面的 `/Rotate`、`/MediaBox`、`/CropBox`、`/Resources` 可能繼承自 `/Pages` 節點（`pdfio.py` 的 `_inherited()` 就是處理這個）。重排或旋轉後必須確認幾何沒跑掉；若發現遺失，輸出前把繼承來的屬性明確寫到頁面本身。測試要用「屬性繼承自 `/Pages` 節點」的 PDF 驗證。
  5. 密碼保護的 PDF：`PdfDoc` 開啟時已丟使用者錯誤，不需另外處理。
- **紀錄**：沿用 A 的 `audit.py`，`action = "arrange-pages"`；`changes` 每個輸出頁一筆 `{"outPage": 1, "srcPage": 3, "rotate": 90}`，另在頂層加 `"deletedSrcPages": [2, 4]`；`source`、`output`、`operator`、`createdAt`、雜湊欄位與 A2 相同；CSV 欄位為 `序號, 輸出頁碼, 原始頁碼, 旋轉角度`，被刪除的頁另列最後幾行，動作類型寫「刪除」。

#### B3. WPF 規格

仿照「PDF 轉 Word」（`MainWindow.Word.cs`、`MainWindow.xaml` 的 `WordView`）與「PDF 合併」（`MainWindow.Merge.cs` 的拖曳排序）的做法：

1. **側欄**：`NavRail` 新增按鈕 `NavPages`，文字「▦   整理頁面」，位於「PDF 轉 Word」之下；`Click="OnNavPages"`。
2. **模式**：`MainWindow.Merge.cs` 的 `enum Mode { Edit, Merge, Word }` 加入 `Pages`；`SetMode()` 要補：`PagesView` 的顯示/隱藏、`NavPages.Tag`、`SubTitle`（「整理頁面」）、進入時的狀態列文字。
3. **按鍵與拖放**：`MainWindow.xaml.cs` 的 `OnKeyDown`（約第 523 行起）與 `OnDrop`（約第 133 行起）會依 `_mode` 分流，**要為 `Mode.Pages` 補上**：`Ctrl+O` 開檔、忙碌時 `Esc` 取消；拖入 PDF 時開啟該檔（與編輯模式相同）。
4. **新增 `PagesView`**（`Grid.Column="1"`，預設 `Collapsed`），兩欄：
   - **左：縮圖清單**。`ListView` 內用 `WrapPanel` 排列；每個項目顯示縮圖、「第 N 頁」（原始頁碼）；已旋轉的頁，縮圖用 `RenderTransform` 顯示旋轉後的樣子；可多選（Ctrl／Shift）。
   - **拖曳重排**：照 `MergeList` 的 `_dragItem`、`_dragStart` 做法。
   - **右：操作面板**（寬 340，樣式同 `WordView` 右欄）：
     - 按鈕「⟲ 向左轉 90°」「⟳ 向右轉 90°」：套用到選取的頁。
     - 按鈕「刪除選取的頁」：從清單移除（不可刪光，至少留 1 頁）。
     - 按鈕「全選」「重設」（重設＝重新載入縮圖，回到原始狀態）。
     - 按鈕「輸出為新 PDF  →」：呼叫 `arrange`，預設檔名「原檔名_整理.pdf」，存檔對話框與 `OnWordConvert` 相同。
     - 區塊「拆分」：三種方式——「每 N 頁一份」（數字輸入）、「依頁碼分組」（文字輸入，例如 `1-3; 4-6; 7-`，組與組之間用分號 `;` 或 `；`）、「每頁一份」；按鈕「拆分並輸出…」。
5. **資料模型**：`ObservableCollection<PageItem>`，`PageItem` 欄位：`SrcPage`（int）、`Rotation`（0/90/180/270，順時針累計並對 360 取餘）、`ThumbPath`、`Order`（畫面上的序號）。刪除就是從集合移除。
6. **載入縮圖的時機**：進入 `Mode.Pages` 且已開啟 PDF 時，若目前的縮圖不是這個檔案的，就呼叫 `thumbnails`；顯示「縮圖載入中…」並可用 `Esc` 取消。
7. **拆分規則**：
   - 「每 N 頁一份」：依目前清單順序切段。
   - 「依頁碼分組」：每組文字交給 `Services/PageRange.cs` 的 `TryParse` 解析（`PageRange` 現有的是「把整串合成一個集合」，所以**先用 `;` 切成多段，各段分別呼叫**）；每組頁碼是**原始頁碼**，並依清單目前的順序輸出。任何一組解析失敗就顯示錯誤並中止，不輸出任何檔案。
   - 輸出資料夾用 `Microsoft.Win32.OpenFolderDialog`（.NET 8 以上有）選擇。
   - 檔名：`<原檔名>_<頁碼描述>.pdf`，頁碼描述用 `PageRange.Describe`，並把 `, ` 換成 `_`（例如 `合約_p1-3.pdf`）。**若同名檔已存在，改在檔名後加 `_2`、`_3`…，不可覆蓋。**
   - 依序呼叫 `arrange`，進度顯示「拆分中 2 / 5…（Esc 取消）」；取消時，已輸出的檔案保留，並在完成框列出已輸出的份數。
8. **完成框**：用 `DoneDialog(path, detail, heading)` 通用版；`heading` 為「整理完成」或「拆分完成」；`detail` 寫明輸出了幾頁／幾份、刪除了哪些頁、「原始檔未更動」，以及紀錄檔路徑。
9. **提示文字**：`PagesView` 上方固定顯示 B1 第 4 點的提示（依原始 PDF 整理、不含未輸出的文字修改）。

#### B4. 測試（新增 `engine/tests/test_pageops.py`）

沿用 `fixtures.make_text_pdf` 造多頁 PDF（單頁造完再用 pikepdf 合成，或在 `fixtures.py` 補一個 `make_multipage_pdf`）；掃描頁可用 `scanpdf.py`：

1. **重排**：3 頁，`plan` 為 `[3, 1, 2]`，輸出的每頁文字依序對應，頁數 3。
2. **刪除**：`plan` 只含 `[1, 3]`，輸出頁數 2，內容對應 1、3；回應的 `deleted == [2]`。
3. **旋轉**：`rotate=90` 的頁，輸出的 `/Rotate` 比原來多 90（原本 270 → 0，要對 360 取餘）；渲染後尺寸的寬高對調；既有 `PageGeometry` 讀得出正確幾何。
4. **位元組不變**：用掃描 PDF 驗證，保留頁的影像串流位元組與原檔相同。
5. **刪除頁的資料不殘留**：造一份含唯一標記影像（或唯一標記文字）的頁並刪除它，確認輸出檔位元組中找不到該標記。
6. **書籤情況**：造一份有 `/Outlines` 指向被刪除頁的 PDF，刪除該頁後，輸出檔仍找不到被刪頁的標記，且輸出檔能被 `pikepdf` 與 `pypdfium2` 正常開啟。
7. **繼承屬性**：造一份 `/Rotate`、`/MediaBox` 繼承自 `/Pages` 節點的 PDF，重排與旋轉後幾何正確。
8. **驗證錯誤**：空 `plan`、`src` 超出範圍、`src` 重複、`rotate=45`、輸出等於原檔，分別得到對應的 `EngineError` 訊息；輸出等於原檔時不產生任何檔案。
9. **原檔不變**：輸出前後原檔雜湊相同。
10. **`thumbnails`**：回傳的 PNG 都存在；`pages` 省略時張數等於頁數；寬高與頁面比例一致。
11. **紀錄**：`arrange` 之後 `.audit.json` 的 `action == "arrange-pages"`，`changes` 與 `deletedSrcPages` 正確。
12. **子程序測試**：用 `test_server.py` 的 `Client` 呼叫 `thumbnails` 與 `arrange`。

#### B5. 驗收（做完逐項打勾）

- [x] 引擎測試全過（含新增測試）（已測：`tests/test_pageops.py`）
- [ ] 實機：開一份多頁 PDF → 進入「整理頁面」→ 縮圖出現
- [ ] 旋轉、刪除、拖曳重排後輸出，用 PDF 閱讀器開啟，順序、方向、頁數正確
- [x] 刪除頁的影像不在輸出檔內（以 B4 第 5、6 項驗證）（引擎已測，含「關掉保護就會洩漏」的對照組）
- [ ] 拆分三種方式都能輸出；已存在同名檔案時自動加 `_2`，不覆蓋
- [ ] 拆分過程中 `Esc` 取消，已輸出的檔案保留，完成框顯示份數
- [ ] 原檔雜湊不變；輸出旁有 `.audit.json`、`.audit.csv`
- [ ] 掃描 PDF 整理後，再用「編輯 PDF」開啟輸出檔，仍能辨識並編輯

#### B6. 本次不做

- 複製頁面、裁切、頁面大小調整
- 合併時的紀錄（`PdfMergeService` 維持原樣）
- 整理頁面與未輸出的文字修改同時進行（見 B1 第 4 點）

---

### 待確認（需要使用者或合規單位決定，AI 不要自行決定）

1. 修改紀錄的**保存期限與格式**，以及**預設是否包含文字內容**（目前預設包含）。
2. **操作者**以 Windows 帳號名稱與電腦名稱識別是否可接受。
3. 紀錄是否要涵蓋合併與轉 Word。
4. 拆分檔案的**命名規則**是否符合公司歸檔慣例（目前為「原檔名_頁碼描述」）。
5. 稽核是否需要**防竄改**（簽章或雜湊串接）。


---

## 追加（2026-11-05）：OCR 與轉 Word 準確度修正（以 `範例/` 兩份檔案實測）

`範例/` 內有個資（姓名、薪資），已加入 `.gitignore`，**不可推上 GitHub**。

**量測方式**：掃描檔（錄用通知書）人工打出正解，逐行比對字元錯誤率（CER）。
修正前 13.2% → 修正後 2.8%；轉 Word 由 6 頁亂版 → 1 頁、表格 8×4 正確。網頁 PDF（登機證）文字 100% 正確、1 頁。

**找到的原因與修正**（引擎，pytest 138 項全過，新增 `tests/test_ocr_accuracy.py`）：
1. 「整行被大改」：線被切段後，每段重新單獨 OCR 並**直接取代**頁面辨識的結果；粗體／底線字單獨辨識常變亂碼。
   → Tesseract 改輸出 hOCR（每個字的位置），切段時沿用原本的辨識結果；重新辨識只在「信心明顯較高且是同一段文字」或「只有它符合墨跡寬度」時才採用。
2. 表格整列被讀成一行（「報到|110年…|報到中國人壽…」）：
   → 依表格**實際畫出的直線**切開；表格線本身（| [ ]）丟掉；量測文字前先把表格線塗掉。
3. 掃描稍微歪斜（約 0.4°）時表格完全偵測不到；粗體字的筆畫被當成表格線（一個表格變 35 欄）。
   → 線的粗細改用「墨量／長度」；筆畫兩側有墨的不是表格線；落在已辨識文字內的筆畫不是表格線；Logo、粗字不當成底色格。
4. 標點：中文旁的 `, : ; ! ?` 轉全形。
5. 清單符號：同一欄多數行以 ※ 開頭時，誤讀成 K、HK、*、了 的改回 ※；Word 保留 ※ 而不是換成圓點。
6. 字級：同一份清單被估成 9–12 pt → 以中文字距推算並對齊頁面常用字級。
7. 文字型 PDF：窄字（1）的緊框造成數字被拆開（`1 1 7`）→ 用字寬（advance）判斷空格；字級不同且有間距時分成不同區塊。
8. 轉 Word：標題樣式的斜體不外漏；頁底留一點空間避免最後一列被擠到下一頁。

**仍存在（Tesseract 模型本身的辨識錯誤）**：吳秉儒→RKB、團膳→團騰、繳→緻、年→£、Logo 讀成 Cd、手寫簽名與日期變亂碼。
tessdata_best 在此樣本反而較差（CER 13.8%、慢 2.4 倍），維持 fast。下一步若要再提升，是試 PP-OCRv5（RapidOCR，需使用者同意新增套件與模型檔約數十 MB）。
