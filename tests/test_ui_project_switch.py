"""Project replacement must preserve edits or explicitly resolve them first."""
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordtool import ui
from coordtool.core import Point, Settings
from coordtool.project import atomic_json, load_project


@unittest.skipUnless(os.name == "nt", "Requires the Windows desktop Tk runtime")
class ProjectSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="coord-project-switch-")
        cls.folder = Path(cls.directory.name)
        cls.project = cls.folder / "project-b.json"
        atomic_json(cls.project, {
            "format": "coordtool-project", "version": 1,
            "points": [asdict(Point("B1", 100, 200))],
            "settings": asdict(Settings()), "drawing": None,
        })
        cls.app = ui.CoordinateApp(Path(__file__).resolve().parents[1])
        cls.app.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app.dirty = False
        cls.app.close()
        cls.app.executor.shutdown(wait=True)
        cls.directory.cleanup()

    def setUp(self):
        self.schedule = patch.object(self.app, "schedule_preview")
        self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.app.clear_base()
        self.app._set_settings(Settings(scale=1000, offset_e=500, offset_n=-20))
        self.app._changed_points([Point("A1", 10, 20, -3)], record=False)
        self.app.undo = [("A修改", [Point("A0", 9, 19)])]
        self.app.project_path = self.folder / "project-a.json"
        self.app.dirty = True

    def assert_original(self):
        self.assertEqual(self.app.points, [Point("A1", 10, 20, -3)])
        self.assertEqual(self.app.settings().scale, 1000)
        self.assertEqual(self.app.settings().offset_e, 500)
        self.assertTrue(self.app.dirty)
        self.assertEqual(len(self.app.undo), 1)

    def test_cancel_keeps_points_settings_history_and_project(self):
        original = self.app.project_path
        with patch.object(ui.messagebox, "askyesnocancel", return_value=None) as prompt:
            self.app.open_project(self.project)
        prompt.assert_called_once()
        self.assert_original()
        self.assertEqual(self.app.project_path, original)

    def test_cancel_save_dialog_does_not_discard_original_project(self):
        with patch.object(ui.messagebox, "askyesnocancel", return_value=True), \
             patch.object(self.app, "_save_path", return_value=""):
            self.app.open_project(self.project)
        self.assert_original()

    def test_failed_save_does_not_discard_original_project(self):
        with patch.object(ui.messagebox, "askyesnocancel", return_value=True), \
             patch.object(self.app, "_save_path", return_value=str(self.folder / "failed.json")), \
             patch("coordtool.project.atomic_json", side_effect=OSError("test write denied")), \
             patch.object(self.app, "error") as error:
            self.app.open_project(self.project)
        error.assert_called_once()
        self.assert_original()

    def test_save_before_switch_preserves_old_units_and_points_on_disk(self):
        saved = self.folder / "saved-a.json"
        with patch.object(ui.messagebox, "askyesnocancel", return_value=True), \
             patch.object(self.app, "_save_path", return_value=str(saved)):
            self.app.open_project(self.project)
        points, settings, _ = load_project(saved)
        self.assertEqual(points, [Point("A1", 10, 20, -3)])
        self.assertEqual((settings.scale, settings.offset_e), (1000, 500))
        self.assertEqual(self.app.points, [Point("B1", 100, 200)])
        self.assertFalse(self.app.dirty)

    def test_switch_resets_history_and_undo_stays_inside_new_project(self):
        with patch.object(ui.messagebox, "askyesnocancel", return_value=False):
            self.app.open_project(self.project)
        self.assertEqual(self.app.undo, [])
        self.app.undo_points()
        self.assertEqual(self.app.points, [Point("B1", 100, 200)])
        self.assertEqual((self.app.settings().scale, self.app.settings().offset_e), (1, 0))
        self.app._changed_points([Point("B2", 101, 201)], "编辑B")
        self.app.undo_points()
        self.assertEqual(self.app.points, [Point("B1", 100, 200)])

    def test_invalid_project_does_not_prompt_or_change_original(self):
        invalid = self.folder / "invalid.json"
        invalid.write_text("{}", encoding="utf-8")
        with patch.object(ui.messagebox, "askyesnocancel") as prompt, \
             patch.object(self.app, "error") as error:
            self.app.open_project(invalid)
        prompt.assert_not_called()
        error.assert_called_once()
        self.assert_original()

    def test_deleted_all_points_still_prompts_on_close(self):
        self.app._changed_points([], "删除全部坐标")
        with patch.object(ui.messagebox, "askyesnocancel", return_value=None) as prompt:
            self.app.close()
        prompt.assert_called_once()
        self.assertFalse(self.app.closed)
        self.assertTrue(self.app.dirty)

    def test_demo_cancel_preserves_current_project(self):
        with patch.object(ui.messagebox, "askyesnocancel", return_value=None), \
             patch.object(self.app, "load_base") as drawing:
            self.app.load_demo()
        drawing.assert_not_called()
        self.assert_original()

    def test_recovery_uses_replay_and_requires_new_project_save(self):
        recovered = [Point("找回的点", 11, 22)]
        with patch("coordtool.picker.recover_picker_snapshot", create=True,
                   return_value=(recovered, Settings(), None)) as replay, \
             patch.object(ui.messagebox, "askyesnocancel", return_value=False):
            self.app.open_project(self.folder / "recovery.coordproj", recover_picker=True)
        replay.assert_called_once()
        self.assertEqual(self.app.points, recovered)
        self.assertIsNone(self.app.project_path)
        self.assertTrue(self.app.dirty)
        self.assertEqual(self.app.undo, [])

    def test_recovery_displays_incomplete_tail_notice(self):
        def replay(path, *, warnings):
            warnings.append("最后一条记录未写完，已恢复此前完整记录。")
            return [Point("完整点", 1, 2)], Settings(), None
        with patch("coordtool.picker.recover_picker_snapshot", create=True, side_effect=replay), \
             patch.object(ui.messagebox, "askyesnocancel", return_value=False), \
             patch.object(ui.messagebox, "showwarning") as notice:
            self.app.open_project(self.folder / "recovery.coordproj", recover_picker=True)
        notice.assert_called_once()
        self.assertIn("最后一条记录未写完", notice.call_args.args[1])
        self.assertEqual(self.app.points, [Point("完整点", 1, 2)])


if __name__ == "__main__":
    unittest.main()
