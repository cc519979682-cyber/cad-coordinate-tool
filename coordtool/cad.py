"""Read drawing backgrounds and create independent, editable CAD annotations.

DXF is the loss-preserving exchange path. The optional ezdwg reader is only a
simple-drawing convenience; its limitation is always returned to the caller.
Nothing in this module connects to or changes an already-open CAD document.
"""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import tempfile
import uuid

import ezdxf
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf.const import VALID_DXF_LINEWEIGHTS
from ezdxf.lldxf.validator import is_valid_layer_name
from ezdxf.units import METER_FACTOR

from .core import (calc_labels, annotation_segments, annotation_texts,
                   connection_groups, _validate_point, validate_settings)
from .native_dwg import find_core_console, convert_native_dwg


class DrawingError(ValueError):
    """A drawing could not be read or safely exported."""


def _validate(points, settings):
    validate_settings(settings)
    layer = settings.layer
    if not isinstance(layer, str) or not layer.strip() or len(layer) > 255:
        raise ValueError("图层名不能为空，且不能超过 255 个字符。")
    if not is_valid_layer_name(layer) or any(ord(c) < 32 for c in layer):
        raise ValueError('图层名不能含控制字符或 <>/\\":;?*|= 等字符。')
    if not 1 <= int(settings.color) <= 255:
        raise ValueError("CAD 颜色应为 1–255 的整数。")
    if settings.lineweight not in VALID_DXF_LINEWEIGHTS or settings.lineweight < 0:
        raise ValueError("请选择有效 CAD 线宽（单位为 0.01 毫米）。")
    for key in ("radius", "leader", "text_height", "scale"):
        value = getattr(settings, key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} 必须是大于零的有限数值。")
    for key in ("offset_e", "offset_n"):
        if not math.isfinite(getattr(settings, key)):
            raise ValueError("坐标偏移量必须是有限数值。")
    if not points:
        raise ValueError("请先导入坐标点。")
    for point in points:
        _validate_point(point)
        if point.placement is not None and point.z is not None and not math.isfinite(point.z*settings.scale):
            raise ValueError("转换后的标注高程必须是有限数值。")
        if not point.name.strip() or any(ord(c) < 32 for c in point.name):
            raise ValueError("点名不能为空或包含换行等控制字符。")
        if not math.isfinite(point.e) or not math.isfinite(point.n):
            raise ValueError("坐标必须是有限数值。")


def _audit_read(doc: Drawing, warnings: list[str]):
    auditor = doc.audit()
    if auditor.has_errors:
        raise DrawingError(f"图纸结构校验未通过（{len(auditor.errors)} 项错误）。请在 CAD 中另存为 DXF 后重试。")
    if auditor.has_fixes:
        warnings.append(f"读取副本已修复 {len(auditor.fixes)} 项 DXF 结构问题；原文件未修改。")
    if any(block.block.is_xref for block in doc.blocks):
        warnings.append("底图含外部参照：预览无法保证显示未绑定的参照内容，请先在 CAD 中绑定后另存 DXF。")
    if doc.modelspace().query("ACAD_PROXY_ENTITY IMAGE PDFUNDERLAY DWFUNDERLAY DGNUNDERLAY"):
        warnings.append("底图含代理实体、图片或参照底图，软件中的简图预览可能不显示这些内容。")


