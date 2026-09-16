"""Recoverable CAD picking, including attachment to already open drawings.

COM references live only inside ``launch`` on its worker thread. Journal replay
and recovery publication are transactional: a bad batch cannot partially update
the coordinate table or its saved snapshot.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid

from .core import Placement, Point, Settings, _validate_point, validate_settings
from .project import atomic_json
from .picker_bridge import ZBBridge


class PickerError(ValueError):
    """A picking session cannot continue safely; its files remain recoverable."""


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _integer(value, label, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PickerError(f"{label}必须是大于等于 {minimum} 的整数。")
    return value


def _number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PickerError(f"{label}必须是有限数字。")
    if not math.isfinite(value) or (positive and value <= 0):
        raise PickerError(f"{label}必须是{'大于零的' if positive else ''}有限数字。")
    return float(value)


def _road_name(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 100
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise PickerError("道路名称不能为空、不能有首尾空格或控制字符，最多 100 个字符。")
    return value


def _numbered_road(name):
    """Read only a final ASCII numeric suffix; hyphens inside road names stay."""
    road, separator, suffix = name.rpartition("-")
    if not separator or not road or not re.fullmatch(r"[0-9]+", suffix):
        return None
    try:
        _road_name(road)
    except PickerError:
        return None
    # A CAD index cannot exceed signed 32-bit. A larger imported suffix is not
    # a usable picking series, so do not invent a wrapped continuation index.
    digits = suffix.lstrip("0") or "0"
    if len(digits) > 10 or int(digits) >= 2147483647:
        return road, 2147483647
    return road, int(digits)


def _road_numbers(points, road, start):
    names, next_numbers = {}, {}
    for point in points:
        numbered = _numbered_road(point.name)
        if numbered:
            name, number = numbered
            key = name.casefold()
            names.setdefault(key, name)
            next_numbers[key] = max(next_numbers.get(key, 1), number + 1)
    key = road.casefold()
    names.setdefault(key, road)
    next_numbers[key] = max(next_numbers.get(key, 1), start)
    return {names[key]: value for key, value in next_numbers.items()}


def _path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _hresult(exc):
    value = getattr(exc, "hresult", None)
    if value is None and exc.args and isinstance(exc.args[0], int):
        value = exc.args[0]
    return value & 0xFFFFFFFF if isinstance(value, int) else None


def _read_com(call, *, timeout=5.0):
    """Retry transient read proxies and rejected reads, never a mutation.

    AutoCAD's dynamic dispatch proxies can temporarily lack FullName and other
    readable properties while activating or opening a document. Keep retries
    bounded so a permanently missing member still reports the original error.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        try:
            return call()
        except Exception as exc:
            retryable = isinstance(exc, AttributeError) or _hresult(exc) in (0x80010001, 0x8001010A)
            remaining = deadline - time.monotonic()
            if not retryable or remaining <= 0:
                raise
            attempt += 1
            time.sleep(min(0.1 * attempt, 0.5, remaining))


def _com_modules():
    import pythoncom
    import win32com.client
    return pythoncom, win32com.client


def _attachment_target(target):
    if not isinstance(target, dict):
        raise PickerError("请先选择要取点的 CAD 图纸。")
    result = dict(target)
    name = result.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PickerError("CAD 图纸名称无效，请重新选择。")
    path = result.get("path")
    if path is not None:
        if not isinstance(path, (str, Path)) or not str(path):
            raise PickerError("CAD 图纸路径无效。")
        result["path"] = str(Path(path).expanduser().resolve())
    if "doc_hwnd" in result:
        for key in ("app_hwnd", "doc_hwnd"):
            if isinstance(result.get(key), bool) or not isinstance(result.get(key), int) or result[key] <= 0:
                raise PickerError("CAD 图纸窗口标识无效，请重新选择。")
    elif not path or Path(path).suffix.lower() not in (".dwg", ".dxf") or not Path(path).is_file():
        raise PickerError("请选择已存在的 DWG 或 DXF 图纸。")
    return result


def _copy_primary_objects(result):
    """Normalize late/early-bound COM return conventions without counting IDPairs.

    AutoCAD's generated pywin32 wrapper returns ``(RetVal, IDPairs)`` because
    IDPairs is an output argument. Late-bound wrappers may return RetVal alone.
    A direct array containing exactly two entities must remain a two-item array.
    """
    if not isinstance(result, (tuple, list)):
        raise PickerError("CAD 旧坐标复制返回值无效。")
    if len(result) == 2 and isinstance(result[0], (tuple, list)):
        if result[1] is not None and not isinstance(result[1], (tuple, list)):
            raise PickerError("CAD 旧坐标复制映射返回值无效。")
        return tuple(result[0])
    if any(isinstance(item, (tuple, list)) for item in result):
        raise PickerError("CAD 旧坐标复制实体数组格式无效。")
    return tuple(result)


