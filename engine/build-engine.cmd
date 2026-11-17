@echo off
rem ------------------------------------------------------------------
rem  Builds the PdfTailor engine (Python) into  ..\publish\engine\
rem  Needs: Python 3.11+ on PATH (py launcher or python.exe)
rem  Optional: Tesseract-OCR installed (copied next to the engine)
rem  Log: engine\build-engine-log.txt
rem
rem  Steps are skipped when nothing they depend on changed (fingerprints
rem  from buildhash.py, kept in .venv-build\ and build\):
rem    packages : requirements.txt unchanged          -> no pip
rem    tests    : engine sources and tests unchanged   -> no pytest
rem    engine   : engine sources unchanged and the exe -> no PyInstaller
rem               is still in publish\engine
rem  "release.cmd clean" (or deleting engine\build) forces a full build.
rem  Set PDFTAILOR_FULL_BUILD=1 to force it once without cleaning.
rem ------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "LOG=%~dp0build-engine-log.txt"
set "OUT=%~dp0..\publish\engine"
set "STAMPS=%~dp0build"
echo [%date% %time%] build start > "%LOG%"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo ERROR: Python 3 not found. Install Python 3.11+ and tick "Add to PATH".
  echo ERROR: python not found >> "%LOG%"
  exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
  echo Creating build venv...
  %PY% -m venv .venv-build >> "%LOG%" 2>&1 || goto :fail
)
set "VPY=%~dp0.venv-build\Scripts\python.exe"
if not exist "%STAMPS%" mkdir "%STAMPS%" >nul 2>&1
if "%PDFTAILOR_FULL_BUILD%"=="1" (
  del /q "%STAMPS%\*.sha" ".venv-build\req.sha" >nul 2>&1
)

rem ---- packages ------------------------------------------------------
set "H_REQ="
for /f %%H in ('"%VPY%" buildhash.py req') do set "H_REQ=%%H"
if not defined H_REQ set "H_REQ=unknown-%RANDOM%"
set "OLD="
if exist ".venv-build\req.sha" set /p OLD=<".venv-build\req.sha"
if "%OLD%"=="%H_REQ%" (
  echo Packages: up to date.
) else (
  echo Installing packages ^(first run takes a few minutes^)...
  "%VPY%" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller >> "%LOG%" 2>&1 || goto :fail
  >".venv-build\req.sha" echo %H_REQ%
)

rem ---- optional: PP-OCR (RapidOCR + onnxruntime) -----------------
rem  Much better than Tesseract on small or soft scans. Optional: if the
rem  packages or the model files cannot be fetched the build goes on and
rem  the engine uses Tesseract. Models are staged once in vendor\ocr-models.
set "OCR_ARGS="
if not exist ".venv-build\ocr.ok" (
  echo Installing OCR packages ^(RapidOCR, onnxruntime^)...
  "%VPY%" -m pip install --disable-pip-version-check -r requirements-ocr.txt >> "%LOG%" 2>&1 && >".venv-build\ocr.ok" echo ok
  if exist ".venv-build\ocr.ok" del /q "build\engine.sha" >nul 2>&1
)
if exist ".venv-build\ocr.ok" (
  set "OCR_ARGS=--collect-all rapidocr --collect-all onnxruntime"
  if not exist "%~dp0..\vendor\ocr-models\*rec*.onnx" (
    echo Fetching PP-OCR models ^(PP-OCRv6 medium, once^)...
    "%VPY%" -m pdftailor_engine.ocr.rapid --fetch "%~dp0..\vendor\ocr-models" >> "%LOG%" 2>&1
  )
) else (
  echo WARNING: OCR packages not installed - the engine will use Tesseract only.
)

rem ---- tests ---------------------------------------------------------
set "H_TESTS="
for /f %%H in ('"%VPY%" buildhash.py tests') do set "H_TESTS=%%H"
if not defined H_TESTS set "H_TESTS=unknown-%RANDOM%"
set "OLD="
if exist "%STAMPS%\tests.sha" set /p OLD=<"%STAMPS%\tests.sha"
if "%OLD%"=="%H_TESTS%" (
  echo Tests: passed before on this exact code, skipped.
) else (
  echo Running tests...
  "%VPY%" -m pytest tests -q -p no:cacheprovider -W ignore::DeprecationWarning >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo WARNING: some tests failed, see build-engine-log.txt. Building anyway.
    del /q "%STAMPS%\tests.sha" >nul 2>&1
  ) else (
    >"%STAMPS%\tests.sha" echo %H_TESTS%
  )
)