def load_drawing(path) -> tuple[Drawing, list[str]]:
    """Read DXF or a DWG conversion into memory; never write to the source.

    Callers must display all returned warnings. DWG prefers the local AutoCAD
    Core Console, then ODA. The strict ezdwg fallback is explicitly labelled
    approximate, including when that parser reports zero skipped entities.
    """
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    warnings: list[str] = []
    if suffix == ".dxf":
        try:
            doc = ezdxf.readfile(source)
        except (ezdxf.DXFError, UnicodeError) as exc:
            raise DrawingError(f"DXF 读取失败：{exc}。请在 CAD 中另存为 DXF 后重试。") from exc
        quality = "dxf"
    elif suffix == ".dwg":
        from ezdxf.addons import odafc
        if find_core_console() is not None:
            try:
                converted = convert_native_dwg(source)
                doc = ezdxf.readfile(converted)
            except Exception as exc:
                raise DrawingError(f"AutoCAD 原生读取未完成：{exc}") from exc
            quality = "native_autocad"
        elif odafc.is_installed():
            try:
                doc = odafc.readfile(source, audit=False)
            except Exception as exc:
                raise DrawingError(f"ODA 无法转换此 DWG：{exc}。请在 CAD 中另存为 DXF。") from exc
            if doc is None:
                raise DrawingError("ODA 未返回有效图纸。请在 CAD 中另存为 DXF。")
            quality = "oda"
            warnings.append("DWG 已通过本机 ODA 转为内存中的 DXF 副本；原文件未修改。")
        else:
            try:
                import ezdwg
            except ImportError as exc:
                raise DrawingError("当前环境未安装 DWG 转换器。请在 CAD 中另存为 DXF，再打开该 DXF。") from exc
            try:
                with tempfile.TemporaryDirectory(prefix="coord-dwg-") as folder:
                    target = Path(folder) / "background.dxf"
                    result = ezdwg.to_dxf(
                        str(source), str(target), strict=True,
                        include_unsupported=True, modelspace_only=False,
                    )
                    if result.skipped_entities:
                        raise ValueError(f"有 {result.skipped_entities} 个实体未能转换：{result.skipped_by_type}")
                    doc = ezdxf.readfile(target)
                    if not len(doc.modelspace()):
                        raise ValueError("未读到模型空间图形")
                    # The fallback converter creates a new document with metre
                    # defaults; those defaults are not the source DWG's units.
                    try:
                        source_units = int(ezdwg.read(str(source)).insunits or 0)
                        doc.units = source_units if 0 <= source_units <= 24 else 0
                    except Exception:
                        doc.units = 0
                        warnings.append("简图读取未能确认原始图纸单位，请手动核对；不会自动按米判断。")
            except Exception as exc:
                raise DrawingError(f"DWG 简图读取失败，无法确认图形完整：{exc}。请在 CAD 中另存为 DXF 后打开。") from exc
            finally:
                ezdwg.clear_decode_caches()
            quality = "simplified_dwg"
            warnings.append(
                "当前为 DWG 简图读取（ezdwg），不保证复杂块、字体、尺寸、布局及自定义对象完整。"
                "正式底图请在 CAD 中另存为 DXF 后打开；导出会以当前读到的简图为底图，不能视为原 DWG 的完整副本。"
            )
    else:
        raise DrawingError("底图支持 .dxf 或 .dwg 文件。")
    _audit_read(doc, warnings)
    doc._coordtool_source = str(source)
    doc._coordtool_quality = quality
    doc._coordtool_warnings = list(warnings)
    return doc, warnings


def _units_for_scale(scale: float) -> int:
    # Point inputs and annotation dimensions are metres; scale is CAD units/m.
    for factor, code in ((1.0, 6), (1000.0, 4), (100.0, 5), (10.0, 14),
                         (0.001, 7), (1 / 0.0254, 1), (1 / 0.3048, 2)):
        if math.isclose(scale, factor, rel_tol=1e-10):
            return code
    return 0  # An arbitrary multiplier has no unambiguous physical unit.


def _ensure_layer(doc, settings):
    if settings.layer not in doc.layers:
        doc.layers.new(settings.layer, dxfattribs={
            "color": int(settings.color), "lineweight": settings.lineweight,
        })
    else:
        layer = doc.layers.get(settings.layer)
        if layer.is_off() or layer.is_frozen() or layer.is_locked():
            raise DrawingError("标注图层在底图中已关闭、冻结或锁定，请换一个新图层名。")


def _relocate_references(doc: Drawing, source) -> list[tuple[str, str, str]]:
    """Keep file dependencies attached when the drawing moves to a new folder.

    Only the export copy is changed. Missing or ambiguous file locations are
    rejected instead of relying on another CAD installation's search paths.
    """
    references = [(block.block, "xref_path") for block in doc.blocks
                  if block.block.is_xref]
    references.extend((entity, "filename") for entity in doc.objects
                      if entity.dxftype() in {"IMAGEDEF", "PDFDEFINITION",
                                             "DWFDEFINITION", "DGNDEFINITION"})
    original_folder = Path(source).resolve().parent if source else None
    expected = []
    for entity, attribute in references:
        raw = entity.dxf.get(attribute, "")
        if not isinstance(raw, str) or not raw.strip():
            raise DrawingError(f"底图 {entity.dxftype()} 的外部参照未指定文件，未导出。")
        path = Path(raw)
        if not path.is_absolute():
            if original_folder is None:
                raise DrawingError(f"底图来源不明，无法确认外部参照的相对路径：{raw}；未导出。")
            path = original_folder / path
        try:
            path = path.resolve(strict=True)
            if not path.is_file():
                raise OSError("不是文件")
        except (OSError, ValueError) as exc:
            raise DrawingError(
                f"底图外部参照文件无法读取：{raw}。请在 CAD 修复参照路径后导出；目标文件未修改。"
            ) from exc
        value = str(path)
        setattr(entity.dxf, attribute, value)
        expected.append((entity.dxf.handle, attribute, value))
    return expected


