"""修改紀錄（稽核軌跡）：誰、何時、把哪個檔案改成什麼，以及原始檔沒有被動過的證據。

每次輸出成功後，在輸出檔旁邊寫兩個檔：

* ``<輸出檔>.audit.json``：正本，結構固定（``schema`` = 1）。
* ``<輸出檔>.audit.csv``：給人用 Excel 看（UTF-8 含 BOM，已防公式注入）。

兩種動作共用同一個結構：

* ``edit-text``：編輯文字後輸出 PDF（``changes`` 只放真正有修改的行）。
* ``arrange-pages``：整理頁面（旋轉、刪除、重排）後輸出 PDF。

限制（寫在這裡，避免日後被誤解）：

* ``Edit`` 只帶「新樣式」，沒有「原樣式」，所以紀錄只能記新樣式。
* 紀錄只是「紀錄」，沒有簽章，**無法證明紀錄本身未被改過**。
* 紀錄可能含客戶資料（原文），因此 ``include_text=False`` 時只留字數。
"""

from __future__ import annotations

import csv
import datetime as _dt
import getpass
import hashlib
import json
import os
import socket
from typing import Any

import pikepdf

from . import __version__
from .models import Edit

SCHEMA = 1
_CHUNK = 1 << 20  # 1 MiB

KIND_LABELS = {"text": "修改文字", "delete": "刪除", "style": "修改樣式", "move": "移動"}
EDIT_CSV_COLUMNS = ["序號", "頁碼", "動作類型", "原文", "新文", "原字數", "新字數", "原位置", "新位置", "字型", "字級", "粗體", "斜體", "顏色"]
ARRANGE_CSV_COLUMNS = ["序號", "輸出頁碼", "原始頁碼", "旋轉角度", "動作類型"]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def audit_paths(out_path: str) -> tuple[str, str]:
    base = os.path.splitext(os.path.abspath(out_path))[0]
    return base + ".audit.json", base + ".audit.csv"


def _operator() -> dict[str, str]:
    try:
        user = getpass.getuser()
    except Exception:  # no passwd entry, no env vars...
        user = ""
    try:
        machine = socket.gethostname()
    except Exception:
        machine = ""
    return {"user": user, "machine": machine}


def _page_count(path: str) -> int:
    with pikepdf.open(path) as pk:
        return len(pk.pages)


def _file_info(path: str, page_count: int | None = None, sha: str | None = None) -> dict[str, Any]:
    path = os.path.abspath(path)
    return {
        "path": path,
        "sha256": sha if sha is not None else sha256_file(path),
        "bytes": os.path.getsize(path),
        "pageCount": page_count if page_count is not None else _page_count(path),
    }


def _header(action: str, src_path: str, src_pages: int, out_path: str, src_sha_before: str | None) -> dict[str, Any]:
    src = _file_info(src_path, src_pages)
    # The same file hashed before the export started (when the caller did) and now: equal
    # means the original really was left alone.
    if src_sha_before is not None:
        src["unchanged"] = src_sha_before == src["sha256"]
    return {
        "schema": SCHEMA,
        "tool": {"name": "PdfTailor", "version": __version__},
        "createdAt": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "operator": _operator(),
        "action": action,
        "source": src,
        "output": _file_info(out_path),
    }


# ---- edit-text ------------------------------------------------------------------------


def _rect(r) -> list[float]:
    return [round(float(r.x), 2), round(float(r.y), 2), round(float(r.w), 2), round(float(r.h), 2)]


def edit_kinds(e: Edit) -> list[str]:
    """What the user did to one line (a line can have several of these at once)."""
    kinds: list[str] = []
    blank = e.text.strip() == ""
    if e.text != e.original_text and not blank:
        kinds.append("text")
    if e.modified and blank and e.original_text.strip() != "":
        kinds.append("delete")
    if e.style_dirty:
        kinds.append("style")
    if e.bounds != e.original_bounds:
        kinds.append("move")
    return kinds


