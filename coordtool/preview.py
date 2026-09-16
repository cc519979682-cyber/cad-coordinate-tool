"""Prepare DXF primitives off the UI thread, then attach batched artists.

The supported ezdxf 1.4.3 Recorder resolves blocks, visibility, line types,
world-coordinate transforms and font outlines without a Tk canvas. Conversion
to Matplotlib Paths also happens in prepare_preview(). render_prepared() only
creates collections; it never walks DXF entities or resolves a font.

The original drawing is not simplified or rewritten. Preview limitations apply
only to the on-screen modelspace view. Images use their boundary rectangles so
loading a drawing does not synchronously open external image files.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from threading import Lock
from time import perf_counter

import numpy as np
from matplotlib.collections import PathCollection
from matplotlib.colors import to_rgba
from matplotlib.path import Path as MplPath
from matplotlib.transforms import IdentityTransform

from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, ImagePolicy
from ezdxf.addons.drawing.recorder import (
    Recorder, PointsRecord, SolidLinesRecord, PathRecord, FilledPathsRecord,
)
from ezdxf.npshapes import to_matplotlib_path
from ezdxf.fonts import fonts
from .preview_layer import PreparedBaseArtist


_PREPARE_LOCK = Lock()  # ezdxf's font caches are process-wide.
_MAX_PATHS_PER_COLLECTION = 4096


@dataclass(frozen=True)
class _PathBatch:
    paths: tuple
    facecolors: np.ndarray
    edgecolors: np.ndarray
    linewidths: np.ndarray
    bounds: np.ndarray


@dataclass(frozen=True)
class _PointBatch:
    positions: np.ndarray
    colors: np.ndarray


@dataclass(frozen=True)
class PreparedPreview:
    batches: tuple
    bounds: tuple[float, float, float, float] | None
    warnings: tuple[str, ...]
    background: str
    source_entities: int
    primitive_count: int
    path_count: int
    layers: frozenset[str]
    prepare_seconds: float


class _PreviewFrontend(Frontend):
    def __init__(self, *args, **kwargs):
        self.skipped = Counter()
        self.notices = set()
        self.font_replacements = Counter()
        self.style_fonts = {}
        self.missing_font_styles = set()
        self._cmaps = {}
        self._cjk_fallback = None
        super().__init__(*args, **kwargs)

    def skip_entity(self, entity, msg):
        if msg != "invisible":
            self.skipped[(entity.dxftype(), msg)] += 1

    def log_message(self, message):
        # The default PDSIZE=0 means a relative viewport-sized marker. The
        # dimensionless point is rendered explicitly as a fixed-screen dot.
        if message == "relative point size is not supported":
            return
        self.notices.add(str(message))

    def _cmap(self, face):
        name = face.filename.lower() if face is not None else ""
        if name not in self._cmaps:
            try:
                self._cmaps[name] = fonts.font_manager.get_ttf_font(name).getBestCmap() or {}
            except Exception:
                self._cmaps[name] = {}
        return self._cmaps[name]

    def override_properties(self, entity, properties):
        if entity.dxftype() not in {"TEXT", "ATTRIB", "ATTDEF", "MTEXT"}:
            return
        text = entity.text if entity.dxftype() == "MTEXT" else entity.dxf.get("text", "")
        # Include the ideograph ranges, leaving CAD symbol/Latin fonts intact.
        required = {ord(ch) for ch in text if "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"}
        style_key = entity.dxf.get("style", "Standard").lower()
        original = self.style_fonts.get(style_key)
        if not required:
            return
        if required.issubset(self._cmap(properties.font)):
            if style_key in self.missing_font_styles:
                self.font_replacements[original or "默认字体"] += 1
            return
        if self._cjk_fallback is None:
            for name in ("simhei.ttf", "msyh.ttc", "msyh.ttf", "simsun.ttc", "NotoSansCJK-Regular.ttc"):
                if fonts.font_manager.has_font(name):
                    candidate = fonts.find_font_face(name)
                    if required.issubset(self._cmap(candidate)):
                        self._cjk_fallback = candidate
                        break
        if self._cjk_fallback is None or not required.issubset(self._cmap(self._cjk_fallback)):
            raise ValueError("底图含汉字，但本机可用字体不支持这些字形；请安装黑体、微软雅黑或思源黑体后重试。")
        if not original:
            original = properties.font.filename if properties.font is not None else "默认字体"
        self.font_replacements[original or "默认字体"] += 1
        # Properties belong to RenderContext; the original STYLE and entity
        # remain unchanged in doc and in subsequent DXF exports.
        properties.font = self._cjk_fallback


def prepare_preview(doc, *, background: str = "#101a26") -> PreparedPreview:
    """Return a modelspace preview bundle. Safe to call from a worker thread.

    Do not concurrently edit ``doc``. Rendering font caches is serialized across
    this function's callers; no Matplotlib Figure, Axes, or Tk object is touched.
    Empty/hidden-only drawings return bounds=None. Record order and fill holes
    are retained, including when adjacent entities have different colors.
    """
    started = perf_counter()
    with _PREPARE_LOCK:
        recorder = Recorder()
        config = Configuration(
            background_policy=BackgroundPolicy.CUSTOM,
            custom_bg_color=background,
            image_policy=ImagePolicy.RECT,
        )
        frontend = _PreviewFrontend(RenderContext(doc), recorder, config=config)
        frontend.style_fonts = {
            style.dxf.name.lower(): " + ".join(
                value for value in (style.dxf.get("font", ""), style.dxf.get("bigfont", "")) if value
            )
            for style in doc.styles
        }
        frontend.missing_font_styles = {
            style.dxf.name.lower() for style in doc.styles
            if style.dxf.get("font", "") and not fonts.font_manager.has_font(style.dxf.font)
        }
        frontend.draw_layout(doc.modelspace(), finalize=True)
        player = recorder.player()
        bbox = player.bbox()
        bounds = None
        if bbox.has_data:
            values = (bbox.extmin.x, bbox.extmin.y, bbox.extmax.x, bbox.extmax.y)
            if not all(math.isfinite(value) for value in values):
                raise ValueError("底图预览含无效坐标，无法计算图形范围。")
            bounds = values

        batches = []
        paths, faces, edges, weights, path_bounds = [], [], [], [], []
        point_positions, point_colors = [], []
        layers = set()
        rgba_cache = {}
        path_count = 0

        def flush_paths():
            if paths:
                batches.append(_PathBatch(tuple(paths), np.asarray(faces), np.asarray(edges), np.asarray(weights), np.asarray(path_bounds)))
                paths.clear(); faces.clear(); edges.clear(); weights.clear(); path_bounds.clear()

        def flush_points():
            if point_positions:
                batches.append(_PointBatch(np.asarray(point_positions), np.asarray(point_colors)))
                point_positions.clear(); point_colors.clear()

        def point(position, rgba):
            flush_paths()
            point_positions.append(position)
            point_colors.append(rgba)

        def path(shape, rgba, linewidth, fill=False):
            nonlocal path_count
            if not len(shape.vertices):
                return
            flush_points()
            paths.append(shape)
            faces.append(rgba if fill else (0, 0, 0, 0))
            edges.append((0, 0, 0, 0) if fill else rgba)
            weights.append(0.0 if fill else linewidth)
            vertices = shape.vertices
            if shape.codes is not None:
                vertices = vertices[shape.codes != MplPath.CLOSEPOLY]
            # The control-vertex hull is conservative for Bézier curves. Ignore
            # CLOSEPOLY's unused placeholder coordinate when computing bounds.
            low, high = vertices.min(axis=0), vertices.max(axis=0)
            path_bounds.append((low[0], low[1], high[0], high[1]))
            path_count += 1
            if len(paths) >= _MAX_PATHS_PER_COLLECTION:
                flush_paths()

        for record, properties in player.recordings():
            rgba = rgba_cache.get(properties.color)
            if rgba is None:
                rgba = rgba_cache[properties.color] = to_rgba(properties.color)
            layers.add(properties.layer)
            if isinstance(record, PointsRecord):
                vertices = record.points.np_vertices()
                count = len(vertices)
                if count == 1:
                    point(vertices[0], rgba)
                elif count == 2:
                    if np.array_equal(vertices[0], vertices[1]):
                        point(vertices[0], rgba)
                    else:
                        path(MplPath(vertices), rgba, properties.lineweight)
                elif count > 2:
                    # Explicit closure includes the last-to-first polygon edge.
                    closed = np.concatenate((vertices, vertices[:1]))
                    codes = np.full(len(closed), MplPath.LINETO, dtype=np.uint8)
                    codes[0], codes[-1] = MplPath.MOVETO, MplPath.CLOSEPOLY
                    path(MplPath(closed, codes), rgba, 0, fill=True)
            elif isinstance(record, SolidLinesRecord):
                vertices = record.lines.np_vertices()
                if len(vertices):
                    codes = np.full(len(vertices), MplPath.LINETO, dtype=np.uint8)
                    codes[::2] = MplPath.MOVETO
                    path(MplPath(vertices, codes), rgba, properties.lineweight)
            elif isinstance(record, PathRecord):
                path(to_matplotlib_path([record.path]), rgba, properties.lineweight)
            elif isinstance(record, FilledPathsRecord):
                path(to_matplotlib_path(record.paths, detect_holes=True), rgba, 0, fill=True)
            else:
                raise ValueError(f"不支持的预览记录：{type(record).__name__}")
        flush_paths()
        flush_points()

        warnings = []
        if frontend.font_replacements:
            replaced = "、".join(sorted(frontend.font_replacements))
            warnings.append(
                f"预览中 {sum(frontend.font_replacements.values())} 段汉字使用系统中文字体替代 {replaced}；"
                "字体外观或字宽可能不同，原图文字样式和出图内容未更改。"
            )
        if frontend.skipped:
            by_type = Counter()
            for (kind, _reason), count in frontend.skipped.items():
                by_type[kind] += count
            summary = "、".join(f"{kind} {count} 个" for kind, count in sorted(by_type.items()))
            warnings.append("二维预览未显示部分实体：" + summary + "；原图实体仍保留在导出底图中。")
        if doc.modelspace().query("IMAGE"):
            warnings.append("二维预览中外部图片只显示边框；请在 CAD 中查看图片内容。")
        if frontend.notices:
            warnings.append("底图渲染提示：" + "；".join(sorted(frontend.notices)))
        return PreparedPreview(
            tuple(batches), bounds, tuple(warnings), player.background,
            len(doc.modelspace()), len(player.records), path_count, frozenset(layers),
            perf_counter() - started,
        )


def render_prepared(bundle: PreparedPreview, ax, *, interactive_cache: bool = True) -> list:
    """Attach the prepared base in the UI thread and return all its artists.

    Does not clear axes or change current view limits. The caller can set
    ``base_bounds = bundle.bounds``, toggle visibility on returned artists and
    continue using its existing equal-aspect axes/fit/coordinate overlay code.
    All base collections use zorder=1, below the coordinate overlay. For large
    drawings the returned list contains one PreparedBaseArtist; the caller may
    call set_interacting(True/False) for temporary fast pan/zoom frames. Pass
    interactive_cache=False only for reference rendering/diagnostics.
    """
    artists = []
    grouped = interactive_cache and bundle.path_count >= 5000
    minimum_width = 72.0 / ax.get_figure().dpi
    ax.set_facecolor(bundle.background)
    for batch in bundle.batches:
        if isinstance(batch, _PathBatch):
            widths = np.where(batch.linewidths > 0, np.maximum(batch.linewidths, minimum_width), 0)
            artist = PathCollection(
                batch.paths, facecolors=batch.facecolors, edgecolors=batch.edgecolors,
                linewidths=widths, transform=ax.transData, zorder=1,
                capstyle="butt", joinstyle="round", antialiaseds=True,
            )
        else:
            # Dimensionless DXF POINT stays at its WCS position with a fixed
            # screen-size dot, matching the standard ezdxf Matplotlib backend.
            artist = PathCollection(
                [MplPath.unit_circle()], sizes=[0.1], offsets=batch.positions,
                offset_transform=ax.transData, transform=IdentityTransform(),
                facecolors=batch.colors, edgecolors="none", zorder=1,
            )
        if grouped:
            artist.axes = ax
            artist.set_figure(ax.figure)
            artist.set_clip_path(ax.patch)
        else:
            ax.add_collection(artist, autolim=False)
        artists.append(artist)
    if bundle.bounds is not None:
        x0, y0, x1, y1 = bundle.bounds
        ax.update_datalim(((x0, y0), (x1, y1)))
    if grouped:
        layer = PreparedBaseArtist(bundle, ax, artists)
        ax.add_artist(layer)
        return [layer]
    return artists
