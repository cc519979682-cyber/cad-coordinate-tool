from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import unittest

import ezdxf
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.colors import to_rgba

from coordtool.preview import prepare_preview, render_prepared


class PreviewTests(unittest.TestCase):
    def axes(self, size=(6, 4)):
        figure = Figure(figsize=size, dpi=100)
        canvas = FigureCanvasAgg(figure)
        return figure.add_axes((0, 0, 1, 1)), canvas

    def test_world_block_transform_and_hidden_layers(self):
        doc = ezdxf.new()
        doc.layers.new("hidden")
        doc.layers.get("hidden").off()
        block = doc.blocks.new("marker")
        block.add_line((0, 0), (2, 0), dxfattribs={"color": 0})
        doc.modelspace().add_blockref("marker", (5000, 10000), dxfattribs={"rotation": 90, "xscale": 3, "color": 1})
        doc.modelspace().add_line((-100, -100), (99999, 99999), dxfattribs={"layer": "hidden"})
        with ThreadPoolExecutor(1) as worker:
            bundle = worker.submit(prepare_preview, doc).result()
        np.testing.assert_allclose(bundle.bounds, (5000, 10000, 5000, 10006), atol=1e-9)
        self.assertNotIn("hidden", bundle.layers)
        self.assertFalse(bundle.warnings)
        ax, canvas = self.axes()
        artists = render_prepared(bundle, ax)
        self.assertTrue(any(np.allclose(color, to_rgba("#ff0000")) for a in artists for color in a.get_edgecolors()))
        self.assertEqual(tuple(doc.modelspace().query("INSERT")[0].dxf.insert), (5000, 10000, 0))

    def test_many_independent_entities_use_few_artists(self):
        doc = ezdxf.new()
        for i in range(10_000):
            doc.modelspace().add_line((i, 0), (i, 10), dxfattribs={"color": 1 + i % 3})
        bundle = prepare_preview(doc)
        ax, canvas = self.axes()
        artists = render_prepared(bundle, ax)
        self.assertEqual(bundle.primitive_count, 10_000)
        self.assertLessEqual(len(artists), 3)
        self.assertEqual(len(ax.lines), 0)
        self.assertEqual(len(ax.patches), 0)
        self.assertEqual(bundle.bounds, (0, 0, 9999, 10))

    def test_fill_draw_order_and_world_coordinates(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_solid([(5000, 10000), (5010, 10000), (5000, 10010), (5010, 10010)], dxfattribs={"color": 1})
        msp.add_solid([(5002, 10002), (5008, 10002), (5002, 10008), (5008, 10008)], dxfattribs={"color": 5})
        bundle = prepare_preview(doc)
        ax, canvas = self.axes()
        render_prepared(bundle, ax)
        ax.set_xlim(4999, 5011); ax.set_ylim(9999, 10011)
        ax.set_axis_off(); canvas.draw()
        pixels = np.asarray(canvas.buffer_rgba())
        np.testing.assert_array_equal(pixels[200, 300, :3], (0, 0, 255))
        self.assertEqual(bundle.bounds, (5000, 10000, 5010, 10010))

    def test_glyph_outlines_arcs_and_sample_geometry(self):
        sample = Path(__file__).parents[1] / "examples" / "sample_base.dxf"
        bundle = prepare_preview(ezdxf.readfile(sample))
        self.assertIn("BASE_TEXT", bundle.layers)
        self.assertGreater(bundle.path_count, 50)
        self.assertLess(len(bundle.batches), 10)
        self.assertTrue(all(math.isfinite(v) for v in bundle.bounds))
        ax, canvas = self.axes()
        render_prepared(bundle, ax)
        ax.set_xlim(4995, 5125); ax.set_ylim(9995, 10105)
        canvas.draw()
        self.assertGreater(len(np.unique(np.asarray(canvas.buffer_rgba()).reshape(-1, 4), axis=0)), 30)

    def test_external_image_uses_rectangle_without_opening_file(self):
        doc = ezdxf.new()
        image = doc.add_image_def(filename="this_image_does_not_exist.png", size_in_pixel=(100, 100))
        doc.modelspace().add_image(image, (5000, 10000), (10, 10))
        bundle = prepare_preview(doc)
        self.assertTrue(any("图片" in text for text in bundle.warnings))
        np.testing.assert_allclose(bundle.bounds, (5000, 10000, 5010, 10010), atol=.2)
        ax, canvas = self.axes()
        self.assertGreater(len(render_prepared(bundle, ax)), 0)

    def test_chinese_fallback_preserves_source_styles_and_real_glyphs(self):
        doc = ezdxf.new()
        style = doc.styles.new("missing", dxfattribs={"font": "missing_cjk.shx", "bigfont": "missing_big.shx"})
        source_attributes = style.dxf.all_existing_dxf_attribs().copy()
        text = doc.modelspace().add_text("工地", dxfattribs={"style": "missing", "height": 2})
        text.dxf.insert = (5000, 10000)
        bundle = prepare_preview(doc)
        self.assertTrue(any("missing_cjk.shx + missing_big.shx" in warning for warning in bundle.warnings))
        self.assertEqual(style.dxf.all_existing_dxf_attribs(), source_attributes)
        self.assertEqual(text.dxf.text, "工地")
        vertices = sum(len(path.vertices) for batch in bundle.batches if hasattr(batch, "paths") for path in batch.paths)
        self.assertGreater(vertices, 40)  # Actual ideograph outlines, not two tofu rectangles.

    def test_empty_and_point_only_drawing(self):
        doc = ezdxf.new()
        bundle = prepare_preview(doc)
        self.assertIsNone(bundle.bounds)
        ax, canvas = self.axes()
        self.assertEqual(render_prepared(bundle, ax), [])
        doc.modelspace().add_point((5000, 10000))
        bundle = prepare_preview(doc)
        self.assertEqual(bundle.bounds, (5000, 10000, 5000, 10000))
        artists = render_prepared(bundle, ax)
        self.assertEqual(len(artists), 1)
        np.testing.assert_array_equal(artists[0].get_offsets(), [[5000, 10000]])


if __name__ == "__main__":
    unittest.main()