def _edit_change(e: Edit, kinds: list[str], include_text: bool) -> dict[str, Any]:
    c: dict[str, Any] = {"page": e.page, "blockId": e.id, "kinds": kinds}
    if include_text:
        c["originalText"] = e.original_text
        c["newText"] = e.text
    c["originalLength"] = len(e.original_text)
    c["newLength"] = len(e.text)
    c["originalBounds"] = _rect(e.original_bounds)
    c["newBounds"] = _rect(e.bounds)
    s = e.style
    c["style"] = {"family": s.family, "size": round(float(s.size), 2), "bold": bool(s.bold), "italic": bool(s.italic), "color": list(s.color)}
    return c


def build_edit_audit(
    src,
    out_path: str,
    edits: list[Edit],
    verify: dict | None,
    include_text: bool = True,
    src_sha_before: str | None = None,
) -> dict[str, Any]:
    """The audit record for an ``export``. ``src`` is the open source ``PdfDoc``."""
    audit = _header("edit-text", src.path, src.page_count, out_path, src_sha_before)
    audit["includeText"] = bool(include_text)
    changes = []
    for e in edits:
        if not e.modified:
            continue
        kinds = edit_kinds(e)
        if kinds:
            changes.append(_edit_change(e, kinds, include_text))
    audit["changes"] = changes
    audit["verify"] = verify
    return audit


# ---- arrange-pages --------------------------------------------------------------------


def build_arrange_audit(
    src,
    out_path: str,
    plan: list[dict[str, int]],
    deleted: list[int],
    src_sha_before: str | None = None,
) -> dict[str, Any]:
    audit = _header("arrange-pages", src.path, src.page_count, out_path, src_sha_before)
    audit["changes"] = [{"outPage": i + 1, "srcPage": int(p["src"]), "rotate": int(p.get("rotate", 0))} for i, p in enumerate(plan)]
    audit["deletedSrcPages"] = [int(p) for p in deleted]
    return audit


# ---- writing --------------------------------------------------------------------------


def _cell(v: Any) -> Any:
    """Spreadsheet formula injection: text starting with = + - @ is typed as a formula by
    Excel, so it gets a leading apostrophe (shown by Excel as plain text)."""
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def _pos(r: list[float]) -> str:
    return ";".join(f"{v:g}" for v in r)


def _edit_rows(audit: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for i, c in enumerate(audit["changes"], 1):
        s = c["style"]
        rows.append(
            [
                i,
                c["page"],
                "、".join(KIND_LABELS[k] for k in c["kinds"]),
                c.get("originalText", ""),
                c.get("newText", ""),
                c["originalLength"],
                c["newLength"],
                _pos(c["originalBounds"]),
                _pos(c["newBounds"]),
                s["family"] or "",
                s["size"],
                "是" if s["bold"] else "否",
                "是" if s["italic"] else "否",
                ";".join(str(v) for v in s["color"]),
            ]
        )
    return rows


def _arrange_rows(audit: dict[str, Any]) -> list[list[Any]]:
    rows = []
    n = 0
    for c in audit["changes"]:
        n += 1
        rows.append([n, c["outPage"], c["srcPage"], c["rotate"], "旋轉" if c["rotate"] else "保留"])
    for p in audit["deletedSrcPages"]:
        n += 1
        rows.append([n, "", p, "", "刪除"])
    return rows


def _write_atomic(path: str, writer) -> None:
    tmp = path + ".tmp"
    try:
        writer(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_audit(audit: dict[str, Any], out_path: str) -> dict[str, str]:
    """Writes ``<out>.audit.json`` and ``<out>.audit.csv`` next to the output file."""
    json_path, csv_path = audit_paths(out_path)
    if audit["action"] == "arrange-pages":
        header, rows = ARRANGE_CSV_COLUMNS, _arrange_rows(audit)
    else:
        header, rows = EDIT_CSV_COLUMNS, _edit_rows(audit)

    def w_json(p: str) -> None:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)

    def w_csv(p: str) -> None:
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([_cell(v) for v in r])

    _write_atomic(json_path, w_json)
    _write_atomic(csv_path, w_csv)
    return {"json": json_path, "csv": csv_path}