def _check_references(doc: Drawing, references):
    for handle, attribute, filename in references:
        entity = doc.entitydb.get(handle)
        try:
            valid = (entity is not None and entity.dxf.is_supported(attribute)
                     and entity.dxf.get(attribute) == filename and Path(filename).is_file())
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise DrawingError(f"导出副本的外部参照校验失败：{filename}；目标文件未修改。")


def export_dxf(path, points, settings, base_doc: Drawing | None = None) -> Path:
    """Save an independently editable copy and validate it before replacement."""
    points = list(points)
    _validate(points, settings)
    destination = Path(path).resolve()
    if destination.suffix.lower() != ".dxf":
        raise ValueError("导出路径必须以 .dxf 结尾。")
    references = []
    if base_doc is not None:
        source = getattr(base_doc, "_coordtool_source", None) or base_doc.filename
        if source and os.path.normcase(str(Path(source).resolve())) == os.path.normcase(str(destination)):
            raise DrawingError("请另存为新文件，不能覆盖当前底图原文件。")
        unit = base_doc.units
        expected_scale = METER_FACTOR[unit] if 0 < unit < len(METER_FACTOR) else None
        if expected_scale and not math.isclose(settings.scale, expected_scale, rel_tol=1e-8):
            raise DrawingError(f"CAD 比例与底图声明的单位不一致：该底图每米应为 {expected_scale:g} CAD 单位，请先调整比例。")
        doc = copy.deepcopy(base_doc)
        references = _relocate_references(doc, source)
        # R12 cannot store actual lineweights. ezdxf upgrades its internal R12
        # entities when saved at the modern version; the source stays untouched.
        if doc.dxfversion < "AC1015":
            doc.dxfversion = "R2010"
    else:
        doc = ezdxf.new("R2010", units=_units_for_scale(settings.scale))
    _ensure_layer(doc, settings)
    style = "COORD_TEXT_" + uuid.uuid4().hex[:12]
    doc.styles.new(style, dxfattribs={"font": "simhei.ttf"})
    msp = doc.modelspace()
    attrs = {"layer": settings.layer, "color": int(settings.color),
             "lineweight": settings.lineweight}
    labels = calc_labels(points, settings)
    if settings.draw_line:
        for group in connection_groups(points, labels):
            if len(group) > 1:
                msp.add_lwpolyline([(p.x, p.y) for p in group],
                                   close=bool(settings.closed and len(group) >= 3), dxfattribs=attrs)
    radius, height = settings.radius * settings.scale, settings.text_height * settings.scale
    batch = uuid.uuid4().hex
    for i, label in enumerate(labels):
        block_name = f"COORD_{batch}_{i + 1}"
        while block_name in doc.blocks:
            block_name += "_1"
        block = doc.blocks.new(block_name, base_point=(0, 0, 0))
        if label.arrow is None:
            block.add_circle((0, 0), radius, dxfattribs=attrs)
        for start, end in annotation_segments(label, settings):
            block.add_line((start[0]-label.x, start[1]-label.y),
                           (end[0]-label.x, end[1]-label.y), dxfattribs=attrs)
        for value, x, y, text_height, alignment in annotation_texts(label, settings):
            text = block.add_text(value, dxfattribs={
                **attrs, "height": text_height, "style": style,
            })
            text.set_placement((x-label.x, y-label.y),
                               align=TextEntityAlignment.RIGHT if alignment == "right" else TextEntityAlignment.LEFT)
        source_point = points[i]
        z = source_point.z*settings.scale if source_point.placement is not None and source_point.z is not None else 0.
        msp.add_blockref(block_name, (label.x, label.y, z), dxfattribs=attrs)
    doc.header["$LWDISPLAY"] = True
    if base_doc is None:
        xs, ys = [], []
        for p in labels:
            for line in annotation_segments(p, settings):
                for x, y in line:
                    xs.append(x)
                    ys.append(y)
            for value, x, y, th, alignment in annotation_texts(p, settings):
                width = max(th, sum(2 if ord(char) > 127 else 1 for char in value)*th*.8)
                xs.extend((x, x+width*(1 if alignment == "left" else -1)))
                ys.extend((y, y+th))
        size = max(max(xs) - min(xs), max(ys) - min(ys), height * 5)
        doc.set_modelspace_vport(height=size * 1.2,
                                center=((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2))
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".coord-", suffix=".dxf", dir=destination.parent)
    os.close(handle)
    try:
        doc.saveas(temp_name)
        check = ezdxf.readfile(temp_name)
        auditor = check.audit()
        if auditor.has_errors or auditor.has_fixes:
            raise DrawingError("导出副本回读校验未通过，未写入目标文件。")
        _check_references(check, references)
        written = [p for p in check.modelspace().query("INSERT") if p.dxf.name.startswith(f"COORD_{batch}_")]
        if len(written) != len(points):
            raise DrawingError("标注数量校验未通过，未写入目标文件。")
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def _lsp_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("坐标计算溢出，请检查缩放及偏移量。")
    return format(value, ".15g")


