"""A base-drawing layer with exact settled rendering and fast navigation frames.

Only rendered screen pixels are transformed while the caller marks navigation
active. The first settled draw always renders complete vector geometry in the
current view. No DXF entity or prepared path is simplified or modified.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from time import perf_counter

import numpy as np
from PIL import Image
from matplotlib.artist import Artist, allow_rasterization


@dataclass(frozen=True)
class ViewportStats:
    total_paths: int
    visible_paths: int
    visible_points: int
    updated_batches: int
    seconds: float


def _same_indices(a, b):
    return (a is None and b is None) or (a is not None and b is not None and np.array_equal(a, b))


def update_collections_for_view(bundle, collections, ax) -> ViewportStats:
    """Conservatively filter by control-vertex bounds; preserve path order.

    Curves lie inside their control-vertex hull. Padding covers anti-aliasing,
    point markers and stroke width; paths crossing a viewport remain included
    even when all their endpoints are outside. Visibility toggles are untouched.
    """
    started = perf_counter()
    if len(bundle.batches) != len(collections):
        raise ValueError("预览集合与准备数据不匹配。")
    x0, x1 = sorted(ax.get_xlim())
    y0, y1 = sorted(ax.get_ylim())
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        raise ValueError("视图范围必须是有限数值。")
    # In screen pixels; the extra margin also covers round joins and fixed dots.
    maximum_points = max((float(np.max(b.linewidths, initial=0)) for b in bundle.batches if hasattr(b, "paths")), default=0)
    pixels = max(3.0, maximum_points * ax.figure.dpi / 72.0 + 2.0)
    padx = abs(x1-x0) / max(ax.bbox.width, 1) * pixels
    pady = abs(y1-y0) / max(ax.bbox.height, 1) * pixels
    lo_x, hi_x, lo_y, hi_y = x0-padx, x1+padx, y0-pady, y1+pady
    visible_paths = visible_points = updated = 0
    minimum_width = 72.0 / ax.figure.dpi
    for batch, collection in zip(bundle.batches, collections):
        if hasattr(batch, "paths"):
            bounds = batch.bounds
            mask = ((bounds[:, 0] <= hi_x) & (bounds[:, 2] >= lo_x)
                    & (bounds[:, 1] <= hi_y) & (bounds[:, 3] >= lo_y))
            count = int(np.count_nonzero(mask))
            visible_paths += count
            indices = None if count == len(batch.paths) else np.flatnonzero(mask)
            previous = getattr(collection, "_coordtool_view_indices", None)
            if _same_indices(previous, indices):
                continue
            if indices is None:
                paths, faces, edges, weights = batch.paths, batch.facecolors, batch.edgecolors, batch.linewidths
            else:
                paths = [batch.paths[int(i)] for i in indices]
                faces, edges, weights = batch.facecolors[indices], batch.edgecolors[indices], batch.linewidths[indices]
            collection.set_paths(paths)
            collection.set_facecolor(faces)
            collection.set_edgecolor(edges)
            collection.set_linewidth(np.where(weights > 0, np.maximum(weights, minimum_width), 0))
        else:
            positions = batch.positions
            mask = ((positions[:, 0] >= lo_x) & (positions[:, 0] <= hi_x)
                    & (positions[:, 1] >= lo_y) & (positions[:, 1] <= hi_y))
            count = int(np.count_nonzero(mask))
            visible_points += count
            indices = None if count == len(positions) else np.flatnonzero(mask)
            previous = getattr(collection, "_coordtool_view_indices", None)
            if _same_indices(previous, indices):
                continue
            collection.set_offsets(positions if indices is None else positions[indices])
            collection.set_facecolor(batch.colors if indices is None else batch.colors[indices])
        collection._coordtool_view_indices = indices
        updated += 1
    return ViewportStats(bundle.path_count, visible_paths, visible_points, updated, perf_counter()-started)


class PreparedBaseArtist(Artist):
    """Own base collections and cache their pixels before axes/overlays draw.

    Call ``set_interacting(True)`` during pan/zoom, then False on mouse release
    or after the final scroll event. A stationary cache may also be reused on
    unrelated overlay redraws, but only at the exact same view and resolution.
    """
    def __init__(self, bundle, ax, collections):
        super().__init__()
        self.bundle = bundle
        self.collections = tuple(collections)
        self.axes = ax
        self.set_figure(ax.figure)
        self.set_zorder(1)
        self._interacting = False
        self._snapshot = None
        self._signature = None
        self._cache_matrix = None
        self._cache_box = None
        self.last_view_stats = None
        self.last_draw_seconds = 0.0
        self.last_draw_mode = "none"
        self.vector_draws = 0
        self.cached_draws = 0

    def set_interacting(self, active: bool):
        self._interacting = bool(active)
        self.stale = True

    def set_visible(self, visible):
        if bool(visible) != self.get_visible():
            self.invalidate_cache()
        super().set_visible(visible)

    def invalidate_cache(self):
        self._snapshot = None
        self._signature = None
        self._cache_matrix = None
        self._cache_box = None
        self.stale = True

    def get_children(self):
        return list(self.collections)

    def _geometry(self, renderer):
        width, height = int(renderer.width), int(renderer.height)
        bbox = self.axes.bbox
        box = (max(0, math.floor(bbox.x0)), max(0, math.floor(bbox.y0)),
               min(width, math.ceil(bbox.x1)), min(height, math.ceil(bbox.y1)))
        matrix = self.axes.transData.get_affine().get_matrix().copy()
        # Equal-aspect layout can move a bound by floating point roundoff while
        # panning; subpixel roundoff must not force an expensive vector redraw.
        resolution = (width, height, self.axes.figure.dpi, tuple(round(v, 5) for v in bbox.bounds), self.axes.get_facecolor())
        return box, matrix, resolution

    def _paint_snapshot(self, renderer, box, matrix):
        x0, y0, x1, y1 = box
        old_x0, _old_y0, _old_x1, old_y1 = self._cache_box
        if x1 <= x0 or y1 <= y0:
            return
        if np.array_equal(matrix, self._cache_matrix) and box == self._cache_box:
            pixels = self._snapshot
        else:
            transform = self._cache_matrix @ np.linalg.inv(matrix)
            # PIL coordinates run downward; display coordinates run upward.
            a, b = transform[0, 0], -transform[0, 1]
            c = transform[0, 0]*x0 + transform[0, 1]*y1 + transform[0, 2] - old_x0
            d, e = -transform[1, 0], transform[1, 1]
            f = old_y1 - transform[1, 0]*x0 - transform[1, 1]*y1 - transform[1, 2]
            background = tuple(round(v*255) for v in self.axes.get_facecolor())
            pixels = np.asarray(Image.fromarray(self._snapshot).transform(
                (x1-x0, y1-y0), Image.Transform.AFFINE, (a, b, c, d, e, f),
                resample=Image.Resampling.BILINEAR, fillcolor=background,
            ))
        gc = renderer.new_gc()
        try:
            gc.set_clip_rectangle(self.axes.bbox)
            renderer.draw_image(gc, x0, y0, np.ascontiguousarray(pixels[::-1]))
        finally:
            gc.restore()

    @allow_rasterization
    def draw(self, renderer):
        if not self.get_visible():
            return
        started = perf_counter()
        can_cache = (hasattr(renderer, "buffer_rgba") and hasattr(renderer, "width")
                     and self.axes.get_xscale() == "linear" and self.axes.get_yscale() == "linear"
                     and self.axes.xaxis.get_zorder() > self.get_zorder()
                     and self.axes.yaxis.get_zorder() > self.get_zorder())
        # If a caller puts the grid below this layer (axisbelow=True), the
        # already-rendered pixels contain grid lines. Fall back to vectors so
        # those axes/grid pixels can never be shifted as part of the base cache.
        if can_cache:
            box, matrix, resolution = self._geometry(renderer)
            complete_signature = (resolution, tuple(matrix.ravel()))
            same_resolution = self._signature is not None and self._signature[0] == resolution
            if self._snapshot is not None and same_resolution and (self._interacting or self._signature == complete_signature):
                self._paint_snapshot(renderer, box, matrix)
                self.last_draw_mode = "interactive-raster" if self._interacting else "exact-raster"
                self.cached_draws += 1
                self.last_draw_seconds = perf_counter()-started
                self.stale = False
                return
        self.last_view_stats = update_collections_for_view(self.bundle, self.collections, self.axes)
        for collection in self.collections:
            collection.draw(renderer)
        self.vector_draws += 1
        self.last_draw_mode = "complete-vector"
        if can_cache:
            x0, y0, x1, y1 = box
            pixels = np.asarray(renderer.buffer_rgba())
            self._snapshot = pixels[int(renderer.height)-y1:int(renderer.height)-y0, x0:x1].copy()
            self._signature = complete_signature
            self._cache_matrix = matrix
            self._cache_box = box
        self.last_draw_seconds = perf_counter()-started
        self.stale = False
