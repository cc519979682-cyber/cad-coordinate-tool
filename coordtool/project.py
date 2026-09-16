"""Versioned, validated project snapshots without executing input content."""
import json
import math
import os
from pathlib import Path
import tempfile
from .core import Point, Placement, Settings, _validate_point, validate_settings


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def load_project(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("坐标项目文件格式损坏，无法读取。") from exc
    if not isinstance(payload, dict) or payload.get("format") != "coordtool-project" or payload.get("version") != 1:
        raise ValueError("不是支持的坐标项目文件。")
    if not isinstance(payload.get("settings"), dict) or not isinstance(payload.get("points"), list):
        raise ValueError("项目缺少有效的坐标列表或标注设置。")
    try:
        settings = Settings(**payload["settings"])
    except (TypeError, ValueError) as exc:
        raise ValueError("项目中的标注设置格式无效。") from exc
    validate_settings(settings)
    if settings.scale not in (1., 100., 1000.) or settings.color not in range(1, 8):
        raise ValueError("项目中的图纸单位或颜色不受支持。")
    points = []
    capture_ids = set()
    for i, row in enumerate(payload["points"], 1):
        if not isinstance(row, dict) or not all(key in row for key in ("name", "e", "n")):
            raise ValueError(f"项目第 {i} 个点缺少点名或坐标。")
        name = row["name"]
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError(f"项目第 {i} 个点的点名无效。")
        try:
            if isinstance(row["e"], bool) or isinstance(row["n"], bool):
                raise ValueError
            e, n = float(row["e"]), float(row["n"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"项目第 {i} 个点的坐标无效。") from exc
        if not math.isfinite(e) or not math.isfinite(n):
            raise ValueError(f"项目第 {i} 个点的坐标无效。")
        z = row.get("z")
        if z is not None:
            try:
                if isinstance(z, bool):
                    raise ValueError
                z = float(z)
                if not math.isfinite(z):
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"项目第 {i} 个点的高程无效。") from exc
        placement = row.get("placement")
        try:
            if placement is not None:
                if not isinstance(placement, dict):
                    raise ValueError("标注位置必须是对象")
                placement = Placement(**placement)
            point = Point(name, e, n, z, placement,
                          row.get("capture_id", ""), row.get("group_id", ""))
            _validate_point(point)
            if point.capture_id:
                if point.capture_id in capture_ids:
                    raise ValueError("取点标识重复")
                capture_ids.add(point.capture_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"项目第 {i} 个点的取点或标注数据无效：{exc}") from exc
        points.append(point)
    drawing = payload.get("drawing")
    if drawing is not None:
        if not isinstance(drawing, str) or not drawing.strip() or "\x00" in drawing:
            raise ValueError("项目中的底图路径无效。")
        drawing_path = Path(drawing)
        if not drawing_path.is_absolute():
            drawing_path = path.parent/drawing_path
        drawing = str(drawing_path.resolve())
    return points, settings, drawing
