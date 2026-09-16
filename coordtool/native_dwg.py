"""Convert a private DWG copy with an installed AutoCAD Core Console.

The input is never passed to AutoCAD directly. A constant-name copy and a
constant ASCII command script run inside an isolated working directory; no
desktop COM document or security setting is changed. Validated DXF output is
cached by source content, conversion recipe and converter installation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

import ezdxf

from .launcher import _registered_autocad


RECIPE = "autocad-coreconsole-dxf2018-precision16-v1"
SCRIPT = "FILEDIA\n0\n_.DXFOUT\nnative_output.dxf\n_Version\n2018\n16\n_.QUIT\n_Yes\n"
_conversion_lock = threading.Lock()


class NativeDwgError(ValueError):
    """The native conversion is unavailable, failed or could not be verified."""


def find_core_console() -> Path | None:
    """Find the existing AutoCAD console beside its registered desktop binary."""
    autocad = _registered_autocad()
    if autocad:
        candidate = Path(autocad).parent / "accoreconsole.exe"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path(tempfile.gettempdir())) / "CoordinateTool" / "dwg-cache-v1"


def _converter_id(executable: Path) -> str:
    info = executable.stat()
    value = f"{str(executable).casefold()}|{info.st_size}|{info.st_mtime_ns}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_cached(output: Path, metadata_path: Path, identity: dict) -> bool:
    """Check content hashes, not just the presence of a plausible DXF filename."""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if any(metadata.get(k) != v for k, v in identity.items()):
            return False
        return (output.is_file() and metadata.get("dxf_bytes") == output.stat().st_size
                and metadata.get("dxf_sha256") == _sha256(output)
                and metadata.get("audit_errors") == 0 and metadata.get("audit_fixes") == 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _run_console(executable: Path, work: Path, timeout: float) -> tuple[int, Path]:
    """Start only our Core Console process, hidden and disconnected from GUI."""
    isolation = work / "profile"
    isolation.mkdir()
    script = work / "native_convert.scr"
    script.write_text(SCRIPT, encoding="ascii", newline="\n")
    log_path = work / "native_console.log"
    args = [str(executable), "/i", str(work / "native_input.dwg"),
            "/s", str(script), "/readonly", "/isolate",
            "CoordToolNative_" + work.name.replace("-", "_"), str(isolation)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                args, cwd=str(work), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, shell=False,
                creationflags=flags,
            )
        except OSError as exc:
            raise NativeDwgError(f"AutoCAD 后台转换程序无法启动：{exc}") from exc
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Do not use taskkill /IM or touch another AutoCAD process. Only the
            # process we created is terminated; the user's CAD window stays up.
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise NativeDwgError(f"AutoCAD 后台转换超过 {timeout:g} 秒，已停止本次转换。") from exc
    return code, log_path


def _validate_output(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size < 64:
        raise NativeDwgError("AutoCAD 未生成有效 DXF 文件，可能存在文件损坏或授权提示。")
    try:
        doc = ezdxf.readfile(path)
        audit = doc.audit()
    except (OSError, ezdxf.DXFError, UnicodeError) as exc:
        raise NativeDwgError(f"原生转换结果无法回读：{exc}") from exc
    if audit.has_errors or audit.has_fixes:
        raise NativeDwgError(
            f"原生转换结果需修复（{len(audit.errors)} 项错误、{len(audit.fixes)} 项修复），"
            "未作为完整底图载入，请在 CAD 中检查后另存 DXF。"
        )
    return {"dxf_version": doc.dxfversion, "modelspace_entities": len(doc.modelspace()),
            "blocks": len(doc.blocks), "layouts": doc.layouts.names(),
            "units": doc.units, "audit_errors": 0, "audit_fixes": 0,
            "dxf_bytes": path.stat().st_size, "dxf_sha256": _sha256(path)}


def _checked_work_directory(folder, root: Path) -> Path:
    """Accept only the new single child, including Windows MSIX aliases."""
    logical = Path(folder)
    try:
        work = logical.resolve()
        valid = (logical.name.startswith("native-") and len(logical.name) > 7
                 and work.name == logical.name and work.is_dir()
                 and logical.parent.samefile(root) and work.parent.samefile(root)
                 and not work.samefile(root))
    except OSError as exc:
        raise NativeDwgError("转换临时目录身份无法确认。") from exc
    if not valid:
        raise NativeDwgError("转换临时目录校验失败。")
    return work


def convert_native_dwg(path, *, cache_dir=None, timeout: float = 120,
                       executable=None) -> Path:
    """Return a validated cached DXF converted from a read-only source DWG.

    ``cache_dir`` and ``executable`` support deployments and isolated tests. The
    default executable is discovered through AutoCAD's existing COM registry
    entry. No installation, file association, drawing or security policy is
    changed. External references and embedded objects remain in the DXF; a
    previewer may still have display limitations for those entity types.
    """
    source = Path(path).expanduser().resolve()
    if source.suffix.casefold() != ".dwg" or not source.is_file():
        raise NativeDwgError("请选择已存在的 DWG 文件。")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("转换超时必须是大于零的有限秒数。")
    console = Path(executable).resolve() if executable is not None else find_core_console()
    if console is None or not console.is_file() or console.name.casefold() != "accoreconsole.exe":
        raise NativeDwgError("未找到已安装的 AutoCAD Core Console。请在 CAD 中另存 DXF 后打开。")
    root = (Path(cache_dir) if cache_dir is not None else _cache_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Windows packaged-app filesystem redirection can resolve a previously
    # nonexistent LocalAppData directory differently after its first creation.
    root = root.resolve()
    with _conversion_lock:
        source_hash = _sha256(source)
        identity = {"recipe": RECIPE, "source_sha256": source_hash,
                    "converter_id": _converter_id(console)}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("ascii")).hexdigest()
        output, metadata_path = root / (key + ".dxf"), root / (key + ".json")
        if _read_cached(output, metadata_path, identity):
            return output
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="native-", dir=root) as folder:
            # Validate the exact recursive-cleanup target before the temporary
            # directory context can remove it. MSIX may give its parent another
            # path spelling; filesystem identity remains the same.
            work = _checked_work_directory(folder, root)
            copied = work / "native_input.dwg"
            shutil.copyfile(source, copied)
            if _sha256(copied) != source_hash:
                raise NativeDwgError("复制期间源图发生变化，请保存完成后重新打开。")
            try:
                code, log_path = _run_console(console, work, float(timeout))
                if code != 0:
                    raise NativeDwgError(f"AutoCAD 后台转换未正常完成（退出码 {code}）。")
                temporary_output = work / "native_output.dxf"
                metadata = {**identity, **_validate_output(temporary_output),
                            "conversion_seconds": round(time.monotonic() - start, 3)}
                if _sha256(source) != source_hash:
                    raise NativeDwgError("转换期间源图发生变化，请保存完成后重新打开。")
                # Publish only after a full DXF read/audit and source recheck.
                temporary_metadata = work / "native_metadata.json"
                temporary_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary_output, output)
                os.replace(temporary_metadata, metadata_path)
                shutil.copyfile(log_path, root / (key + ".log"))
            except (NativeDwgError, OSError) as exc:
                log_path = work / "native_console.log"
                diagnostic = root / (key + ".failure.log")
                detail = ""
                if log_path.is_file():
                    try:
                        shutil.copyfile(log_path, diagnostic)
                        detail = f" 转换日志：{diagnostic}"
                    except OSError:
                        pass
                raise NativeDwgError(f"{exc}{detail}") from exc
        return output
