"""整理頁面：旋轉、刪除、重排（拆分由外殼對每一組頁面各呼叫一次）。

只調整頁面的順序、旋轉旗標與去留，**不碰頁面內容**：保留頁的內容串流、影像串流逐位元組
與原檔相同（存檔參數與 :mod:`writer` 一樣，不重新壓縮）。

被刪除頁的資料不可留在輸出檔裡。qpdf 只會寫出「從根物件讀得到」的物件，所以真正的風險是
還有東西指向被刪除的頁：書籤、命名目的地、開啟動作、保留頁上指向它的連結、結構樹、
表單欄位、討論串。這裡先移除這些來源，最後再掃一遍所有物件，把殘留的參照拿掉。
"""

from __future__ import annotations

import os
from typing import Any

import pikepdf

from .pdfio import _inherited

_ROTATIONS = (0, 90, 180, 270)
_INHERITABLE = ("/MediaBox", "/CropBox", "/Rotate", "/Resources")


class PlanError(ValueError):
    """The plan is not valid; the message is shown to the user as is."""


def validate_plan(plan: Any, page_count: int) -> list[tuple[int, int]]:
    """Checks a plan and returns it as ``[(src_page, extra_clockwise_rotation), ...]``."""
    if not plan:
        raise PlanError("至少要保留 1 頁。")
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for item in plan:
        try:
            src = int(item["src"])
            rot = int(item.get("rotate", 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PlanError("整理計畫格式不正確。") from None
        if not 1 <= src <= page_count:
            raise PlanError(f"頁碼超出範圍：{src}")
        if src in seen:
            raise PlanError("每一頁最多只能出現一次。")
        if rot not in _ROTATIONS:
            raise PlanError("旋轉角度只能是 0、90、180、270。")
        seen.add(src)
        out.append((src, rot))
    return out


def arrange(src_path: str, plan: Any, out_path: str) -> dict[str, Any]:
    """Writes a new PDF holding the planned pages in the planned order. Never overwrites
    ``src_path`` (the caller checks) and leaves no half-written file behind."""
    with pikepdf.open(src_path) as pdf:
        n = len(pdf.pages)
        steps = validate_plan(plan, n)
        keep = [s for s, _ in steps]
        deleted = [i for i in range(1, n + 1) if i not in set(keep)]

        pages = list(pdf.pages)
        for page in pages:
            _materialize(page.obj)  # geometry must not depend on a /Pages node we move away from
        dead = {pages[i - 1].obj.objgen for i in deleted}

        if dead:
            _drop_document_links(pdf)
            for s in keep:
                _drop_links_to(pages[s - 1].obj, dead)

        kept = [pages[s - 1] for s in keep]
        del pdf.pages[:]
        for page in kept:
            pdf.pages.append(page)
        for page, (_, rot) in zip(pdf.pages, steps):
            if rot:
                page.rotate(rot, relative=True)

        if dead:
            _drop_form_fields_on(pdf, dead)
            _scrub(pdf, dead)

        tmp = out_path + ".tmp"
        try:
            # compress_streams=True would make qpdf rewrite untouched streams (it strips
            # ASCII85, re-deflates), so nothing is recompressed.
            pdf.save(
                tmp,
                compress_streams=False,
                stream_decode_level=pikepdf.StreamDecodeLevel.none,
                object_stream_mode=pikepdf.ObjectStreamMode.preserve,
            )
            os.replace(tmp, out_path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return {"deleted": deleted, "pageCount": len(keep)}


# ---- helpers --------------------------------------------------------------------------


def _indirect(v) -> bool:
    # pikepdf hands scalars (ints, bools, floats) back as plain Python values
    return isinstance(v, pikepdf.Object) and v.is_indirect


def _kind(v):
    return v._type_code if isinstance(v, pikepdf.Object) else None


def _materialize(page_obj) -> None:
    """Writes inherited page attributes (/Rotate, /MediaBox, ...) onto the page itself."""
    for key in _INHERITABLE:
        if key not in page_obj:
            val = _inherited(page_obj, key)
            if val is not None:
                page_obj[key] = val


def _drop_document_links(pdf) -> None:
    """Things at document level that point at pages by reference. When pages go away,
    bookmarks and named destinations are dropped rather than left pointing at nothing
    (and keeping the deleted pages alive in the file)."""
    root = pdf.Root
    for key in ("/Outlines", "/OpenAction", "/StructTreeRoot", "/Threads"):
        if key in root:
            del root[key]
    names = root.get("/Names")
    if names is not None and "/Dests" in names:
        del names["/Dests"]
    if "/Dests" in root:
        del root["/Dests"]


def _dest_page(annot):
    """The page object a link annotation jumps to, or None (named and external targets)."""
    dest = annot.get("/Dest")
    if dest is None:
        action = annot.get("/A")
        if action is not None and action.get("/S") == "/GoTo":
            dest = action.get("/D")
    if _kind(dest) == pikepdf.ObjectType.array and len(dest) > 0:
        first = dest[0]
        if _indirect(first):
            return first
    return None


def _drop_links_to(page_obj, dead: set) -> None:
    annots = page_obj.get("/Annots")
    if annots is None:
        return
    i = 0
    while i < len(annots):
        a = annots[i]
        try:
            target = _dest_page(a) if a.get("/Subtype") == "/Link" else None
        except (AttributeError, TypeError):
            target = None
        if target is not None and target.objgen in dead:
            del annots[i]
        else:
            i += 1


def _drop_form_fields_on(pdf, dead: set) -> None:
    form = pdf.Root.get("/AcroForm")
    fields = form.get("/Fields") if form is not None else None
    if fields is None:
        return
    i = 0
    while i < len(fields):
        p = fields[i].get("/P")
        if _indirect(p) and p.objgen in dead:
            del fields[i]
        else:
            i += 1


def _scrub(pdf, dead: set) -> None:
    """Last line of defence: removes every remaining reference to a deleted page, so that
    nothing but the page tree could have kept it in the file."""
    T = pikepdf.ObjectType

    def is_dead(v) -> bool:
        return _indirect(v) and v.objgen in dead

    def container(v) -> bool:
        return not _indirect(v) and _kind(v) in (T.array, T.dictionary)

    def clean(c) -> None:
        t = c._type_code
        if t == T.array:
            i = 0
            while i < len(c):
                v = c[i]
                if is_dead(v):
                    del c[i]
                    continue
                if container(v):
                    clean(v)
                i += 1
        elif t in (T.dictionary, T.stream):
            for k in list(c.keys()):
                v = c[k]
                if is_dead(v):
                    del c[k]
                elif container(v):
                    clean(v)

    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Object):
            continue
        if obj.is_indirect and obj.objgen in dead:
            continue
        if obj._type_code in (T.array, T.dictionary, T.stream):
            clean(obj)