def generate_lsp(points, settings) -> str:
    """Generate APPLOAD-safe BINDLINE; it draws only when explicitly invoked."""
    points = list(points)
    _validate(points, settings)
    labels = calc_labels(points, settings)
    layer = _lsp_string(settings.layer)
    attrs = f"(cons 8 {layer}) '(62 . {int(settings.color)}) '(370 . {settings.lineweight})"
    radius, height = settings.radius * settings.scale, settings.text_height * settings.scale
    rows = [
        "; 坐标生成器 v22 / APPLOAD 后输入 BINDLINE 绘制。坐标为 WCS。",
        "; 每次绘制为一个撤销组；此文件加载时不修改图纸。",
        "(vl-load-com)",
        "(defun C:BINDLINE (/ *error* oldcmd oldlayer doc undoopen blockopen bn k style layerdata)",
        "  (if (= (getvar \"CVPORT\") 1)",
        "    (princ \"\\n请先进入模型空间再绘制坐标标注；布局空间未修改。\")",
        "    (progn",
        "  (setq oldcmd (getvar \"CMDECHO\") oldlayer (getvar \"CLAYER\"))",
        "  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))",
        "  (defun *error* (msg)",
        "    (if blockopen (progn (entmake '((0 . \"ENDBLK\"))) (setq blockopen nil)))",
        "    (if undoopen (progn (vla-EndUndoMark doc) (setq undoopen nil)))",
        "    (setvar \"CMDECHO\" oldcmd) (setvar \"CLAYER\" oldlayer)",
        "    (if msg (princ (strcat \"\\n绘制中断: \" msg \"；可用 U 撤销本次绘制。\"))) (princ))",
        f"  (setq layerdata (tblsearch \"LAYER\" {layer}))",
        "  (if (and layerdata (or (< (cdr (assoc 62 layerdata)) 0)",
        "        (/= 0 (logand 5 (cdr (assoc 70 layerdata))))))",
        "    (progn (princ \"\\n目标图层已关闭、冻结或锁定，请修改图层名称。\") (exit)))",
        "  (vla-StartUndoMark doc) (setq undoopen T) (setvar \"CMDECHO\" 0)",
        f"  (if (not layerdata) (entmake (list '(0 . \"LAYER\") (cons 2 {layer}) '(70 . 0) '(6 . \"Continuous\") '(62 . {int(settings.color)}) '(370 . {settings.lineweight}))))",
        f"  (setq style {_lsp_string('COORD_TEXT_' + uuid.uuid4().hex[:12])})",
        "  (if (not (entmake (list '(0 . \"STYLE\") '(100 . \"AcDbSymbolTableRecord\") '(100 . \"AcDbTextStyleTableRecord\") (cons 2 style) '(70 . 0) '(40 . 0.0) '(41 . 1.0) '(50 . 0.0) '(71 . 0) '(42 . 2.5) '(3 . \"simhei.ttf\") '(4 . \"\"))))",
        "    (progn (princ \"\\n无法创建标注文字样式，已停止绘制。\") (exit)))",
    ]
    def point(x, y, z=0.):
        return f"(list {_number(x)} {_number(y)} {_number(z) if z else '0.0'})"
    def entity(kind, extra):
        rows.append(f"  (if (not (entmake (list '(0 . \"{kind}\") {attrs} {extra}))) (exit))")
    if settings.draw_line:
        for group in connection_groups(points, labels):
            pairs = list(zip(group, group[1:]))
            if settings.closed and len(group) >= 3:
                pairs.append((group[-1], group[0]))
            for a, b in pairs:
                entity("LINE", f"(cons 10 {point(a.x, a.y)}) (cons 11 {point(b.x, b.y)})")
    prefix = "COORD_" + uuid.uuid4().hex[:16]
    for i, p in enumerate(labels):
        rows.extend([
            f"  (setq bn {_lsp_string(prefix + '_' + str(i + 1))} k 0)",
            f"  (while (tblsearch \"BLOCK\" bn) (setq k (1+ k) bn (strcat {_lsp_string(prefix + '_' + str(i + 1) + '_')} (itoa k))))",
            "  (if (not (entmake (list '(0 . \"BLOCK\") (cons 2 bn) '(70 . 0) '(10 0.0 0.0 0.0)))) (exit))",
            "  (setq blockopen T)",
        ])
        if p.arrow is None:
            entity("CIRCLE", f"'(10 0.0 0.0 0.0) (cons 40 {_number(radius)})")
        for start, end in annotation_segments(p, settings):
            entity("LINE", f"(cons 10 {point(start[0]-p.x, start[1]-p.y)}) (cons 11 {point(end[0]-p.x, end[1]-p.y)})")
        for value, x, y, text_height, alignment in annotation_texts(p, settings):
            anchor = point(x-p.x, y-p.y)
            entity("TEXT", f"(cons 10 {anchor}) (cons 11 {anchor}) (cons 40 {_number(text_height)}) (cons 1 {_lsp_string(value)}) (cons 7 style) '(50 . 0.0) '(72 . {2 if alignment == 'right' else 0}) '(73 . 0)")
        rows.append("  (if (not (entmake '((0 . \"ENDBLK\")))) (exit))")
        rows.append("  (setq blockopen nil)")
        source_point = points[i]
        z = source_point.z*settings.scale if source_point.placement is not None and source_point.z is not None else 0.
        entity("INSERT", f"(cons 2 bn) (cons 10 {point(p.x, p.y, z)}) '(41 . 1.0) '(42 . 1.0) '(43 . 1.0) '(50 . 0.0)")
    rows.extend([
        "  (vla-EndUndoMark doc) (setq undoopen nil)",
        "  (setvar \"CMDECHO\" oldcmd) (setvar \"CLAYER\" oldlayer)",
        "  (vla-Regen doc 1)",
        f"  (princ \"\\n已生成 {len(points)} 个独立坐标标注块，可用 U 撤销本次绘制。\") (princ))))",
        '(princ "\\n坐标工具已加载。输入 BINDLINE 绘制坐标标注。") (princ)',
    ])
    return "\n".join(rows) + "\n"


