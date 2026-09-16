"""Native-Tk regression checks for reversible, cached bottom-drawing visibility."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import ezdxf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coordtool import ui
from coordtool.core import Point, Settings, calc_labels


@unittest.skipUnless(os.name == "nt", "Requires the Windows desktop Tk runtime")
class BaseToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="coord-base-toggle-")
        cls.drawing = Path(cls.directory.name) / "small_base.dxf"
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_line((100, 200), (130, 240))
        doc.modelspace().add_circle((115, 220), 5)
        doc.saveas(cls.drawing)
        cls.app = ui.CoordinateApp(ROOT)
        cls.app.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app.dirty = False
        cls.app.close()
        cls.app.executor.shutdown(wait=True)
        cls.directory.cleanup()

    @staticmethod
    def complete_job(fn, callback):
        try:
            result = fn()
        except Exception as error:
            callback(None, error)
        else:
            callback(result, None)

    def setUp(self):
        app = self.app
        if app.preview_job:
            app.root.after_cancel(app.preview_job)
            app.preview_job = None
        self.submit = patch.object(app, "_submit", side_effect=self.complete_job)
        self.schedule = patch.object(app, "schedule_preview")
        self.warning = patch.object(ui.messagebox, "showwarning")
        self.error = patch.object(app, "error")
        for item in (self.submit, self.schedule, self.warning, self.error):
            item.start()
            self.addCleanup(item.stop)
        app.clear_base()
        app._changed_points([], record=False)
        app.pending_fit = False
        app.pending_drawing_fit = False
        app.dirty = False

    def load(self):
        self.app.load_base(self.drawing)
        self.assertIsNotNone(self.app.base_doc)
        self.assertTrue(self.app.base_artists)
        self.app.error.assert_not_called()

    def assert_controls(self, visible, loaded=True):
        app = self.app
        self.assertEqual(app.base_toggle_text.get(), "关闭底图" if visible and loaded else "打开底图")
        self.assertEqual(bool(app.show_base.get()), bool(visible and loaded))
        self.assertEqual(app.base_visibility_check.instate(["selected"]), bool(visible and loaded))
        self.assertEqual(app.base_visibility_check.instate(["disabled"]), not loaded)
        self.assertFalse(app.base_toggle_button.instate(["disabled"]))

    def test_initial_button_opens_file_picker(self):
        self.assert_controls(False, loaded=False)
        with patch.object(self.app, "open_base") as opener:
            self.app.base_toggle_button.invoke()
        opener.assert_called_once_with()
        self.assertIsNone(self.app.base_doc)

    def test_loaded_drawing_enables_close_button_and_checkbox(self):
        self.load()
        self.assert_controls(True)
        self.assertTrue(all(artist.get_visible() for artist in self.app.base_artists))

    def test_close_open_reuses_drawing_and_artists_without_changing_view_or_points(self):
        app = self.app
        self.load()
        app._changed_points([Point("A", 110, 210, -2.295), Point("B", 120, 230, 1.5)], record=False)
        app.labels = calc_labels(app.points, app.settings())
        app._draw_overlay(app.settings())
        app.ax.set_xlim(105.25, 127.75)
        app.ax.set_ylim(207.25, 235.75)
        app.canvas.draw()
        doc, preview, path, bounds = app.base_doc, app.base_preview, app.base_path, app.base_bounds
        artists, overlay, points = tuple(app.base_artists), tuple(app.artists), app.points
        xlim, ylim = app.ax.get_xlim(), app.ax.get_ylim()
        app.dirty = False
        with patch.object(ui.cad, "load_drawing") as load, \
             patch("coordtool.preview.prepare_preview") as prepare, \
             patch.object(app, "_render_base") as render:
            for _ in range(3):
                app.base_toggle_button.invoke()
                self.assert_controls(False)
                self.assertTrue(all(not item.get_visible() for item in artists))
                self.assertTrue(all(item.get_visible() for item in overlay))
                app.canvas.draw()
                app.base_toggle_button.invoke()
                self.assert_controls(True)
                self.assertTrue(all(item.get_visible() for item in artists))
                app.canvas.draw()
            load.assert_not_called()
            prepare.assert_not_called()
            render.assert_not_called()
        self.assertIs(app.base_doc, doc)
        self.assertIs(app.base_preview, preview)
        self.assertIs(app.base_path, path)
        self.assertIs(app.base_bounds, bounds)
        self.assertEqual(tuple(app.base_artists), artists)
        self.assertEqual(tuple(app.artists), overlay)
        self.assertIs(app.points, points)
        self.assertEqual(app.ax.get_xlim(), xlim)
        self.assertEqual(app.ax.get_ylim(), ylim)
        self.assertFalse(app.dirty, "A visibility-only action must not alter project data")

    def test_bottom_checkbox_and_top_button_stay_in_sync(self):
        self.load()
        self.app.base_visibility_check.invoke()
        self.assert_controls(False)
        self.app.base_toggle_button.invoke()
        self.assert_controls(True)
        self.app.base_toggle_button.invoke()
        self.assert_controls(False)
        self.app.base_visibility_check.invoke()
        self.assert_controls(True)

    def test_internal_clear_does_not_restore_old_drawing(self):
        self.load()
        self.app.base_toggle_button.invoke()
        self.app.clear_base()
        self.assert_controls(False, loaded=False)
        self.assertIsNone(self.app.base_doc)
        self.assertIsNone(self.app.base_preview)
        self.assertIsNone(self.app.base_path)
        self.assertIsNone(self.app.base_bounds)
        self.assertEqual(self.app.base_artists, [])
        with patch.object(self.app, "open_base") as opener:
            self.app.base_toggle_button.invoke()
        opener.assert_called_once_with()
        self.assertIsNone(self.app.base_doc)

    def test_loading_disables_controls_and_failure_restores_empty_state(self):
        jobs = []
        with patch.object(self.app, "_submit", side_effect=lambda fn, done: jobs.append((fn, done))):
            self.app.load_base(self.drawing)
        self.assertTrue(self.app.loading_drawing)
        self.assertTrue(self.app.base_toggle_button.instate(["disabled"]))
        self.assertTrue(self.app.base_visibility_check.instate(["disabled"]))
        jobs[0][1](None, ValueError("test read failure"))
        self.assertFalse(self.app.loading_drawing)
        self.assert_controls(False, loaded=False)
        self.app.error.assert_called_once()

    def test_loading_failure_keeps_previously_hidden_drawing_recoverable(self):
        self.load()
        app = self.app
        doc, artists, path = app.base_doc, tuple(app.base_artists), app.base_path
        app.base_toggle_button.invoke()
        with patch.object(ui.cad, "load_drawing", side_effect=ValueError("test replacement failure")):
            app.load_base(self.drawing.with_name("missing.dxf"))
        self.assert_controls(False)
        self.assertIs(app.base_doc, doc)
        self.assertIs(app.base_path, path)
        self.assertEqual(tuple(app.base_artists), artists)
        app.base_toggle_button.invoke()
        self.assert_controls(True)

    def test_late_completed_load_cannot_resurrect_cleared_drawing(self):
        jobs = []
        with patch.object(self.app, "_submit", side_effect=lambda fn, done: jobs.append((fn, done))):
            self.app.load_base(self.drawing)
        result = jobs[0][0]()
        self.app.clear_base()
        jobs[0][1](result, None)
        self.assert_controls(False, loaded=False)
        self.assertIsNone(self.app.base_doc)
        self.assertEqual(self.app.base_artists, [])

    def test_legacy_project_without_drawing_clears_previous_hidden_drawing(self):
        self.load()
        self.app.base_toggle_button.invoke()
        project = Path(self.directory.name) / "legacy-no-drawing.json"
        project.write_text(json.dumps({
            "format": "coordtool-project", "version": 1,
            "points": [{"name": "old", "e": 10, "n": 20}],
            "settings": asdict(Settings()), "drawing": None,
        }), encoding="utf-8")
        with patch.object(ui.messagebox, "askyesnocancel", return_value=False):
            self.app.open_project(project)
        self.app.error.assert_not_called()
        self.assertEqual(self.app.points, [Point("old", 10, 20)])
        self.assert_controls(False, loaded=False)
        self.assertIsNone(self.app.base_path)
        with patch.object(self.app, "open_base") as opener:
            self.app.base_toggle_button.invoke()
        opener.assert_called_once_with()

    def test_hidden_drawing_is_still_saved_as_project_drawing(self):
        self.load()
        self.app.base_toggle_button.invoke()
        project = Path(self.directory.name) / "hidden-drawing.json"
        with patch.object(self.app, "_save_path", return_value=str(project)):
            self.app.save_project()
        self.app.error.assert_not_called()
        self.assertEqual(json.loads(project.read_text(encoding="utf-8"))["drawing"], str(self.drawing.resolve()))
        self.assert_controls(False)

    def test_render_failure_restores_hidden_drawing_and_button(self):
        self.load()
        app = self.app
        doc, preview, path = app.base_doc, app.base_preview, app.base_path
        app.base_toggle_button.invoke()
        from coordtool.preview import render_prepared
        calls = []
        def fail_first(candidate, axes):
            calls.append(candidate)
            if len(calls) == 1:
                raise RuntimeError("test render failure")
            return render_prepared(candidate, axes)
        with patch("coordtool.preview.render_prepared", side_effect=fail_first):
            with self.assertRaisesRegex(RuntimeError, "test render failure"):
                app.load_base(self.drawing)
        self.assert_controls(False)
        self.assertIs(app.base_doc, doc)
        self.assertIs(app.base_preview, preview)
        self.assertIs(app.base_path, path)
        self.assertTrue(app.base_artists)
        self.assertTrue(all(not item.get_visible() for item in app.base_artists))
        app.base_toggle_button.invoke()
        self.assert_controls(True)

    def test_hidden_drawing_is_retained_in_actual_dxf_export(self):
        self.load()
        app = self.app
        app._changed_points([Point("exported", 110, 210, -2.295)], record=False)
        app.base_toggle_button.invoke()
        output = Path(self.directory.name) / "hidden-drawing-export.dxf"
        with patch.object(app, "_save_path", return_value=str(output)):
            app.export_dxf()
        app.error.assert_not_called()
        exported = ezdxf.readfile(output).modelspace()
        self.assertTrue(any(item.dxftype() == "LINE" and item.dxf.layer == "0"
                            and tuple(item.dxf.start)[:2] == (100., 200.)
                            and tuple(item.dxf.end)[:2] == (130., 240.) for item in exported))
        self.assertTrue(any(item.dxftype() == "CIRCLE" and item.dxf.layer == "0"
                            and item.dxf.radius == 5 for item in exported))
        self.assertTrue(any(item.dxf.layer == "COORD_POINTS" for item in exported))
        self.assert_controls(False)


if __name__ == "__main__":
    unittest.main()