class PickerSession:
    OPEN_TIMEOUT = 90.0
    APP_READY_TIMEOUT = 90.0
    READY_TIMEOUT = 10.0
    ZB_IDLE_TIMEOUT = 3.0

    def __init__(self, directory, session_id, original_path, drawing_path,
                 road, start, settings, snapshot_settings=None):
        self.directory = Path(directory)
        self.session_id = session_id
        self.original_path = original_path
        self.drawing_path = Path(drawing_path)
        self.road, self.start, self.settings = road, start, settings
        self.initial_road, self.initial_start = road, start
        self.road_numbers = {road: start}
        self.snapshot_settings = snapshot_settings if snapshot_settings is not None else settings
        self.event_path = self.directory / "events.tsv"
        self.stop_path = self.directory / "stop.request"
        self.lsp_path = self.directory / "picker.lsp"
        self.recovery_path = self.directory / "recovery.coordproj"
        self.seed_path = None
        self.seed_entity_count = 0
        self.seed_layer = "COORD_SEED_" + session_id
        self.ready = False
        self.done = False
        self.last_error = ""
        self.launch_stage = "未启动"
        self.warnings = []
        self._offset = 0
        self._partial = b""
        self._seen = {}
        self._active = []
        self._undone = set()
        self._last_serial = 0
        self._segments = {0: {"road": road, "start": start}}
        self._seen_segments = {}
        self._current_segment = 0
        self._lock = threading.RLock()
        self._launch_attempted = False
        self.resume_parent = None
        self.native_prefix = ""
        self.wait_for_idle = False
        self.zb_bridge = ZBBridge(self.directory, self.session_id)
        self.attached_target = None

    @property
    def current_road(self):
        return self.road

    @property
    def current_segment(self):
        return self._current_segment

    @property
    def active_count(self):
        """Retained captures across all roads, rather than only the undo stack."""
        return len(self._seen) - len(self._undone)

    def publish_zb_ready(self):
        with self._lock:
            if self.ready and self.done and not self.last_error and self.native_prefix:
                self.zb_bridge.publish()
            else:
                self.zb_bridge.disable()

    def disable_zb(self, reason=""):
        with self._lock:
            self.zb_bridge.disable()

    def take_zb_request(self):
        with self._lock:
            if not self.ready or not self.done or self.last_error:
                self.zb_bridge.disable()
                return None
            try:
                return self.zb_bridge.take_request()
            except ValueError as exc:
                raise PickerError(str(exc)) from exc

    def next_road_start(self, road, points):
        """ZB always asks for a road; number from retained coordinates only."""
        _road_name(road)
        self._validate_points(points)
        start = max((value[1] + 1 for point in points
                     if (value := _numbered_road(point.name)) is not None
                     and value[0].casefold() == road.casefold()), default=1)
        if start > 2147483647:
            raise PickerError("道路编号已达到 CAD 上限，请使用新的道路名称。")
        return start

    @classmethod
    def attach(cls, target, road, start, settings, root=None, *,
               initial_points=None, snapshot_settings=None):
        """Prepare only a journal; native launch uses the selected drawing itself."""
        return cls.create(None, road, start, settings, root=root,
                          initial_points=initial_points, snapshot_settings=snapshot_settings,
                          _attach_target=_attachment_target(target))

    @classmethod
    def resume(cls, previous, road, start, settings, root=None, *,
               initial_points=None, snapshot_settings=None):
        """Start a new journal on the still-open, previously annotated drawing."""
        return cls.create(None, road, start, settings, root=root,
                          initial_points=initial_points, snapshot_settings=snapshot_settings,
                          _resume_from=previous)

    @staticmethod
    def _validate_resume(previous, points, snapshot_settings):
        from .project import load_project
        if (not isinstance(previous, PickerSession) or not previous.ready or not previous.done
                or previous.last_error or not previous.native_prefix):
            raise PickerError("上一轮取点尚未正常结束，不能续接；请先结束并确认坐标已回传。")
        prior_points, prior_settings, _ = load_project(previous.recovery_path)
        if points != prior_points or snapshot_settings != prior_settings:
            raise PickerError("坐标或设置已在上轮结束后改变，请使用 CAD 取点重新建立工作图。")

    @classmethod
    def create(cls, drawing_path, road, start, settings, root=None, *,
               initial_points=None, base_doc=None, snapshot_settings=None, _resume_from=None,
               _attach_target=None):
        """Prepare a private drawing and initial recovery before touching CAD."""
        _road_name(road)
        _integer(start, "起始编号")
        if start > 2147483647:
            raise PickerError("起始编号超出 CAD 支持范围。")
        if not isinstance(settings, Settings):
            raise PickerError("取点设置无效。")
        validate_settings(settings)
        snapshot_settings = settings if snapshot_settings is None else snapshot_settings
        if not isinstance(snapshot_settings, Settings):
            raise PickerError("项目标注设置无效。")
        validate_settings(snapshot_settings)
        pick_values, snapshot_values = asdict(settings), asdict(snapshot_settings)
        pick_values.pop("text_height")
        snapshot_values.pop("text_height")
        if pick_values != snapshot_values:
            raise PickerError("取点设置与项目设置只能有字高差异。")
        points = list(initial_points or [])
        cls._validate_points(points)
        if _resume_from is not None:
            cls._validate_resume(_resume_from, points, snapshot_settings)
        attachment = (_attachment_target(_attach_target) if _attach_target is not None else
                      dict(_resume_from.attached_target) if _resume_from is not None
                      and _resume_from.attached_target is not None else None)
        if any(p.name.casefold() == f"{road}-{start:02d}".casefold()
               or ((numbered := _numbered_road(p.name)) is not None
                   and numbered[0].casefold() == road.casefold() and numbered[1] == start)
               for p in points):
            raise PickerError(f"点名 {road}-{start:02d} 已存在，请更换道路名或起始编号。")
        source = (_resume_from.original_path if _resume_from is not None else
                  Path(drawing_path).expanduser().resolve() if drawing_path else None)
        if _resume_from is None and source and (source.suffix.casefold() not in (".dwg", ".dxf") or not source.is_file()):
            raise PickerError("请选择已存在的 DWG 或 DXF 底图。")
        base = Path(root) if root is not None else (
            Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
            / "CoordinateGeneratorV22" / "picks")
        base = base.expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        base = base.resolve()
        session_id = uuid.uuid4().hex
        directory_name = datetime.now().strftime("%Y%m%d-%H%M%S_") + session_id
        directory = base / directory_name
        directory.mkdir()
        directory = directory.resolve()
        # Windows packaged-app redirection can resolve a new child to its
        # physical LocalCache path while its existing parent retains the
        # logical LocalAppData spelling. Compare filesystem identity instead
        # of rejecting two names for the same actual parent directory.
        if directory.name != directory_name or not directory.parent.samefile(base):
            raise PickerError("取点工作目录校验失败。")
        target = (_resume_from.drawing_path if _resume_from is not None else
                  Path(attachment.get("path") or attachment["name"]) if attachment is not None else
                  directory / ("取点工作副本.dxf" if source is None
                               else "取点工作副本" + source.suffix.lower()))
        session = cls(directory, session_id, source, target, road, start, settings, snapshot_settings)
        session.resume_parent = _resume_from
        session.attached_target = attachment
        session.road_numbers = _road_numbers(points, road, start)
        try:
            source_hash = _sha256(source) if source and source.is_file() else None
            # Never serialize the background through ezdxf here: third-party
            # DWG/DXF objects can be valid in CAD but unsupported by a rewriter.
            # base_doc remains an accepted UI argument, but is not modified or
            # exported. Old labels are brought into this raw copy natively.
            if _resume_from is not None or attachment is not None:
                pass  # The native document remains open, including unsaved edits.
            elif source:
                shutil.copyfile(source, target)
                if _sha256(target) != source_hash:
                    raise PickerError("复制期间原图发生变化，请保存原图后重试。")
            else:
                import ezdxf
                from .cad import _units_for_scale
                drawing = ezdxf.new("R2018")
                drawing.units = _units_for_scale(settings.scale)
                drawing.saveas(target)
            if points and _resume_from is None and attachment is None:
                import ezdxf
                from .cad import export_dxf
                session.seed_path = directory / "annotation_seed.dxf"
                # CopyObjects merges identically named layers with the target.
                # Use a private fresh layer so a hidden/frozen original layer
                # cannot hide the labels or require modifying the background.
                seed_settings = replace(snapshot_settings, layer=session.seed_layer)
                export_dxf(session.seed_path, points, seed_settings, base_doc=None)
                session.seed_entity_count = len(ezdxf.readfile(session.seed_path).modelspace())
                if session.seed_entity_count == 0:
                    raise PickerError("已有坐标未生成有效的 CAD 标注。")
            if _resume_from is None and source and _sha256(source) != source_hash:
                raise PickerError("创建取点副本期间原图发生变化，请保存原图后重试。")
            from .picker_lisp import generate_picker_lsp
            script = generate_picker_lsp(
                session.event_path, session.stop_path, road, start,
                settings.scale, settings.offset_e, settings.offset_n,
                settings.text_height * settings.scale, road_numbers=session.road_numbers,
                bridge_dir=session.directory, bridge_token=session.session_id)
            session.native_prefix = re.search(r'\*cgp-prefix\*\s+"(CGP_[0-9A-F]+)"', script)[1]
            session.lsp_path.write_text(script, encoding="utf-8", newline="\n")
            atomic_json(directory / "session.json", {
                "format": "coordtool-picker-session", "version": 1,
                "session_id": session_id, "road": road, "start": start,
                "road_numbers": session.road_numbers,
                "settings": asdict(settings),
                "snapshot_settings": asdict(snapshot_settings),
                "original_path": str(source) if source else None,
                "original_sha256": source_hash, "drawing_path": str(target),
                "drawing_sha256": _sha256(target) if attachment is None and target.is_file() else None,
                "attached_target": attachment,
                "native_prefix": session.native_prefix,
                "resume_from": _resume_from.session_id if _resume_from is not None else None,
                "seed_path": str(session.seed_path) if session.seed_path else None,
                "seed_entity_count": session.seed_entity_count,
                "seed_layer": session.seed_layer,
            })
            session._save(points, {}, [], set(), 0, False, False, "")
        except Exception as exc:
            session.last_error = f"取点准备失败：{exc}；工作目录保留在 {directory}"
            raise PickerError(session.last_error) from exc
        return session

    @staticmethod
    def _validate_points(points):
        ids = set()
        for point in points:
            if not isinstance(point, Point):
                raise PickerError("坐标列表包含无效点。")
            _validate_point(point)
            if point.capture_id:
                if point.capture_id in ids:
                    raise PickerError("坐标列表包含重复取点标识。")
                ids.add(point.capture_id)

    def _save(self, points, seen, active, undone, serial, ready, done, error, *,
              segments=None, current_segment=None, seen_segments=None):
        atomic_json(self.recovery_path, self._snapshot(
            points, seen, active, undone, serial, ready, done, error,
            segments=segments, current_segment=current_segment, seen_segments=seen_segments))

    def _snapshot(self, points, seen, active, undone, serial, ready, done, error, *,
                  segments=None, current_segment=None, seen_segments=None):
        segments = self._segments if segments is None else segments
        current_segment = self._current_segment if current_segment is None else current_segment
        seen_segments = self._seen_segments if seen_segments is None else seen_segments
        current = segments[current_segment]
        return {
            "format": "coordtool-project", "version": 1,
            "points": [asdict(p) for p in points], "settings": asdict(self.snapshot_settings),
            # Reopen against the original background, not the already annotated
            # CAD working copy, to avoid drawing each captured label twice.
            "drawing": str(self.original_path) if self.original_path else None,
            "picker": {"session_id": self.session_id, "directory": str(self.directory),
                       "road": current["road"], "start": current["start"],
                       "initial_road": self.initial_road, "initial_start": self.initial_start,
                       "current_road": current["road"], "current_segment": current_segment,
                       "segments": {str(k): v for k, v in segments.items()},
                       "road_numbers": self.road_numbers,
                       "seen_segments": {str(k): v for k, v in seen_segments.items()},
                       "active_count": len(seen) - len(undone),
                       "seen": {str(k): asdict(v) for k, v in seen.items()},
                       "active": active, "undone": sorted(undone),
                       "last_serial": serial, "ready": ready, "done": done,
                       "last_error": error},
        }

    def poll(self):
        """Read complete UTF-8 journal lines once; retain any partial tail."""
        from .picker_lisp import parse_event
        with self._lock:
            if not self.event_path.exists():
                return []
            with self.event_path.open("rb") as stream:
                if stream.seek(0, 2) < self._offset:
                    raise PickerError("取点记录被截短，已停止读取；请检查会话工作目录。")
                stream.seek(self._offset)
                data = stream.read(1024 * 1024)
            pending = self._partial + data
            lines = pending.split(b"\n")
            partial = lines.pop()
            if len(partial) > 65536:
                raise PickerError("取点记录单行过长。")
            try:
                events = [parse_event(line.rstrip(b"\r").decode("utf-8-sig"))
                          for line in lines if line.strip()]
            except (ValueError, UnicodeError) as exc:
                self.last_error = f"取点记录无法读取：{exc}；恢复文件：{self.recovery_path}"
                self.request_stop()
                raise PickerError(self.last_error) from exc
            self._offset += len(data)
            self._partial = partial
            return events

    def _point(self, event, segments=None):
        serial = _integer(event.get("serial"), "记录流水号")
        index = _integer(event.get("index"), "点编号")
        road_id = _integer(event.get("road_id", 0), "道路段编号", minimum=0)
        segments = self._segments if segments is None else segments
        if road_id not in segments:
            raise PickerError("取点记录引用了尚未切换的道路段。")
        if index > 2147483647:
            raise PickerError("点编号超出 CAD 支持范围。")
        values = {key: _number(event.get(key), key, key in ("height", "arrow"))
                  for key in ("x", "y", "z", "lx", "ly", "hx", "height", "arrow")}
        s = self.settings
        point = Point(
            f"{segments[road_id]['road']}-{index:02d}", values["x"] / s.scale - s.offset_e,
            values["y"] / s.scale - s.offset_n, values["z"] / s.scale,
            Placement(values["lx"] / s.scale - s.offset_e,
                      values["ly"] / s.scale - s.offset_n,
                      values["hx"] / s.scale - s.offset_e,
                      values["height"] / s.scale, values["arrow"] / s.scale),
            f"{self.session_id}:{serial}", self.session_id if road_id == 0 else f"{self.session_id}:road:{road_id}")
        _validate_point(point)
        if "name" in event and event["name"] != point.name:
            raise PickerError("取点记录点名与道路编号不一致。")
        return serial, index, point, road_id

    def _next_road_index(self, road, points):
        key = road.casefold()
        baseline = max((number for name, number in self.road_numbers.items()
                        if name.casefold() == key), default=1)
        for point in points:
            numbered = _numbered_road(point.name)
            if numbered and numbered[0].casefold() == key:
                baseline = max(baseline, numbered[1] + 1)
        return baseline

    def apply(self, points, events):
        """Validate and persist an entire batch before publishing any changes."""
        with self._lock:
            result = list(points)
            self._validate_points(result)
            seen, active, undone = dict(self._seen), list(self._active), set(self._undone)
            segments = {key: dict(value) for key, value in self._segments.items()}
            seen_segments = dict(self._seen_segments)
            current_segment = self._current_segment
            serial, ready, done, error = self._last_serial, self.ready, self.done, self.last_error
            by_id = {p.capture_id: p for p in result if p.capture_id}
            for key in seen.keys() - undone:
                if by_id.get(seen[key].capture_id) != seen[key]:
                    raise PickerError("取点期间已采集坐标被修改，请结束本次取点后再编辑。")
            try:
                for event in events:
                    if not isinstance(event, dict):
                        raise PickerError("取点事件格式无效。")
                    kind = event.get("type")
                    if kind == "ADD":
                        key, index, point, road_id = self._point(event, segments)
                        if key in seen:
                            if seen[key] != point or seen_segments[key] != road_id:
                                raise PickerError("同一取点流水号包含冲突数据。")
                            continue
                        if done:
                            raise PickerError("取点结束后收到新的坐标。")
                        if (road_id != current_segment or key != serial + 1
                                or index != segments[current_segment]["start"] + len(active)):
                            raise PickerError("取点流水号或点编号不连续。")
                        if any(p.name.casefold() == point.name.casefold() for p in result):
                            raise PickerError(f"点名 {point.name} 已存在，本批取点未写入。")
                        if any(p.capture_id == point.capture_id for p in result):
                            raise PickerError("取点标识与现有坐标冲突。")
                        result.append(point)
                        seen[key] = point
                        seen_segments[key] = road_id
                        active.append(key)
                        serial = key
                    elif kind == "ROAD":
                        road_id = _integer(event.get("road_id"), "道路段编号")
                        start = _integer(event.get("start"), "道路起始编号")
                        road = _road_name(event.get("road"))
                        if road_id > 2147483647 or start > 2147483647:
                            raise PickerError("道路段或起始编号超出 CAD 支持范围。")
                        metadata = {"road": road, "start": start}
                        if road_id in segments:
                            if segments[road_id] != metadata:
                                raise PickerError("同一道路段编号包含冲突数据。")
                            continue
                        if done:
                            raise PickerError("取点结束后收到新的换路记录。")
                        if road_id != current_segment + 1:
                            raise PickerError("道路段编号不连续。")
                        if start != self._next_road_index(road, result):
                            raise PickerError("换路起始编号与已有坐标不一致。")
                        segments[road_id] = metadata
                        current_segment = road_id
                        active = []
                    elif kind == "UNDO":
                        key = _integer(event.get("serial"), "撤销流水号")
                        if key in undone:
                            continue
                        if done:
                            raise PickerError("取点结束后收到新的撤销记录。")
                        if not active or active[-1] != key:
                            raise PickerError("撤销记录与本次取点顺序不一致。")
                        target = seen[key].capture_id
                        result = [p for p in result if p.capture_id != target]
                        active.pop()
                        undone.add(key)
                    elif kind == "READY":
                        ready = True
                    elif kind == "DONE":
                        done = True
                    elif kind == "ERROR":
                        message = event.get("message", event.get("error", "CAD 取点出现错误。"))
                        if not isinstance(message, str):
                            raise PickerError("取点错误消息格式无效。")
                        error, done = message, True
                    else:
                        raise PickerError(f"未知取点事件：{kind}")
                self._validate_points(result)
                self._save(result, seen, active, undone, serial, ready, done, error,
                           segments=segments, current_segment=current_segment,
                           seen_segments=seen_segments)
            except Exception as exc:
                self.last_error = f"本批取点未写入：{exc}；恢复文件：{self.recovery_path}"
                raise PickerError(self.last_error) from exc
            self._seen, self._active, self._undone = seen, active, undone
            self._segments, self._seen_segments = segments, seen_segments
            self._current_segment = current_segment
            self.road, self.start = segments[current_segment]["road"], segments[current_segment]["start"]
            self._last_serial, self.ready, self.done, self.last_error = serial, ready, done, error
            return result

    def request_stop(self):
        """Signal our LISP loop; never cancel another CAD command or document."""
        self.stop_path.write_text("STOP\n", encoding="ascii")

    def _wait_document(self, app, opened, expected_path=None):
        """AutoCAD can return an unusable Open proxy until its UI is ready."""
        expected = _path_key(expected_path if expected_path is not None else self.drawing_path)
        deadline = time.monotonic() + self.OPEN_TIMEOUT
        while True:
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，CAD 工作副本保留。")
            candidates = [opened]
            try:
                candidates.append(_read_com(lambda: app.ActiveDocument))
                documents = _read_com(lambda: app.Documents)
                count = _read_com(lambda: documents.Count)
                for index in range(count):
                    candidates.append(_read_com(lambda i=index: documents.Item(i)))
            except Exception as exc:
                if not isinstance(exc, AttributeError) and _hresult(exc) not in (0x80010001, 0x8001010A):
                    raise
            for candidate in candidates:
                if candidate is None:
                    continue
                try:
                    if _path_key(_read_com(lambda: candidate.FullName, timeout=0)) == expected:
                        return candidate
                except Exception as exc:
                    if not isinstance(exc, (AttributeError, TypeError)) and _hresult(exc) not in (0x80010001, 0x8001010A):
                        raise
            if time.monotonic() >= deadline:
                raise PickerError(f"等待 CAD 打开本会话副本超过 {self.OPEN_TIMEOUT:g} 秒，未发送取点命令。")
            time.sleep(0.1)

    def _seed_existing_points(self, app, documents, main_doc, pythoncom, client):
        """Deep-copy annotation-only entities between our two CAD documents.

        Autodesk CopyObjects requires one source owner and the destination
        modelspace as Owner; enumerate completely before making the copy.
        """
        if self.seed_path is None:
            return
        seed_doc = seed_space = main_space = entities = copied = None
        try:
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，CAD 工作副本保留。")
            if (_path_key(_read_com(lambda: main_doc.FullName)) != _path_key(self.drawing_path)
                    or not self.seed_path.resolve().is_relative_to(self.directory.resolve())):
                raise PickerError("旧坐标复制的工作图纸路径校验失败。")
            self.launch_stage = "打开旧坐标辅助图"
            seed_doc = documents.Open(str(self.seed_path), True)
            self.launch_stage = "等待旧坐标辅助图"
            seed_doc = self._wait_document(app, seed_doc, self.seed_path)
            seed_space = _read_com(lambda: seed_doc.ModelSpace)
            main_space = _read_com(lambda: main_doc.ModelSpace)
            count = _read_com(lambda: seed_space.Count)
            if count != self.seed_entity_count or count <= 0:
                raise PickerError("CAD 读取的旧坐标标注数量不完整，已停止取点。")
            entities = tuple(_read_com(lambda i=index: seed_space.Item(i)) for index in range(count))
            before = _read_com(lambda: main_space.Count)
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，CAD 工作副本保留。")
            if (_path_key(_read_com(lambda: seed_doc.FullName)) != _path_key(self.seed_path)
                    or _path_key(_read_com(lambda: main_doc.FullName)) != _path_key(self.drawing_path)):
                raise PickerError("旧坐标复制前图纸路径发生变化，已停止取点。")
            objects = client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, entities)
            # This mutation is issued once. An uncertain COM response must not
            # duplicate the annotations by replaying CopyObjects.
            self.launch_stage = "复制旧坐标到主副本"
            copied = seed_doc.CopyObjects(objects, main_space)
            self.launch_stage = "校验旧坐标复制结果"
            after = _read_com(lambda: main_space.Count)
            primary = _copy_primary_objects(copied)
            if after != before + count or len(primary) != count:
                raise PickerError("CAD 旧坐标复制数量校验失败，工作副本已保留。")
            if _path_key(_read_com(lambda: seed_doc.FullName)) != _path_key(self.seed_path):
                raise PickerError("旧坐标辅助图路径发生变化，未关闭该窗口。")
            self.launch_stage = "关闭旧坐标辅助图"
            seed_doc.Close(False)
            seed_doc = None
            self.launch_stage = "激活取点主副本"
            main_doc.Activate()
            self.launch_stage = "确认主副本活动状态"
            if _path_key(_read_com(lambda: app.ActiveDocument.FullName)) != _path_key(self.drawing_path):
                raise PickerError("CAD 未返回取点工作副本，未启动取点。")
        finally:
            # On failure leave owned documents open for inspection. Never close
            # a document unless copying was verified and its own path matched.
            seed_doc = seed_space = main_space = entities = copied = None

    def _wait_ready(self):
        """A successful SendCommand is not proof that AutoLISP ran correctly."""
        deadline = time.monotonic() + self.READY_TIMEOUT
        while True:
            if self.event_path.exists():
                # Do not consume records here: the UI applies READY and any
                # points together via the ordinary transactional journal path.
                with self.event_path.open("rb") as stream:
                    data = stream.read(65536)
                lines = data.split(b"\n")[:-1]
                if any(line.rstrip(b"\r") == b"READY" for line in lines):
                    return
                failures = [line for line in lines if line.startswith(b"ERROR\t")]
                if failures:
                    from .picker_lisp import parse_event
                    message = parse_event(failures[0].decode("ascii"))["message"]
                    raise PickerError("CAD 取点初始化失败：" + message)
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，CAD 工作副本保留。")
            if time.monotonic() >= deadline:
                raise PickerError(f"CAD 未在 {self.READY_TIMEOUT:g} 秒内确认取点就绪，请检查 CAD 命令行。")
            time.sleep(0.1)

    def _wait_new_application(self, app):
        """A newly created CAD instance may still be initializing its UI."""
        deadline = time.monotonic() + self.APP_READY_TIMEOUT
        while True:
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，已启动的 CAD 窗口保留。")
            try:
                state = _read_com(lambda: app.GetAcadState())
                if _read_com(lambda: state.IsQuiescent):
                    documents = _read_com(lambda: app.Documents)
                    count = _read_com(lambda: documents.Count)
                    if count == 0:
                        return state, documents
                    active_doc = _read_com(lambda: app.ActiveDocument)
                    if _read_com(lambda: active_doc.GetVariable("CMDACTIVE")) == 0:
                        return state, documents
            except Exception as exc:
                if not isinstance(exc, AttributeError) and _hresult(exc) not in (0x80010001, 0x8001010A):
                    raise
            if time.monotonic() >= deadline:
                raise PickerError(f"等待新启动的 AutoCAD 就绪超过 {self.APP_READY_TIMEOUT:g} 秒，请检查 CAD 启动提示。")
            time.sleep(0.1)

    def _resume_document(self, documents):
        """Find the previous native layer, even after Save As, without opening files."""
        previous = self.resume_parent
        prefix = previous.native_prefix
        matches = []
        for index in range(_read_com(lambda: documents.Count)):
            candidate = _read_com(lambda i=index: documents.Item(i))
            layers = _read_com(lambda: candidate.Layers)
            layer_name = None
            try:
                layer_name = _read_com(lambda: layers.Item(prefix).Name)
            except Exception:
                # cgp-begin adds a suffix only on the unlikely name collision.
                for number in range(_read_com(lambda: layers.Count)):
                    name = str(_read_com(lambda i=number: layers.Item(i).Name))
                    if re.fullmatch(re.escape(prefix) + r"(?:_[1-9][0-9]*)?", name):
                        layer_name = name
                        break
            if layer_name is None:
                continue
            groups = _read_com(lambda: candidate.Groups)
            for serial in previous._seen.keys() - previous._undone:
                try:
                    count = _read_com(lambda s=serial: groups.Item(f"{layer_name}_{s}").Count)
                except Exception as exc:
                    raise PickerError("上一轮 CAD 标注已缺失，请使用 CAD 取点重新建立工作图。") from exc
                if count != 6:
                    raise PickerError("上一轮 CAD 标注已改变，请使用 CAD 取点重新建立工作图。")
            matches.append(candidate)
        if len(matches) != 1:
            raise PickerError("未能唯一找到仍打开的上一轮取点图，请使用 CAD 取点重新开始。")
        doc = matches[0]
        if _read_com(lambda: doc.GetVariable("CMDACTIVE")) != 0:
            raise PickerError("上一轮取点图仍有未结束的 CAD 命令，请先结束该命令。")
        # Store primitive identity only; COM proxies stay on this worker thread.
        self.drawing_path = Path(_read_com(lambda: doc.FullName)).resolve()
        metadata_path = self.directory / "session.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["drawing_path"] = str(self.drawing_path)
        atomic_json(metadata_path, metadata)
        return doc

    def _selected_document(self, app, documents):
        """Resolve the explicit native tab, or open the one user-selected file."""
        target = self.attached_target
        if target.get("doc_hwnd"):
            from .cad_documents import find_open_document
            return find_open_document(app, target)
        path = Path(target["path"])
        matches = []
        for index in range(_read_com(lambda: documents.Count)):
            candidate = _read_com(lambda i=index: documents.Item(i))
            if (_read_com(lambda: candidate.Path)
                    and _path_key(_read_com(lambda: candidate.FullName)) == _path_key(path)):
                matches.append(candidate)
        if len(matches) > 1:
            raise PickerError("同一路径对应多张 CAD 图纸，请重新选择具体图纸。")
        if matches:
            return matches[0]
        if not path.is_file():
            raise PickerError("所选图纸已不存在，请重新选择。")
        try:
            opener = documents.Open
        except AttributeError:
            # Empty AutoCAD can expose a late-bound collection without method
            # metadata. Mark a documented method before issuing it once.
            documents._FlagAsMethod("Open")
            opener = documents.Open
        opened = opener(str(path), False)
        return self._wait_document(app, opened, path)

    def _record_attached_document(self, app, doc):
        target = dict(self.attached_target)
        target.update(app_hwnd=int(_read_com(lambda: app.HWND)),
                      doc_hwnd=int(_read_com(lambda: doc.HWND)),
                      name=str(_read_com(lambda: doc.Name)))
        target["path"] = str(_read_com(lambda: doc.FullName)) if _read_com(lambda: doc.Path) else None
        unit_id = int(_read_com(lambda: doc.GetVariable("INSUNITS")))
        expected_scale = {6: 1., 4: 1000., 5: 100.}.get(unit_id)
        if expected_scale is not None and expected_scale != self.settings.scale:
            raise PickerError("所选 CAD 图纸单位与取点设置不同，请重新选择图纸并核对米/毫米/厘米。")
        target["units"] = unit_id
        self.attached_target = target
        self.drawing_path = Path(target["path"] or target["name"])
        metadata_path = self.directory / "session.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(attached_target=target, drawing_path=str(self.drawing_path))
        atomic_json(metadata_path, metadata)

    def _active_document_matches(self, app):
        doc = _read_com(lambda: app.ActiveDocument)
        if self.attached_target is None:
            return _path_key(_read_com(lambda: doc.FullName)) == _path_key(self.drawing_path)
        target = self.attached_target
        if (int(_read_com(lambda: doc.HWND)) != target["doc_hwnd"]
                or str(_read_com(lambda: doc.Name)).casefold() != target["name"].casefold()):
            return False
        path = str(_read_com(lambda: doc.FullName)) if _read_com(lambda: doc.Path) else None
        return (_path_key(path) == _path_key(target["path"])) if path and target["path"] else path == target["path"]

    def launch(self):
        """Attach or open the chosen drawing and run picking on this worker."""
        with self._lock:
            if self._launch_attempted:
                raise PickerError("本会话已经尝试启动；请检查 CAD 或创建新的取点会话。")
            self._launch_attempted = True
        initialized = False
        app = doc = documents = state = active_doc = None
        pythoncom = None
        try:
            self.launch_stage = "初始化 COM"
            from .picker_lisp import picker_commands
            pythoncom, client = _com_modules()
            pythoncom.CoInitialize()
            initialized = True
            created_app = False
            try:
                self.launch_stage = "连接已有 AutoCAD"
                app = _read_com(lambda: client.GetActiveObject("AutoCAD.Application"))
            except Exception as exc:
                if _hresult(exc) != 0x800401E3:  # MK_E_UNAVAILABLE: not running
                    raise
                if self.resume_parent is not None or (self.attached_target and self.attached_target.get("doc_hwnd")):
                    raise PickerError("原 AutoCAD 已关闭，不能同图续接；请使用 CAD 取点重新开始。") from exc
                self.launch_stage = "启动 AutoCAD"
                app = client.DispatchEx("AutoCAD.Application")
                created_app = True
            if (self.attached_target and self.attached_target.get("app_hwnd")
                    and int(_read_com(lambda: app.HWND)) != self.attached_target["app_hwnd"]):
                raise PickerError("当前连接的 AutoCAD 与所选图纸不一致，请重新选择图纸。")
            if created_app:
                self.launch_stage = "等待 AutoCAD 就绪"
                state, documents = self._wait_new_application(app)
            else:
                self.launch_stage = "检查现有 AutoCAD 是否空闲"
                state = _read_com(lambda: app.GetAcadState())
                if self.wait_for_idle and self.resume_parent is not None:
                    # ZB writes its request just before returning to the command
                    # prompt. Wait only for that handoff; never cancel commands.
                    deadline = time.monotonic() + self.ZB_IDLE_TIMEOUT
                    while not _read_com(lambda: state.IsQuiescent):
                        if self.stop_path.exists():
                            raise PickerError("本次 ZB 取点已取消。")
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.1)
                if not _read_com(lambda: state.IsQuiescent):
                    raise PickerError("AutoCAD 当前图纸有未结束的命令，请先结束后重新取点。")
                documents = _read_com(lambda: app.Documents)
                if _read_com(lambda: documents.Count) > 0:
                    active_doc = _read_com(lambda: app.ActiveDocument)
                    if _read_com(lambda: active_doc.GetVariable("CMDACTIVE")) != 0:
                        raise PickerError("AutoCAD 当前图纸有未结束的命令，请先结束后重新取点。")
                    active_doc = None
            if self.stop_path.exists():
                raise PickerError("本次取点已取消。")
            app.Visible = True
            # Open/Activate/SendCommand must not be retried: a COM failure can
            # mean the action happened but its reply was lost.
            if self.resume_parent is not None:
                self.launch_stage = "查找仍打开的上一轮取点图"
                doc = self._resume_document(documents)
            elif self.attached_target is not None:
                self.launch_stage = "连接所选 CAD 图纸"
                doc = self._selected_document(app, documents)
            else:
                self.launch_stage = "打开取点主副本"
                doc = documents.Open(str(self.drawing_path), False)
                self.launch_stage = "等待取点主副本"
                doc = self._wait_document(app, doc)
                self._seed_existing_points(app, documents, doc, pythoncom, client)
            if self.attached_target is not None:
                self._record_attached_document(app, doc)
            self.launch_stage = "激活取点图纸"
            doc.Activate()
            self.launch_stage = "确认取点图纸活动状态"
            if not self._active_document_matches(app):
                raise PickerError("CAD 活动图纸发生变化，未发送取点命令。")
            if self.stop_path.exists():
                raise PickerError("本次取点已取消，CAD 工作副本保留。")
            source = self.lsp_path.read_text(encoding="utf-8")
            commands = picker_commands(source, auto_start=True)
            for index, command in enumerate(commands):
                self.launch_stage = f"发送取点程序 {index + 1}/{len(commands)}"
                if self.stop_path.exists():
                    raise PickerError("本次取点已取消，CAD 工作副本保留。")
                if not self._active_document_matches(app):
                    raise PickerError("CAD 活动图纸发生变化，已停止发送本次取点程序。")
                if index == len(commands) - 1 and _read_com(lambda: doc.GetVariable("CMDACTIVE")) != 0:
                    raise PickerError("CAD 当前有未结束的命令，未启动取点。")
                doc.SendCommand(command)
            self.launch_stage = "等待取点就绪回执"
            self._wait_ready()
            self.launch_stage = "取点已就绪"
        except Exception as exc:
            log_path = self.directory / "launch_error.log"
            try:
                log_path.write_text(
                    f"{datetime.now().isoformat(timespec='seconds')}\n阶段：{self.launch_stage}\n"
                    + traceback.format_exc(), encoding="utf-8")
                diagnostic = f"诊断日志：{log_path}。"
            except OSError:
                diagnostic = "诊断日志未能写入。"
            try:
                self.request_stop()
            except OSError:
                pass  # Preserve the original COM failure and recovery path.
            self.last_error = (f"CAD 取点未能确认启动（{self.launch_stage}）：{exc}。不会自动重复发送命令；"
                               f"请检查 CAD 当前状态。{diagnostic}恢复文件：{self.recovery_path}")
            raise PickerError(self.last_error) from exc
        finally:
            active_doc = state = documents = doc = app = None
            if initialized:
                pythoncom.CoUninitialize()


