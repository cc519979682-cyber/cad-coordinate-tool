"""Small native-Tk regression checks for asynchronous import/edit guards."""
from pathlib import Path
import os
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coordtool import ui
from coordtool.core import Point


@unittest.skipUnless(os.name == "nt", "Requires the Windows desktop Tk runtime")
class ImportEditGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.CoordinateApp(ROOT)
        cls.app.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app.dirty = False
        cls.app.close()
        cls.app.executor.shutdown(wait=True)

    def setUp(self):
        self.app.loading_drawing = False
        self.app.pending_coordinates = None
        self.app._changed_points([], record=False)

    def test_new_edit_cancels_deferred_file_import_and_keeps_elevation(self):
        app = self.app
        app.loading_drawing = True
        source = ROOT/"examples"/"sample_points.csv"
        app.load_coordinates(source)
        self.assertEqual(app.pending_coordinates, ("file", source))
        edited = Point("edited", 10, 20, -2.295)
        app._changed_points([edited], "edit after queued import", record=False)
        self.assertIsNone(app.pending_coordinates)
        self.assertEqual(app.points, [edited])
        self.assertEqual(app.points[0].z, -2.295)
        app.loading_drawing = False

    def test_stale_editor_cannot_overwrite_newly_imported_points_or_z(self):
        app = self.app
        app._changed_points([Point("old", 1, 2, -3)], record=False)
        app.tree.selection_set("0")
        app.edit_point()
        dialog = next(widget for widget in app.root.winfo_children() if isinstance(widget, tk.Toplevel))
        dialog.withdraw()
        try:
            newer = Point("newly imported", 100, 200, 12.345)
            app._changed_points([newer], "background import", record=False)
            save = next(widget for widget in dialog.winfo_children()
                        if isinstance(widget, ui.ttk.Button) and widget.cget("text") == "保存")
            with patch.object(ui.messagebox, "showerror") as errors:
                save.invoke()
            self.assertEqual(errors.call_count, 1)
            self.assertIn("后台更新", errors.call_args.args[1])
            self.assertEqual(app.points, [newer])
            self.assertEqual(float(app.tree.item("0", "values")[3]), newer.z)
        finally:
            dialog.destroy()

    def test_failed_drawing_cancels_deferred_file_and_clipboard_imports(self):
        app = self.app
        original = Point("保留点", 10, 20)
        for kind in ("file", "text"):
            app._changed_points([original], record=False)
            jobs = []
            with patch.object(app, "_submit", side_effect=lambda work, done: jobs.append(done)):
                app.load_base("unreadable.dwg")
            app.pending_coordinates = (kind, "queued.csv" if kind == "file" else "1,2")
            with patch.object(app, "error") as error:
                jobs[0](None, ValueError("测试底图读取失败"))
            self.assertFalse(app.loading_drawing)
            self.assertIsNone(app.pending_coordinates)
            self.assertEqual(app.points, [original])
            self.assertIn("坐标导入已取消", str(error.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