rem ---- engine --------------------------------------------------------
set "H_ENG="
for /f %%H in ('"%VPY%" buildhash.py engine') do set "H_ENG=%%H"
if not defined H_ENG set "H_ENG=unknown-%RANDOM%"
set "OLD="
if exist "%STAMPS%\engine.sha" set /p OLD=<"%STAMPS%\engine.sha"
if "%OLD%"=="%H_ENG%" if exist "%OUT%\pdftailor-engine.exe" (
  echo Engine: no source changes since the last build, skipped.
  goto :tesseract
)

echo Building engine...
rem  No --clean: PyInstaller reuses its analysis cache in build\ when nothing changed.
rem  Excluded modules are never imported by the engine; PyInstaller only found them
rem  through optional imports inside other packages (scipy alone was 68 MB).
"%VPY%" -m PyInstaller --noconfirm --onedir --console ^
  --name pdftailor-engine ^
  --distpath "%~dp0dist" --workpath "%~dp0build" ^
  --collect-all pypdfium2 --collect-all pypdfium2_raw ^
  --collect-data reportlab --collect-submodules reportlab ^
  --collect-submodules fontTools ^
  --collect-data docx --collect-submodules docx --collect-submodules lxml ^
  --exclude-module scipy --exclude-module pytest --exclude-module _pytest ^
  --exclude-module pygments --exclude-module tkinter --exclude-module matplotlib ^
  --exclude-module IPython --exclude-module pandas --exclude-module win32com ^
  %OCR_ARGS% ^
  run_engine.py >> "%LOG%" 2>&1 || goto :fail

rem  OpenCV's video plugin (FFmpeg, ~30 MB) is only loaded for video capture.
del /q "dist\pdftailor-engine\_internal\cv2\opencv_videoio_ffmpeg*.dll" >nul 2>&1

if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%" >nul 2>&1
xcopy /e /i /q /y "dist\pdftailor-engine" "%OUT%" >> "%LOG%" 2>&1 || goto :fail
>"%STAMPS%\engine.sha" echo %H_ENG%

:tesseract
rem ---- bundle Tesseract if it is installed --------------------------
rem  Priority: vendor\tesseract (pre-staged offline copy, see vendor\README.md)
rem            > installed Tesseract-OCR (Program Files / LocalAppData)
rem  Copied only when missing from publish\engine (it never changes between builds).
set "TESS="
if exist "%~dp0..\vendor\tesseract\tesseract.exe" set "TESS=%~dp0..\vendor\tesseract"
if not defined TESS if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESS=%ProgramFiles%\Tesseract-OCR"
if not defined TESS if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESS=%LOCALAPPDATA%\Programs\Tesseract-OCR"
if defined TESS (
  if exist "%OUT%\tesseract\tesseract.exe" (
    echo Tesseract: already in place.
  ) else (
    echo Copying Tesseract from "%TESS%"...
    xcopy /e /i /q /y "%TESS%" "%OUT%\tesseract" >> "%LOG%" 2>&1
  )
  if not exist "%OUT%\tesseract\tessdata\chi_tra.traineddata" (
    echo WARNING: chi_tra.traineddata missing - Traditional Chinese OCR will not work.
    echo          Download it from github.com/tesseract-ocr/tessdata_fast into "%OUT%\tesseract\tessdata".
  )
) else (
  if not exist "%OUT%\tesseract\tesseract.exe" (
    echo WARNING: Tesseract-OCR not found. Scanned pages cannot be read until it is installed
    echo          ^(UB Mannheim build, with Chinese Traditional^) or copied to "%OUT%\tesseract".
    echo          For firewalled machines: stage it once under "vendor\tesseract\" instead - see vendor\README.md.
  )
)

rem ---- PP-OCR models next to the engine -----------------------------
if exist "%~dp0..\vendor\ocr-models\*rec*.onnx" (
  if not exist "%OUT%\models" mkdir "%OUT%\models" >nul 2>&1
  xcopy /d /q /y /i "%~dp0..\vendor\ocr-models\*" "%OUT%\models" >> "%LOG%" 2>&1
  echo OCR models: in place.
) else (
  echo WARNING: PP-OCR models incomplete in vendor\ocr-models - scans are read with Tesseract. See the log.
)

echo.
echo Engine ready: "%OUT%\pdftailor-engine.exe"
echo [%date% %time%] build ok >> "%LOG%"
exit /b 0

:fail
echo.
echo BUILD FAILED - see "%LOG%"
echo [%date% %time%] build failed >> "%LOG%"
exit /b 1