def recover_picker_snapshot(path, *, warnings=None):
    """Read a validated snapshot plus its own journal, without publishing files.

    The snapshot must equal a complete journal prefix. Replaying from the
    initial imported points, rather than appending ADDs to a snapshot, also
    preserves tail UNDO/ROAD records and makes repeated recovery idempotent.
    """
    from .project import load_project
    from .picker_lisp import parse_event

    path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        points, snapshot_settings, drawing = load_project(path)
        if "picker" not in payload:
            return points, snapshot_settings, drawing
        metadata = payload["picker"]
        if not isinstance(metadata, dict):
            raise PickerError("恢复文件的取点会话信息无效。")
        directory = path.parent
        # Only read siblings of the selected snapshot. Never follow a path in
        # an imported document to another session's journal.
        declared = metadata.get("directory")
        if (not isinstance(declared, str) or not Path(declared).is_absolute()
                or not Path(declared).samefile(directory)):
            raise PickerError("恢复文件与取点会话目录不一致。")
        manifest_path = directory / "session.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        session_id = metadata.get("session_id")
        if (not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id)
                or not isinstance(manifest, dict)
                or manifest.get("format") != "coordtool-picker-session"
                or manifest.get("version") != 1 or manifest.get("session_id") != session_id):
            raise PickerError("恢复文件与原取点会话标识不一致。")
        settings = Settings(**manifest["settings"])
        validate_settings(settings)
        if asdict(snapshot_settings) != manifest.get("snapshot_settings"):
            raise PickerError("恢复文件的单位或标注设置与原取点会话不一致。")
        pick_values, snapshot_values = asdict(settings), asdict(snapshot_settings)
        pick_values.pop("text_height")
        snapshot_values.pop("text_height")
        if pick_values != snapshot_values:
            raise PickerError("原取点会话的坐标转换与项目设置不一致。")
        road = _road_name(manifest.get("road"))
        start = _integer(manifest.get("start"), "道路起始编号")
        if start > 2147483647:
            raise PickerError("道路起始编号超出 CAD 支持范围。")
        numbers = manifest.get("road_numbers")
        if not isinstance(numbers, dict):
            raise PickerError("恢复文件缺少道路编号基线。")
        for name, number in numbers.items():
            _road_name(name)
            if _integer(number, "道路编号基线") > 2147483648:
                raise PickerError("道路编号基线无效。")

        class ReplaySession(PickerSession):
            def _save(self, *args, **kwargs):
                self.replayed_snapshot = self._snapshot(*args, **kwargs)

        original = manifest.get("original_path")
        replay = ReplaySession(directory, session_id, Path(original) if original else None,
                               manifest["drawing_path"], road, start, settings, snapshot_settings)
        replay.road_numbers = numbers
        # Capture IDs from earlier sessions are imported coordinates and stay.
        initial = [point for point in points if not point.capture_id.startswith(session_id + ":")]
        replay._validate_points(initial)
        result = initial
        replay._save(result, {}, [], set(), 0, False, False, "")

        def matches_snapshot():
            candidate = replay.replayed_snapshot
            # A packaged Windows app may spell the same directory through its
            # redirected LocalAppData alias; samefile above already checked it.
            candidate["picker"]["directory"] = declared
            return (candidate["picker"] == metadata
                    and candidate["points"] == payload["points"]
                    and candidate["settings"] == payload["settings"]
                    and candidate["drawing"] == payload.get("drawing"))

        matched = matches_snapshot()
        event_path = directory / "events.tsv"
        if event_path.exists() and not event_path.resolve().parent.samefile(directory):
            raise PickerError("取点日志指向其他会话目录，已停止恢复。")
        data = event_path.read_bytes() if event_path.exists() else b""
        lines = data.split(b"\n")
        tail = lines.pop()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            if len(line) > 65536:
                raise PickerError(f"取点日志第 {number} 行过长。")
            try:
                event = parse_event(line.rstrip(b"\r").decode("utf-8-sig"))
                result = replay.apply(result, [event])
            except (ValueError, UnicodeError) as exc:
                raise PickerError(f"取点日志第 {number} 行损坏：{exc}") from exc
            matched = matched or matches_snapshot()
        if not matched:
            raise PickerError("恢复快照与完整取点日志不一致；未减少或替换已有坐标，请保留该会话目录。")
        if tail and warnings is not None:
            warnings.append("日志最后一行未写完，已恢复此前所有完整记录；未写完的残片及原始文件均保留。")
        return result, snapshot_settings, drawing
    except PickerError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PickerError(f"取点恢复失败，原文件未改动：{exc}") from exc
