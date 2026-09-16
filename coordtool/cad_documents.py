"""Read-only identities for drawings already open in an existing AutoCAD.

``list_open_drawings`` owns COM initialization on its calling worker thread and
returns primitive dictionaries only. ``find_open_document`` uses the caller's
existing COM apartment and returns one proxy for use on that same thread.
Neither function opens, activates, saves, changes, or closes any CAD document.
"""
from __future__ import annotations

import ntpath
from pathlib import Path


def _path_identity(path):
    if path is None:
        return None
    # CAD may retain an 8.3 path while the session stores its resolved spelling.
    # Resolve before comparing so both names identify the same saved drawing.
    try:
        path = str(Path(path).resolve())
    except (OSError, RuntimeError, ValueError):
        # Preserve the conservative textual comparison if the path cannot be
        # resolved, e.g. an unavailable external drive. No file is opened here.
        pass
    return ntpath.normcase(ntpath.normpath(path))


def _identity(document, read):
    """Read no FullName for an unsaved drawing: it may only contain its name."""
    hwnd = int(read(lambda: document.HWND))
    name = str(read(lambda: document.Name))
    directory = read(lambda: document.Path)
    path = str(read(lambda: document.FullName)) if directory else None
    return hwnd, name, path


def _same_identity(first, second):
    return (first[0] == second[0] and first[1].casefold() == second[1].casefold()
            and _path_identity(first[2]) == _path_identity(second[2]))


def list_open_drawings():
    """List an existing CAD's drawings, active first, without starting CAD.

    Call from a background worker. A missing running AutoCAD returns an empty
    list; connection and read errors remain visible to the caller.
    """
    # Deliberately local: PickerSession also uses this module during launch.
    from .picker import PickerError, _com_modules, _hresult, _read_com

    pythoncom, client = _com_modules()
    initialized = False
    app = documents = active = document = None
    try:
        pythoncom.CoInitialize()
        initialized = True
        try:
            app = _read_com(lambda: client.GetActiveObject("AutoCAD.Application"))
        except Exception as exc:
            if _hresult(exc) == 0x800401E3:  # MK_E_UNAVAILABLE only
                return []
            raise
        app_hwnd = int(_read_com(lambda: app.HWND))
        documents = _read_com(lambda: app.Documents)
        count = int(_read_com(lambda: documents.Count))
        if count == 0:
            return []
        active = _read_com(lambda: app.ActiveDocument)
        active_identity = _identity(active, _read_com)
        active = None
        result = []
        for index in range(count):
            document = _read_com(lambda index=index: documents.Item(index))
            identity = _identity(document, _read_com)
            units = int(_read_com(lambda: document.GetVariable("INSUNITS")))
            result.append({"app_hwnd": app_hwnd, "doc_hwnd": identity[0],
                           "name": identity[1], "path": identity[2], "units": units,
                           "active": _same_identity(identity, active_identity)})
            document = None
        if sum(row["active"] for row in result) != 1:
            raise PickerError("CAD 图纸列表正在变化，无法唯一识别当前图纸，请刷新后重试。")
        return sorted(result, key=lambda row: not row["active"])
    finally:
        # Release every temporary proxy before leaving the caller's apartment.
        document = active = documents = app = None
        if initialized:
            pythoncom.CoUninitialize()


def find_open_document(app, target):
    """Return the exact previously selected document; never reopen by path.

    Some MDI configurations share document HWND values. Name and saved path are
    therefore checked as well, even when only one HWND match currently remains.
    If the selection was renamed/saved elsewhere, refresh the drawing list.
    The caller owns COM initialization and must retain the proxy on this thread.
    """
    from .picker import PickerError, _read_com

    if (not isinstance(target, dict)
            or any(isinstance(target.get(key), bool) or not isinstance(target.get(key), int)
                   or target[key] == 0 for key in ("app_hwnd", "doc_hwnd"))
            or not isinstance(target.get("name"), str) or not target["name"]
            or "path" not in target
            or (target["path"] is not None and not isinstance(target["path"], str))):
        raise PickerError("CAD 图纸选择信息无效，请刷新图纸列表后重试。")
    documents = document = None
    matches = []
    try:
        if int(_read_com(lambda: app.HWND)) != target["app_hwnd"]:
            raise PickerError("原先选择的 AutoCAD 窗口已关闭或已更换，请重新选择图纸。")
        documents = _read_com(lambda: app.Documents)
        expected = target["doc_hwnd"], target["name"], target["path"]
        for index in range(int(_read_com(lambda: documents.Count))):
            document = _read_com(lambda index=index: documents.Item(index))
            # Avoid reading unrelated documents' paths when HWND already differs.
            if int(_read_com(lambda: document.HWND)) != target["doc_hwnd"]:
                document = None
                continue
            if _same_identity(_identity(document, _read_com), expected):
                matches.append(document)
            document = None
        if len(matches) != 1:
            raise PickerError("所选 CAD 图纸已关闭、改名或无法唯一识别，请刷新列表重新选择。")
        return matches[0]
    except PickerError:
        raise
    except Exception as exc:
        raise PickerError(f"无法读取所选 CAD 图纸，图纸可能已关闭，请刷新后重试：{exc}") from exc
    finally:
        document = documents = None
        matches.clear()