def export_lsp(path, points, settings) -> Path:
    target = Path(path)
    target.write_text(generate_lsp(points, settings), encoding="utf-8", newline="\n")
    return target


def generate_reverse_lsp(layer: str = "COORD_POINTS") -> str:
    """Read world coordinates of direct annotation blocks, including transforms.

    Circle OCS -> block coordinates -> subtract block base -> scale -> rotation
    -> insertion OCS -> WCS -> user-confirmed metres. Ambiguous
    multi-circle/multi-label blocks and MINSERT arrays are reported as skipped.
    """
    if not layer.strip() or not is_valid_layer_name(layer) or any(ord(c) < 32 for c in layer):
        raise ValueError("图层名无效。")
    code = r'''; 坐标生成器 v22 / APPLOAD 后输入 EXPORTCOORD。
; 输出当前图纸 WCS 东/北坐标（米），运行时确认每米 CAD 单位数，不撤销原偏移。
(vl-load-com)
(defun V22:val (code data fallback / pair)
  (if (setq pair (assoc code data)) (cdr pair) fallback))
(defun V22:world (pt base ins ent / dx dy dz a co si)
  (setq dx (* (- (car pt) (car base)) (V22:val 41 ins 1.0))
        dy (* (- (cadr pt) (cadr base)) (V22:val 42 ins 1.0))
        dz (* (- (caddr pt) (caddr base)) (V22:val 43 ins 1.0))
        a (V22:val 50 ins 0.0) co (cos a) si (sin a))
  (trans (mapcar '+ (cdr (assoc 10 ins))
         (list (- (* dx co) (* dy si)) (+ (* dx si) (* dy co)) dz)) ent 0))
(defun V22:csv (s / i ch out)
  (setq i 1 out "\"")
  (repeat (strlen s)
    (setq ch (substr s i 1) i (1+ i)
          out (strcat out (if (= ch "\"") "\"\"" ch))))
  (strcat out "\""))
(defun C:EXPORTCOORD (/ *error* olddim lyr ss i ent ed bdata bent bed typ units factor suggested
                        cp label nc nt world row rows seen skipped fname fp)
  (setq olddim (getvar "DIMZIN"))
  (defun *error* (msg)
    (if fp (progn (close fp) (setq fp nil)))
    (setvar "DIMZIN" olddim)
    (if msg (princ (strcat "\n导出中断: " msg))) (princ))
  (setq lyr (getstring T (strcat "\n图层名 [默认 " __LAYER__ "]: ")))
  (if (= lyr "") (setq lyr __LAYER__))
  (setq units (getvar "INSUNITS")
        suggested (cond ((= units 6) 1.0) ((= units 4) 1000.0) ((= units 5) 100.0)))
  (if suggested
    (progn
      (initget 6)
      (setq factor (getreal (strcat "\n每米对应多少 CAD 单位 [图纸默认 " (rtos suggested 2 0) "]: ")))
      (if (not factor) (setq factor suggested)))
    (progn
      (princ "\n图纸未声明米/毫米/厘米单位，请明确输入换算比例。")
      (initget 7)
      (setq factor (getreal "\n每米对应多少 CAD 单位（米=1，毫米=1000）: "))))
  ; Compare exact layer names below: ssget layer filters interpret wildcards.
  (setq ss (ssget "_X" '((0 . "INSERT") (410 . "Model")))
        i 0 rows nil seen nil skipped 0)
  (if ss
    (repeat (sslength ss)
      (setq ent (ssname ss i) i (1+ i) ed (entget ent))
      (if (= (strcase (cdr (assoc 8 ed))) (strcase lyr))
        (progn
          (setq bdata (tblsearch "BLOCK" (cdr (assoc 2 ed)))
                bent (cdr (assoc -2 bdata)) cp nil label nil nc 0 nt 0)
          (while (and bent (/= "ENDBLK" (cdr (assoc 0 (entget bent)))))
            (setq bed (entget bent) typ (cdr (assoc 0 bed)))
            (cond
              ((= typ "CIRCLE") (setq cp (trans (cdr (assoc 10 bed)) bent 0) nc (1+ nc)))
              ((= typ "TEXT") (setq label (cdr (assoc 1 bed)) nt (1+ nt))))
            (setq bent (entnext bent)))
          (if (and (= nc 1) (= nt 1) (= (V22:val 70 ed 1) 1) (= (V22:val 71 ed 1) 1))
            (progn
              (setq world (V22:world cp (V22:val 10 bdata '(0.0 0.0 0.0)) ed ent)
                    row (list label (/ (car world) factor) (/ (cadr world) factor)))
              (if (not (vl-some '(lambda (r) (equal row r 0.000000001)) seen))
                (setq rows (cons row rows) seen (cons row seen))))
            (setq skipped (1+ skipped)))))))
  (if rows
    (progn
      (setq fname (getfiled "保存坐标 CSV（米）"
                    (strcat (getvar "DWGPREFIX") "坐标导出.csv") "csv" 1))
      (if fname
        (progn
          (setq fp (open fname "w"))
          (if fp
            (progn
              (setvar "DIMZIN" 0)
              (write-line "点名,东E,北N" fp)
              (foreach row (reverse rows)
                (write-line (strcat (V22:csv (car row)) "," (rtos (cadr row) 2 6)
                              "," (rtos (caddr row) 2 6)) fp))
              (close fp) (setq fp nil)
              (princ (strcat "\n已导出 " (itoa (length rows)) " 个坐标点：" fname)))
            (princ "\n文件无法写入，请检查路径和权限。")))))
    (princ "\n未找到每块恰含一个圆和一段文字的坐标标注。"))
  (if (> skipped 0) (princ (strcat "\n跳过 " (itoa skipped)
       " 个非标准/多重插入块；嵌套块需先在 CAD 中检查后单独处理。")))
  (setvar "DIMZIN" olddim) (princ))
(princ "\n反推工具已加载。输入 EXPORTCOORD，确认 CAD 比例后导出 WCS 坐标（米）。")
(princ)
'''
    return code.replace("__LAYER__", _lsp_string(layer))


def open_in_cad(path) -> None:
    """Ask Windows to open one exported file using its registered application."""
    source = Path(path).resolve()
    if not source.is_file() or source.suffix.lower() not in (".dxf", ".dwg"):
        raise ValueError("请选择已存在的 DXF/DWG 文件。")
    if os.name != "nt":
        raise OSError("自动打开 CAD 文件仅适用于 Windows。")
    os.startfile(str(source))
