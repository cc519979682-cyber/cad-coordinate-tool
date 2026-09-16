import unittest

import ezdxf
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from coordtool.preview import prepare_preview, render_prepared
from coordtool.preview_layer import PreparedBaseArtist, update_collections_for_view


class PreviewLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        doc = ezdxf.new()
        msp = doc.modelspace()
        # Visible reference: two colored squares and a crossing line.
        msp.add_solid([(0, 0), (3, 0), (0, 3), (3, 3)], dxfattribs={"color": 1})
        msp.add_solid([(5, 4), (8, 4), (5, 7), (8, 7)], dxfattribs={"color": 5})
        msp.add_line((-20, 2), (20, 2), dxfattribs={"color": 3})
        for i in range(5000):
            msp.add_line((100+i, 100), (100+i, 101), dxfattribs={"color": 7})
        msp.add_point((1, 1), dxfattribs={"color": 2})
        cls.bundle = prepare_preview(doc)

    def figure(self, cached=True):
        fig = Figure(figsize=(6, 4), dpi=100, facecolor="#101a26")
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_axes((.1, .1, .8, .8))
        ax.set_xlim(-2, 10); ax.set_ylim(-1, 9)
        artists = render_prepared(self.bundle, ax, interactive_cache=cached)
        return fig, canvas, ax, artists

    def test_group_culls_far_paths_and_keeps_crossing_geometry(self):
        fig, canvas, ax, artists = self.figure()
        layer = artists[0]
        self.assertIsInstance(layer, PreparedBaseArtist)
        canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")
        self.assertEqual(layer.last_view_stats.visible_paths, 3)
        self.assertEqual(layer.last_view_stats.visible_points, 1)
        self.assertEqual(len(ax.artists), 1)
        self.assertEqual(len(ax.collections), 0)

    def test_settled_culling_pixels_equal_complete_reference(self):
        _fig, canvas, _ax, _artists = self.figure(cached=True)
        canvas.draw(); actual = np.asarray(canvas.buffer_rgba()).copy()
        _fig, canvas, _ax, _artists = self.figure(cached=False)
        canvas.draw(); expected = np.asarray(canvas.buffer_rgba()).copy()
        np.testing.assert_array_equal(actual, expected)

    def test_same_view_snapshot_is_pixel_exact_and_overlay_not_cached(self):
        fig, canvas, ax, artists = self.figure()
        layer = artists[0]
        overlay, = ax.plot([1], [8], "o", color="yellow", markersize=8, zorder=8)
        canvas.draw(); first = np.asarray(canvas.buffer_rgba()).copy()
        canvas.draw(); second = np.asarray(canvas.buffer_rgba()).copy()
        self.assertEqual(layer.last_draw_mode, "exact-raster")
        np.testing.assert_array_equal(first, second)
        overlay.set_data([7], [8])
        canvas.draw(); actual = np.asarray(canvas.buffer_rgba()).copy()
        layer.invalidate_cache(); canvas.draw()
        np.testing.assert_array_equal(actual, np.asarray(canvas.buffer_rgba()))

    def test_interaction_transforms_then_restores_full_detail(self):
        fig, canvas, ax, artists = self.figure()
        layer = artists[0]
        canvas.draw()
        layer.set_interacting(True)
        ax.set_xlim(-1, 11)
        canvas.draw()
        self.assertEqual(layer.last_draw_mode, "interactive-raster")
        self.assertEqual(layer.vector_draws, 1)
        # The red square remains at its current data location during navigation.
        px, py = ax.transData.transform((1.5, 1.5))
        image = np.asarray(canvas.buffer_rgba())
        np.testing.assert_array_equal(image[image.shape[0]-round(py), round(px), :3], (255, 0, 0))
        layer.set_interacting(False)
        canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")
        actual = np.asarray(canvas.buffer_rgba()).copy()
        _fig, other_canvas, other_ax, _artists = self.figure(cached=False)
        other_ax.set_xlim(-1, 11); other_canvas.draw()
        np.testing.assert_array_equal(actual, np.asarray(other_canvas.buffer_rgba()))

    def test_zoom_resize_and_visibility_invalidate_safely(self):
        fig, canvas, ax, artists = self.figure()
        layer = artists[0]
        canvas.draw(); layer.set_interacting(True)
        ax.set_xlim(-.5, 4); ax.set_ylim(-.5, 4)
        canvas.draw()
        self.assertEqual(layer.last_draw_mode, "interactive-raster")
        px, py = ax.transData.transform((1.5, 1.5))
        image = np.asarray(canvas.buffer_rgba())
        np.testing.assert_array_equal(image[image.shape[0]-round(py), round(px), :3], (255, 0, 0))
        fig.set_size_inches(7, 5); canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")
        self.assertEqual(layer._snapshot.shape[1], round(ax.bbox.width))
        layer.set_visible(False); self.assertIsNone(layer._snapshot)
        canvas.draw(); layer.set_visible(True); canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")

    def test_culling_restores_all_paths_and_preserves_visibility(self):
        _fig, _canvas, ax, artists = self.figure(cached=False)
        artists[0].set_visible(False)
        stats = update_collections_for_view(self.bundle, artists, ax)
        self.assertEqual(stats.visible_paths, 3)
        self.assertFalse(artists[0].get_visible())
        x0, y0, x1, y1 = self.bundle.bounds
        ax.set_xlim(x1+1, x0-1)  # Inverted view is valid.
        ax.set_ylim(y1+1, y0-1)
        stats = update_collections_for_view(self.bundle, artists, ax)
        self.assertEqual(stats.visible_paths, self.bundle.path_count)
        self.assertEqual(sum(len(a.get_paths()) for a, b in zip(artists, self.bundle.batches) if hasattr(b, "paths")), self.bundle.path_count)

    def test_grid_above_base_stays_fresh_during_interaction(self):
        fig, canvas, ax, artists = self.figure()
        ax.set_axisbelow("line")
        ax.grid(True, color="white", linewidth=1)
        ax.set_xticks([0, 2, 4, 6, 8, 10]); ax.set_yticks([0, 2, 4, 6, 8])
        layer = artists[0]
        canvas.draw(); layer.set_interacting(True)
        ax.set_xlim(-1, 11); canvas.draw()
        active = np.asarray(canvas.buffer_rgba()).copy()
        layer.set_interacting(False); canvas.draw()
        settled = np.asarray(canvas.buffer_rgba()).copy()
        # Above the base geometry, current grid lines remain pixel-identical.
        px0, py0 = ax.transData.transform((0, 7.5))
        px1, py1 = ax.transData.transform((10, 8.5))
        lo, hi = active.shape[0]-round(py1), active.shape[0]-round(py0)
        np.testing.assert_array_equal(active[lo:hi, round(px0):round(px1)], settled[lo:hi, round(px0):round(px1)])

    def test_grid_below_base_uses_safe_vector_fallback(self):
        fig, canvas, ax, artists = self.figure()
        ax.set_axisbelow(True); ax.grid(True)
        layer = artists[0]
        canvas.draw(); layer.set_interacting(True); ax.set_xlim(-1, 11); canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")
        self.assertIsNone(layer._snapshot)

    def test_equal_aspect_successive_zoom_keeps_interactive_cache(self):
        fig, canvas, ax, artists = self.figure()
        fig.set_size_inches(11.32, 7)
        ax.set_position((.065, .075, .92, .9))
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(-12000.125, -3000.625)
        ax.set_ylim(-8000.875, -3500.125)
        canvas.draw()
        layer = artists[0]
        layer.set_interacting(True)
        center = (-8300.375, -5900.625)
        initial_vectors = layer.vector_draws
        for factor in [1/1.15]*10 + [1.15]*10:
            ax.set_xlim(*(center[0]+(x-center[0])*factor for x in ax.get_xlim()))
            ax.set_ylim(*(center[1]+(y-center[1])*factor for y in ax.get_ylim()))
            # The UI prepares text sizes after this operation, then Matplotlib
            # calls apply_aspect again during the same draw.
            ax.apply_aspect()
            canvas.draw()
            self.assertEqual(layer.last_draw_mode, "interactive-raster")
        self.assertEqual(layer.vector_draws, initial_vectors)
        layer.set_interacting(False)
        canvas.draw()
        self.assertEqual(layer.last_draw_mode, "complete-vector")


if __name__ == "__main__":
    unittest.main()
