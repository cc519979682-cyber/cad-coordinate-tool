"""Open exported drawings using registered AutoCAD without changing associations.

Only an existing executable path is taken from the COM registration. Registry
command-line switches are discarded and never passed to a shell.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import re
import subprocess

try:
    import winreg
except ImportError:  # Allows import on systems where CAD launch is unavailable.
    winreg = None


class CadLaunchError(OSError):
    """An exported drawing could not be opened using the local CAD install."""


def _executable_from_command(command: str) -> str | None:
    """Extract a quoted or unquoted absolute .exe path, excluding all switches."""
    if not isinstance(command, str) or any(c in command for c in "\x00\r\n"):
        return None
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        match = re.match(r'^"([^"\r\n]+\.exe)"(?:\s|$)', command, re.IGNORECASE)
    else:
        # LocalServer32 commonly stores an unquoted path containing spaces.
        # The executable extension, rather than whitespace, is the delimiter.
        match = re.match(r'^([^"\r\n]+?\.exe)(?=\s|$)', command, re.IGNORECASE)
    if not match:
        return None
    executable = match.group(1)
    if not ntpath.isabs(executable):
        return None
    if not Path(executable).is_file():
        return None
    return executable


def _registered_autocad() -> str | None:
    """Read the AutoCAD COM server in both registry views; do not activate COM."""
    if winreg is None:
        return None
    views = (getattr(winreg, "KEY_WOW64_64KEY", 0),
             getattr(winreg, "KEY_WOW64_32KEY", 0))
    for view in dict.fromkeys(views):
        try:
            access = winreg.KEY_READ | view
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"AutoCAD.Application\CLSID", 0, access) as key:
                clsid = winreg.QueryValueEx(key, None)[0]
            if not isinstance(clsid, str) or not re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", clsid):
                continue
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32", 0, access) as key:
                command = winreg.QueryValueEx(key, None)[0]
            executable = _executable_from_command(command)
            if executable:
                return executable
        except (OSError, ValueError, TypeError):
            # Missing registration, access restrictions or a stale install are
            # ordinary reasons to try the other view or the file association.
            continue
    return None


def open_in_cad(path) -> str:
    """Request a visible CAD window and return the selected launch method.

    Returns ``"AutoCAD"`` when the registered application is launched directly,
    or ``"系统默认应用"`` when Windows file associations are used. Successful
    process dispatch does not assert that CAD has finished loading the drawing.
    """
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in (".dxf", ".dwg"):
        raise CadLaunchError("请选择 DXF 或 DWG 图纸文件。")
    if not source.is_file():
        raise CadLaunchError(f"图纸文件不存在：{source}")
    if os.name != "nt":
        raise CadLaunchError("自动打开 CAD 图纸仅适用于 Windows。")
    executable = _registered_autocad()
    if executable:
        try:
            subprocess.Popen([executable, str(source)], shell=False)
        except OSError as exc:
            raise CadLaunchError(
                f"无法启动已安装的 AutoCAD：{exc}。图纸已保留，请在 CAD 中手动打开：{source}"
            ) from exc
        return "AutoCAD"
    try:
        os.startfile(str(source))
    except OSError as exc:
        raise CadLaunchError(
            f"未找到可直接启动的 AutoCAD，系统也无法打开图纸：{exc}。请在 CAD 中手动打开：{source}"
        ) from exc
    return "系统默认应用"
