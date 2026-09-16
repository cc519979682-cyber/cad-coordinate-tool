"""Native Tk checks for CAD return events, editing guards and manual labels.

Run this module in its own Python process: ttkbootstrap retains a Tk singleton.
The session transport is faked; these tests never start or alter AutoCAD.
"""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tkinter as tk
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from matplotlib.collections import LineCollection, PatchCollection

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coordtool import ui
from coordtool.core import Point, Placement, Settings, calc_labels


class FakeSession:
    """Small deterministic journal transport; UI calls its actual public API."""
    def __init__(self, session_id="road-session"):
        self.session_id = session_id
        self.ready = False
        self.done = False
        self.last_error = ""
        self.directory = ROOT/"qa"/"fake-session-never-written"
        self.recovery_path = self.directory/"recovery.coordproj"
        self.events = []
        self.launch = Mock()
        self.request_stop = Mock()
        self.apply_error = None
        self.settings = Settings()
        self.publish_zb_ready = Mock()
        self.disable_zb = Mock()
        self.take_zb_request = Mock(return_value=None)
        self.next_road_start = Mock(side_effect=self.road_start)

    @staticmethod
    def road_start(road, points):
        prefix = road.casefold() + "-"
        numbers = [int(point.name[len(prefix):]) for point in points
                   if point.name[:len(prefix)].casefold() == prefix
                   and point.name[len(prefix):].isascii()
                   and point.name[len(prefix):].isdigit()]
        return max(numbers, default=0) + 1

    def queue(self, *events):
        self.events.extend(events)

    def poll(self):
        events, self.events = self.events, []
        for kind, value in events:
            if kind == "READY":
                self.ready = True
            elif kind == "DONE":
                self.done = True
            elif kind == "ERROR":
                self.done = True
                self.last_error = value
        return events

    def apply(self, points, events):
        if self.apply_error:
            raise self.apply_error
        result = list(points)
        for kind, value in events:
            if kind == "ADD":
                result.append(value)
            elif kind == "UNDO":
                result = [point for point in result if point.capture_id != value]
        return result


@unittest.skipUnless(os.name == "nt", "Requires the Windows desktop Tk runtime")
class PickerUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.CoordinateApp(ROOT)
        cls.app.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app.picker_session = None
        cls.app.picker_starting = False
        cls.app.dirty = False
        cls.app.close()
        cls.app.executor.shutdown(wait=True)

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
        app.picker_session = None
        app.picker_starting = False
        app.picker_discovering = False
        app.picker_discovery_token += 1
        app.picker_stop_requested = False
        app.picker_settings = None
        app.picker_before = []
        app.picker_polled_at = 0.
        app.picker_button_text.set("CAD 取点")
        app._restoring_picker_settings = False
        if app.preview_job:
            app.root.after_cancel(app.preview_job)
            app.preview_job = None
        for attribute, value in (("_submit", self.complete_job), ("schedule_preview", None),
                                 ("error", None)):
            mocked = patch.object(app, attribute, side_effect=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        error = patch.object(ui.messagebox, "showerror")
        self.dialog_error = error.start()
        self.addCleanup(error.stop)
        confirmation = patch.object(ui.messagebox, "askyesno", return_value=False)
        self.disconnect_confirmation = confirmation.start()
        self.addCleanup(confirmation.stop)
        self.target = {"app_hwnd": 101, "doc_hwnd": 201, "name": "现场道路.dwg",
                       "path": str(ROOT/"examples"/"现场道路.dwg"), "units": 6, "active": True}
        discovery = patch("coordtool.cad_documents.list_open_drawings", return_value=[self.target])
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)
        app.clear_base()
        app._set_settings(Settings())
        app._changed_points([], record=False)
        app.undo = []
        app.pending_fit = False
        app.pending_drawing_fit = False
        app.dirty = False
        app.schedule_preview.reset_mock()
        self.addCleanup(self.destroy_dialogs)

    def destroy_dialogs(self):
        for child in self.app.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()

    def point(self, index=1, session=None):
        session_id = session.session_id if session else "road-session"
        return Point(f"道路甲-{index:02}", 100.+index, 200.+index, -2.295-index,
                     Placement(111.+index, 205.+index, 125.+index, 1.1, .6),
                     f"{session_id}:{index}", session_id)

    def start(self, session=None, create_error=None, settings=None):
        session = session or FakeSession()
        session.settings = settings or self.app.settings()
        module = ModuleType("coordtool.picker")
        factory = Mock(return_value=session, side_effect=create_error)
        module.PickerSession = type("PickerSession", (), {"attach": factory})
        with patch.dict(sys.modules, {"coordtool.picker": module}):
            self.app._start_picker("道路甲", 1, settings or self.app.settings(), target=self.target)
        return session, factory

    def draw_now(self):
        app = self.app
        app.labels = calc_labels(app.points, app.settings())
        app._draw_overlay(app.settings())

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from PickerUITests.descendants(child)

    def completed_session(self):
        session, _ = self.start()
        session.current_road = "道路甲"
        session.queue(("READY", None), ("ADD", self.point(1, session)), ("DONE", None))
        self.app._poll_picker(force=True)
        return session

    def test_road_menu_keeps_session_active_until_done_and_explains_q(self):
        app = self.app
        session, _ = self.start()
        session.queue(("READY", None), ("ADD", self.point(1, session)))
        app._poll_picker(force=True)
        # Enter/menu has no protocol event: no DONE means the session stays active.
        app._poll_picker(force=True)
        self.assertTrue(app._picker_active())
        self.assertIn("Enter 道路菜单", app.picker_text.get())
        self.assertIn("Q 结束全部", app.picker_text.get())
        self.assertIn("disabled", app.picker_resume_button.state())
        app.continue_picker()
        self.assertIn("Q 结束全部", app.status.get())
        session.queue(("DONE", None))
        app._poll_picker(force=True)
        self.assertFalse(app._picker_active())
        self.assertNotIn("disabled", app.picker_resume_button.state())

    def test_picker_dialog_documents_enter_menu_and_exact_cancel_behaviour(self):
        app = self.app
        self.assertIn("v22.8", app.root.title())
        app.open_picker()
        dialog = next(child for child in app.root.winfo_children() if isinstance(child, tk.Toplevel))
        dialog.withdraw()
        labels = "\n".join(str(child.cget("text")) for child in self.descendants(dialog)
                           if isinstance(child, ui.ttk.Label))
        for text in ("一条道路取完按 Enter", "菜单 Enter / N 换路", "C 继续当前路",
                     "Q 结束全部", "返回发起换路前的状态", "等待点位或道路菜单中按 Esc",
                     "在 CAD 输入 ZB", "ZB 必须填写路名"):
            self.assertIn(text, labels)

    def test_current_cad_selection_uses_selected_drawing_and_its_units(self):
        app = self.app
        original = Point("旧坐标", 1, 2)
        app._changed_points([original], record=False)
        app.base_path = ROOT/"other-preview.dwg"
        app.loading_drawing = True
        millimetres = dict(self.target, name="真正的道路图.dwg", path=str(ROOT/"真正的道路图.dwg"),
                           doc_hwnd=202, active=False, units=4)
        self.discovery.return_value = [self.target, millimetres]
        session = FakeSession()
        module = ModuleType("coordtool.picker")
        attach, create = Mock(return_value=session), Mock()
        module.PickerSession = type("PickerSession", (), {"attach": attach, "create": create})
        with patch.object(ui.filedialog, "askopenfilename") as chooser:
            app.open_picker()
        chooser.assert_not_called()
        dialog = app.picker_dialog
        dialog.withdraw()
        combo = next(child for child in self.descendants(dialog) if isinstance(child, ui.ttk.Combobox))
        self.assertEqual(str(combo.cget("state")), "readonly")
        self.assertEqual(combo.current(), 0)
        self.assertIn(self.target["name"], combo.get())
        combo.current(1)
        combo.event_generate("<<ComboboxSelected>>")
        details = "\n".join(app.root.getvar(child.cget("textvariable")) for child in self.descendants(dialog)
                            if isinstance(child, ui.ttk.Label) and child.cget("textvariable"))
        self.assertIn(millimetres["path"], details)
        self.assertIn("毫米", details)
        self.assertIn("直接添加到这张图纸", details)
        begin = next(child for child in self.descendants(dialog)
                     if isinstance(child, ui.ttk.Button) and child.cget("text") == "开始 CAD 取点")
        with patch.dict(sys.modules, {"coordtool.picker": module}):
            begin.invoke()
        attach.assert_called_once()
        self.assertEqual(attach.call_args.args[0], millimetres)
        self.assertEqual(attach.call_args.args[3].scale, 1000)
        self.assertEqual(attach.call_args.kwargs["snapshot_settings"].scale, 1000)
        self.assertEqual(attach.call_args.kwargs["initial_points"], [original])
        self.assertEqual(app.points, [original])
        self.assertIsNone(app.base_path)
        self.assertFalse(app.loading_drawing)
        create.assert_not_called()
        session.launch.assert_called_once()

    def test_no_open_cad_prompts_for_file_and_cancel_never_starts(self):
        app = self.app
        self.discovery.return_value = []
        app.base_path = ROOT/"software-preview-is-not-the-target.dwg"
        with patch.object(ui.filedialog, "askopenfilename", return_value="") as chooser, \
                patch.object(app, "_start_picker") as start:
            app.open_picker()
        chooser.assert_called_once()
        start.assert_not_called()
        self.assertIsNone(app.picker_dialog)
        self.assertFalse(app._picker_active())
        self.assertEqual(app.picker_button_text.get(), "CAD 取点")
        selected_path = str(ROOT/"用户选择的原图.dxf")
        app.unit.set("厘米（乘 100）")
        with patch.object(ui.filedialog, "askopenfilename", return_value=selected_path):
            app.open_picker()
        dialog = app.picker_dialog
        dialog.withdraw()
        begin = next(child for child in self.descendants(dialog)
                     if isinstance(child, ui.ttk.Button) and child.cget("text") == "开始 CAD 取点")
        with patch.object(app, "_start_picker") as start:
            begin.invoke()
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["target"],
                         {"path": selected_path, "name": "用户选择的原图.dxf"})
        self.assertEqual(start.call_args.args[2].scale, 100)

    def test_discovery_cancel_close_and_stale_callback_do_not_open_dialog(self):
        app = self.app
        jobs = []
        with patch.object(app, "_submit", side_effect=lambda fn, cb: jobs.append((fn, cb))):
            app.open_picker()
            self.assertTrue(app.picker_discovering)
            self.assertFalse(app._picker_active())
            self.assertEqual(app.picker_button_text.get(), "取消选图")
            app.open_picker()
            self.assertFalse(app.picker_discovering)
            self.assertEqual(len(jobs), 1)
            jobs[0][1]([self.target], None)
            self.assertIsNone(app.picker_dialog)
            app.open_picker()
            app._changed_points([Point("新点", 1, 2)], record=False)
            jobs[1][1]([self.target], None)
            self.assertIsNone(app.picker_dialog)
            app.open_picker()
            app.closed = True
            try:
                jobs[2][1]([self.target], None)
                self.assertIsNone(app.picker_dialog)
            finally:
                app.closed = False
                app._cancel_picker_discovery()
        self.assertEqual(app.picker_button_text.get(), "CAD 取点")

    def test_attach_same_preview_keeps_loaded_drawing_and_missing_target_does_nothing(self):
        app = self.app
        app.base_path = Path(self.target["path"])
        doc = app.base_doc = object()
        with patch.object(app, "clear_base") as clear:
            self.start()
        clear.assert_not_called()
        self.assertIs(app.base_doc, doc)
        app.picker_session = None
        with patch.object(app, "_submit") as submit:
            app._start_picker("道路", 1, app.settings())
        submit.assert_not_called()
        self.assertIn("请先选择当前 CAD 图纸", app.status.get())

    def test_unsaved_cad_target_clears_unrelated_software_background(self):
        app = self.app
        app.base_path = ROOT / "old-background.dwg"
        app.base_doc = object()
        target = dict(self.target, name="Drawing1.dwg", path=None)
        with patch.object(app, "_submit"):
            app._start_picker("道路", 1, app.settings(), target=target)
        self.assertIsNone(app.base_path)
        self.assertIsNone(app.base_doc)

    def test_done_publishes_zb_and_idle_poll_refreshes_without_starting_blank_request(self):
        app = self.app
        previous = self.completed_session()
        previous.publish_zb_ready.assert_called_once_with()
        self.assertIn("在 CAD 输入 ZB", app.picker_text.get())
        with patch.object(app, "_start_picker") as start:
            for request in (None, "", " "):
                previous.take_zb_request.return_value = request
                app._poll_picker(force=True)
        self.assertEqual(previous.publish_zb_ready.call_count, 4)
        self.assertEqual(previous.take_zb_request.call_count, 4)
        start.assert_not_called()
        previous.disable_zb.assert_not_called()
        self.dialog_error.assert_not_called()
        self.assertFalse(app._picker_active())

    def test_zb_two_rounds_resume_without_dialog_and_keep_correct_road_numbers(self):
        app = self.app
        previous = self.completed_session()
        previous.settings = replace(Settings(), text_height=.75)
        second = FakeSession("zb-round-two")
        third = FakeSession("zb-round-three")
        second.settings = third.settings = previous.settings
        module = ModuleType("coordtool.picker")
        create, resume = Mock(), Mock(side_effect=[second, third])
        module.PickerSession = type("PickerSession", (), {"create": create, "resume": resume})
        before = app.points[:]
        previous.take_zb_request.return_value = "道路甲"
        with patch.dict(sys.modules, {"coordtool.picker": module}), \
                patch.object(app, "open_picker") as dialog:
            app._poll_picker(force=True)
            resume.assert_called_once_with(previous, "道路甲", 2, previous.settings,
                                           initial_points=before, snapshot_settings=Settings())
            previous.next_road_start.assert_called_once_with("道路甲", before)
            self.assertTrue(previous.disable_zb.called)
            self.assertTrue(second.wait_for_idle)
            second.launch.assert_called_once_with()
            self.assertIs(app.picker_session, second)
            same_road = replace(self.point(2, second), placement=replace(self.point(2).placement, height=.75))
            second.current_road = "道路甲"
            second.queue(("READY", None), ("ADD", same_road), ("DONE", None))
            app._poll_picker(force=True)
            second.publish_zb_ready.assert_called_once_with()
            self.assertIs(app.picker_previous, second)
            second.take_zb_request.return_value = "新路"
            app._poll_picker(force=True)
            self.assertEqual(resume.call_count, 2)
            self.assertEqual(resume.call_args.args, (second, "新路", 1, second.settings))
            self.assertEqual(resume.call_args.kwargs["initial_points"], before+[same_road])
            self.assertTrue(third.wait_for_idle)
            third.launch.assert_called_once_with()
            third.current_road = "新路"
            new_road = replace(self.point(1, third), name="新路-01")
            third.queue(("READY", None), ("ADD", new_road), ("DONE", None))
            app._poll_picker(force=True)
            self.assertEqual([point.name for point in app.points], ["道路甲-01", "道路甲-02", "新路-01"])
            self.assertIs(app.picker_previous, third)
            third.publish_zb_ready.assert_called_once_with()
            dialog.assert_not_called()
        create.assert_not_called()
        self.dialog_error.assert_not_called()
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in app.root.winfo_children()))

    def test_zb_snapshot_changes_revoke_permission_before_consuming_request(self):
        app = self.app
        for changed in (lambda: app._changed_points([Point("更改点", 1, 2)]),
                        lambda: app.text_height.set("2.5"), app.clear_base):
            with self.subTest(change=changed):
                app._set_settings(Settings())
                app._changed_points([], record=False)
                previous = self.completed_session()
                previous.take_zb_request.return_value = "新道路"
                publish_count = previous.publish_zb_ready.call_count
                request_count = previous.take_zb_request.call_count
                changed()
                self.assertTrue(previous.disable_zb.called)
                with patch.object(app, "_start_picker") as start:
                    app._poll_picker(force=True)
                start.assert_not_called()
                self.assertEqual(previous.publish_zb_ready.call_count, publish_count)
                self.assertEqual(previous.take_zb_request.call_count, request_count)

    def test_invalid_zb_request_reports_once_and_keeps_manual_continue_available(self):
        app = self.app
        previous = self.completed_session()
        previous.take_zb_request.side_effect = ValueError("invalid request token")
        app._poll_picker(force=True)
        count = previous.publish_zb_ready.call_count
        for _ in range(3):
            app._poll_picker(force=True)
        self.assertEqual(previous.publish_zb_ready.call_count, count)
        self.assertEqual(previous.take_zb_request.call_count, 2)
        self.assertIn("invalid request token", app.status.get())
        self.assertTrue(previous.disable_zb.called)
        self.assertIs(app.picker_previous, previous)
        self.assertTrue(app._picker_resume_available())
        self.assertNotIn("disabled", app.picker_resume_button.state())
        app.error.assert_not_called()
        self.dialog_error.assert_not_called()

    def test_zb_factory_error_reports_in_status_without_a_popup(self):
        app = self.app
        previous = self.completed_session()
        previous.take_zb_request.return_value = "新道路"
        module = ModuleType("coordtool.picker")
        resume = Mock(side_effect=ValueError("working drawing unavailable"))
        module.PickerSession = type("PickerSession", (), {"resume": resume})
        with patch.dict(sys.modules, {"coordtool.picker": module}):
            app._poll_picker(force=True)
        resume.assert_called_once()
        self.assertFalse(app._picker_active())
        self.assertIsNone(app.picker_previous)
        self.assertIn("working drawing unavailable", app.status.get())
        self.assertTrue(previous.disable_zb.called)
        app.error.assert_not_called()
        self.dialog_error.assert_not_called()

    def test_zb_close_revokes_only_after_user_confirms_exit(self):
        app = self.app
        previous = self.completed_session()
        app.dirty = True
        with patch.object(ui.messagebox, "askyesnocancel", return_value=None):
            app.close()
        self.assertFalse(app.closed)
        self.assertIs(app.picker_previous, previous)
        previous.disable_zb.assert_not_called()
        with patch.object(ui.messagebox, "askyesnocancel", return_value=False), \
                patch.object(app.root, "destroy"), patch.object(app.canvas, "dispose"), \
                patch.object(app.executor, "shutdown"):
            app.close()
        try:
            self.assertTrue(app.closed)
            previous.disable_zb.assert_called_once()
            self.assertIsNone(app.picker_previous)
            previous.take_zb_request.return_value = "不应开始"
            publish_count = previous.publish_zb_ready.call_count
            with patch.object(app, "_start_picker") as start:
                app._poll_picker(force=True)
            start.assert_not_called()
            self.assertEqual(previous.publish_zb_ready.call_count, publish_count)
        finally:
            app.closed = False

    def test_active_error_or_disconnect_never_publishes_zb(self):
        app = self.app
        session, _ = self.start()
        session.queue(("READY", None), ("ADD", self.point(1, session)))
        app._poll_picker(force=True)
        session.publish_zb_ready.assert_not_called()
        session.queue(("ERROR", "CAD failed"))
        app._poll_picker(force=True)
        session.publish_zb_ready.assert_not_called()
        session, _ = self.start()
        session.queue(("READY", None))
        app._poll_picker(force=True)
        app.stop_picker()
        self.disconnect_confirmation.return_value = True
        app.stop_picker()
        session.publish_zb_ready.assert_not_called()
        self.assertFalse(app._picker_active())
        self.assertIsNone(app.picker_previous)

    def test_stop_request_hint_survives_more_returned_points(self):
        app = self.app
        session, _ = self.start()
        session.queue(("READY", None))
        app._poll_picker(force=True)
        app.stop_picker()
        session.queue(("ADD", self.point(1, session)))
        app._poll_picker(force=True)
        self.assertIn("已请求结束全部取点", app.picker_text.get())
        self.assertIn("Enter 或 Esc 响应", app.picker_text.get())
        self.assertIn("已回传 1 个点", app.picker_text.get())
        self.assertNotIn("Enter 道路菜单", app.picker_text.get())

    def test_resume_requires_completed_session_and_preserves_snapshot_when_saving(self):
        app = self.app
        app.continue_picker()
        self.assertIsNone(app.picker_previous)
        self.assertIn("disabled", app.picker_resume_button.state())
        previous = self.completed_session()
        self.assertIs(app.picker_previous, previous)
        self.assertEqual(app.picker_previous_points, app.points)
        self.assertEqual(app.picker_previous_settings, Settings())
        self.assertTrue(app._picker_resume_available())
        with patch.object(app, "_save_path", return_value=str(ROOT/"qa"/"not-written.json")), \
                patch("coordtool.project.atomic_json") as saved:
            app.save_project()
        saved.assert_called_once()
        self.assertIs(app.picker_previous, previous)
        self.assertTrue(app._picker_resume_available())
        self.assertNotIn("disabled", app.picker_resume_button.state())

    def test_continue_dialog_defaults_last_road_next_number_and_calls_resume_only(self):
        app = self.app
        previous = self.completed_session()
        old_points = app.points[:]
        new_session = FakeSession("resumed-road-session")
        module = ModuleType("coordtool.picker")
        create, resume = Mock(), Mock(return_value=new_session)
        module.PickerSession = type("PickerSession", (), {"create": create, "resume": resume})
        app.continue_picker()
        dialog = next(child for child in app.root.winfo_children() if isinstance(child, tk.Toplevel))
        dialog.withdraw()
        self.assertEqual(dialog.title(), "继续 CAD 取点")
        entries = sorted((child for child in self.descendants(dialog) if isinstance(child, ui.ttk.Entry)),
                         key=lambda child: int(child.grid_info()["row"]))
        self.assertEqual([entry.get() for entry in entries[:2]], ["道路甲", "2"])
        entries[2].delete(0, "end")
        entries[2].insert(0, "0.75")
        begin = next(child for child in self.descendants(dialog)
                     if isinstance(child, ui.ttk.Button) and child.cget("text") == "继续当前 CAD 图")
        with patch.dict(sys.modules, {"coordtool.picker": module}):
            begin.invoke()
        self.dialog_error.assert_not_called()
        create.assert_not_called()
        resume.assert_called_once_with(previous, "道路甲", 2, replace(Settings(), text_height=.75),
                                       initial_points=old_points, snapshot_settings=Settings())
        new_session.launch.assert_called_once_with()
        self.assertEqual(app.picker_before, old_points)
        self.assertIs(app.picker_session, new_session)
        self.assertIsNone(app.picker_previous)
        self.assertIn("disabled", app.picker_resume_button.state())
        appended = replace(self.point(2, new_session), placement=replace(self.point(2).placement, height=.75))
        new_session.queue(("READY", None), ("ADD", appended), ("DONE", None))
        app._poll_picker(force=True)
        self.assertEqual(app.points, old_points+[appended])
        self.assertIs(app.picker_previous, new_session)
        self.assertEqual(app.picker_previous_settings, Settings())
        self.assertEqual(app.points[0].placement.height, 1.1)
        self.assertEqual(app.points[1].placement.height, .75)
        app.undo_points()
        self.assertEqual(app.points, old_points)
        self.assertIsNone(app.picker_previous)

    def test_regular_new_pick_uses_attach_factory_after_completed_session(self):
        app = self.app
        self.completed_session()
        before = app.points[:]
        session, create = self.start()
        create.assert_called_once_with(self.target, "道路甲", 1, Settings(), initial_points=before,
                                       snapshot_settings=Settings())
        self.assertIs(app.picker_session, session)
        self.assertIsNone(app.picker_previous)

    def test_point_or_setting_change_permanently_disables_resume(self):
        app = self.app
        self.completed_session()
        before = app.points[:]
        app._changed_points([replace(before[0], e=before[0].e+1)])
        self.assertIsNone(app.picker_previous)
        self.assertIn("disabled", app.picker_resume_button.state())
        app.undo_points()
        self.assertEqual(app.points, before)
        self.assertFalse(app._picker_resume_available())
        app._changed_points([], record=False)
        self.completed_session()
        app.text_height.set("2.5")
        self.assertIsNone(app.picker_previous)
        self.assertIn("disabled", app.picker_resume_button.state())
        app.continue_picker()
        self.assertIn("坐标、设置或项目已改变", app.status.get())

    def test_new_base_or_project_disables_resume_even_when_points_match(self):
        app = self.app
        self.completed_session()
        app.clear_base()
        self.assertIsNone(app.picker_previous)
        app._changed_points([], record=False)
        self.completed_session()
        before = app.points[:]
        with patch("coordtool.project.load_project", return_value=(before, Settings(), None)), \
                patch("coordtool.ui.messagebox.askyesnocancel", return_value=False):
            app.open_project("another-project.json")
        self.assertEqual(app.points, before)
        self.assertIsNone(app.picker_previous)
        self.assertIn("disabled", app.picker_resume_button.state())

    def test_stale_continue_dialog_is_rejected_before_start(self):
        app = self.app
        self.completed_session()
        app.continue_picker()
        dialog = next(child for child in app.root.winfo_children() if isinstance(child, tk.Toplevel))
        dialog.withdraw()
        begin = next(child for child in self.descendants(dialog)
                     if isinstance(child, ui.ttk.Button) and child.cget("text") == "继续当前 CAD 图")
        app._changed_points([], record=False)
        with patch.object(app, "_start_picker") as start:
            begin.invoke()
        start.assert_not_called()
        self.dialog_error.assert_called_once()
        self.assertIn("不能续接", str(self.dialog_error.call_args))

    def test_error_or_failed_final_apply_cannot_offer_resume(self):
        app = self.app
        session, _ = self.start()
        session.queue(("READY", None), ("ERROR", "CAD failed"))
        app._poll_picker(force=True)
        self.assertIsNone(app.picker_previous)
        self.assertFalse(app._picker_resume_available())
        session, _ = self.start()
        session.apply_error = ValueError("snapshot write failed")
        session.queue(("READY", None), ("DONE", None))
        app._poll_picker(force=True)
        self.assertIsNone(app.picker_previous)
        self.assertFalse(app._picker_resume_available())

    def test_start_captures_current_snapshot_and_invalidates_pending_import(self):
        app = self.app
        original = Point("已有点", 1., 2., -3.)
        app._changed_points([original], record=False)
        app.undo = []
        app.pending_coordinates = ("file", "stale.csv")
        revision = app.file_revision
        selected = replace(app.settings(), text_height=.75)
        session, factory = self.start(settings=selected)
        self.assertEqual(app.file_revision, revision+1)
        self.assertIsNone(app.pending_coordinates)
        self.assertEqual(app.picker_before, [original])
        self.assertEqual(app.picker_settings, Settings())
        self.assertTrue(app._picker_active())
        self.assertFalse(app.picker_starting)
        self.assertIs(app.picker_session, session)
        self.assertEqual(app.picker_button_text.get(), "结束取点")
        factory.assert_called_once_with(self.target, "道路甲", 1, selected,
                                        initial_points=[original],
                                        snapshot_settings=Settings())
        session.launch.assert_called_once_with()

    def test_live_add_undo_preserves_metadata_and_draws_three_manual_texts(self):
        app = self.app
        initial = Point("旧点", 10., 20.)
        app._changed_points([initial], record=False)
        session, _ = self.start()
        point1, point2 = self.point(1), self.point(2)
        session.queue(("READY", None), ("ADD", point1), ("ADD", point2))
        app.schedule_preview.reset_mock()
        app._poll_picker(force=True)
        self.assertEqual(app.points, [initial, point1, point2])
        self.assertIs(app.points[1].placement, point1.placement)
        self.assertEqual(app.tree.selection(), ("2",))
        self.assertAlmostEqual(float(app.tree.item("1", "values")[3]), point1.z)
        app.schedule_preview.assert_called_once_with(fit=False)
        self.draw_now()
        self.assertEqual([text.get_text() for text in app.label_artists], [
            "旧点", "道路甲-01", "E=101.000", "N=201.000",
            "道路甲-02", "E=102.000", "N=202.000"])
        self.assertEqual(app.label_artists[1].get_position(), (112., 206.+.3*1.1))
        self.assertEqual(app.label_artists[1]._coord_height, 1.1)
        circles = next(artist for artist in app.artists if isinstance(artist, PatchCollection))
        self.assertEqual(len(circles.get_paths()), 1, "Only the original ordinary point has a circle")
        session.queue(("UNDO", point2.capture_id))
        app._poll_picker(force=True)
        self.assertEqual(app.points, [initial, point1])
        self.assertEqual(app.undo, [], "Individual CAD picks do not create conflicting app undo steps")
        self.draw_now()
        self.assertEqual(len(app.label_artists), 4)

    def test_existing_view_and_base_do_not_refit_on_each_returned_point(self):
        app = self.app
        app.base_bounds = (0., 0., 1000., 1000.)
        app.ax.set_xlim(90., 130.)
        app.ax.set_ylim(190., 230.)
        before = app.ax.get_xlim(), app.ax.get_ylim()
        session, _ = self.start()
        for index in (1, 2):
            session.queue(("ADD", self.point(index)))
            app.schedule_preview.reset_mock()
            app._poll_picker(force=True)
            app.schedule_preview.assert_called_once_with(fit=False)
            self.draw_now()
            self.assertEqual((app.ax.get_xlim(), app.ax.get_ylim()), before)

    def test_empty_drawing_fits_first_point_once_then_keeps_view(self):
        app = self.app
        session, _ = self.start()
        session.queue(("ADD", self.point(1)))
        app._poll_picker(force=True)
        app.schedule_preview.assert_called_once_with(fit=True)
        app.schedule_preview.reset_mock()
        session.queue(("ADD", self.point(2)))
        app._poll_picker(force=True)
        app.schedule_preview.assert_called_once_with(fit=False)

    def test_import_project_edit_and_drawing_controls_are_guarded_while_picking(self):
        app = self.app
        initial = self.point()
        app._changed_points([initial], record=False)
        app.undo = [("previous", [])]
        app.tree.selection_set("0")
        self.start()
        app.schedule_preview.reset_mock()
        revision, undo = app.file_revision, app.undo[:]
        actions = [app.open_coordinates, lambda: app.load_coordinates("never.csv"), app.paste,
                   app.edit_point, lambda: app.edit_point(new=True), app.delete_points,
                   app.swap, app.undo_points, app.open_base, lambda: app.load_base("never.dwg"),
                   lambda: app.open_project("never.json"), app.load_demo, app.recover_picker,
                   lambda: app._changed_points([Point("bad", 99, 99)])]
        with patch.object(ui.filedialog, "askopenfilename") as chooser:
            for action in actions:
                with self.subTest(action=action):
                    action()
                    self.assertEqual(app.points, [initial])
                    self.assertEqual(app.file_revision, revision)
                    self.assertEqual(app.undo, undo)
                    self.assertIn("正在 CAD 取点", app.status.get())
            chooser.assert_not_called()
        app.schedule_preview.assert_not_called()
        app.error.assert_not_called()
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in app.root.winfo_children()))

    def test_settings_restore_while_session_uses_fixed_coordinate_transform(self):
        app = self.app
        original = Settings(scale=1000., offset_e=10., offset_n=-20., color=3)
        app._set_settings(original)
        self.start()
        app.dirty = False
        app.schedule_preview.reset_mock()
        for variable, value in ((app.unit, "米（1 : 1）"), (app.offset_e, "200"),
                                (app.offset_n, "bad number"), (app.text_height, "15"),
                                (app.color, "红色"), (app.radius, "0")):
            with self.subTest(variable=variable):
                variable.set(value)
                self.assertEqual(app.settings(), original)
        app.draw_line.set(True)
        app._settings_changed()
        self.assertEqual(app.settings(), original)
        self.assertFalse(app.dirty)
        app.schedule_preview.assert_not_called()

    def test_session_finish_creates_one_undo_restoring_whole_prior_snapshot(self):
        app = self.app
        initial = self.point(7, FakeSession("old-road"))
        app._changed_points([initial], record=False)
        app.undo = [("earlier action", [])]
        session, _ = self.start()
        one, two = self.point(1), self.point(2)
        session.queue(("ADD", one))
        app._poll_picker(force=True)
        session.queue(("ADD", two), ("UNDO", two.capture_id), ("DONE", None))
        app._poll_picker(force=True)
        self.assertFalse(app._picker_active())
        self.assertEqual(app.picker_button_text.get(), "CAD 取点")
        self.assertIn("已回传 1 个点", app.picker_text.get())
        self.assertEqual(len(app.undo), 2)
        self.assertEqual(app.undo[-1], ("CAD 取点：道路甲", [initial]))
        app._poll_picker(force=True)
        self.assertEqual(len(app.undo), 2)
        app.undo_points()
        self.assertEqual(app.points, [initial])
        self.assertEqual(app.undo, [("earlier action", [])])

    def test_road_switch_updates_name_without_redraw_and_counts_all_segments(self):
        app = self.app
        session, _ = self.start()
        first = replace(self.point(1), group_id=session.session_id+":road:0")
        session.queue(("READY", None), ("ADD", first))
        app._poll_picker(force=True)
        self.assertIn("已回传 1 个点", app.picker_text.get())
        app.schedule_preview.reset_mock()
        session.current_road = "道路乙"
        session.queue(("ROAD", None))
        app._poll_picker(force=True)
        self.assertEqual(app.picker_road, "道路乙")
        self.assertEqual(app.points, [first])
        self.assertIn("N 换道路", app.picker_text.get())
        app.schedule_preview.assert_not_called()
        second = replace(self.point(2), name="道路乙-01", group_id=session.session_id+":road:1")
        session.queue(("ADD", second))
        app._poll_picker(force=True)
        self.assertIn("已回传 2 个点", app.picker_text.get())
        session.queue(("UNDO", second.capture_id), ("DONE", None))
        app._poll_picker(force=True)
        self.assertEqual(app.points, [first])
        self.assertIn("已回传 1 个点", app.picker_text.get())
        self.assertFalse(app._picker_active())

    def test_start_and_launch_failure_reset_controls_without_losing_existing_points(self):
        app = self.app
        initial = self.point(5)
        app._changed_points([initial], record=False)
        app.undo = []
        self.start(create_error=OSError("cannot create working copy"))
        self.assertFalse(app._picker_active())
        self.assertEqual(app.points, [initial])
        self.assertEqual(app.undo, [])
        self.assertEqual(app.picker_button_text.get(), "CAD 取点")
        self.assertIn("启动失败", app.picker_text.get())
        app.error.assert_called_once()
        app.error.reset_mock()
        session = FakeSession()
        session.launch.side_effect = OSError("CAD launch failed")
        self.start(session)
        self.assertFalse(app._picker_active())
        self.assertEqual(app.points, [initial])
        session.request_stop.assert_called_once_with()
        self.assertIn("恢复记录", app.picker_text.get())
        app.error.assert_called_once()

    def test_launch_send_error_drains_confirmed_events_before_finishing(self):
        app = self.app
        session = FakeSession()
        point = self.point()
        session.queue(("READY", None), ("ADD", point), ("DONE", None))
        session.launch.side_effect = OSError("send returned after CAD completed")
        self.start(session)
        self.assertEqual(app.points, [point])
        self.assertFalse(app._picker_active())
        self.assertEqual(len(app.undo), 1)
        app.error.assert_called_once()

    def test_stop_keeps_session_alive_until_durable_done_event(self):
        app = self.app
        session, _ = self.start()
        app.stop_picker()
        session.request_stop.assert_called_once_with()
        self.assertIs(app.picker_session, session)
        self.assertIn("Enter 或 Esc", app.picker_text.get())
        app.close()
        session.request_stop.assert_called_once_with()
        self.disconnect_confirmation.assert_not_called()
        self.assertFalse(app.closed)
        self.assertTrue(app.root.winfo_exists())
        self.assertIn("最后一批坐标回传", app.status.get())
        session.queue(("DONE", None))
        app._poll_picker(force=True)
        self.assertFalse(app._picker_active())
        self.assertEqual(app.undo, [])

    def test_declining_disconnect_keeps_session_and_confirmed_points(self):
        app = self.app
        session, _ = self.start()
        first = self.point(1)
        session.queue(("READY", None), ("ADD", first))
        app._poll_picker(force=True)
        app.stop_picker()
        self.assertTrue(app.picker_stop_requested)
        self.assertEqual(app.picker_button_text.get(), "断开取点")
        second = self.point(2)
        session.queue(("ADD", second))
        with patch.object(app, "_poll_picker", wraps=app._poll_picker) as poll:
            app.stop_picker()
            poll.assert_not_called()
        self.disconnect_confirmation.assert_called_once()
        self.assertIs(app.picker_session, session)
        self.assertTrue(app._picker_active())
        self.assertEqual(app.points, [first])
        self.assertEqual(session.events, [("ADD", second)])
        self.assertEqual(app.undo, [])
        session.request_stop.assert_called_once_with()

    def test_confirmed_disconnect_force_drains_events_and_keeps_final_points(self):
        app = self.app
        initial = Point("已有点", 10., 20., 3.)
        app._changed_points([initial], record=False)
        app.undo = []
        session, _ = self.start()
        first, second = self.point(1), self.point(2)
        session.queue(("READY", None), ("ADD", first))
        app._poll_picker(force=True)
        app.stop_picker()
        session.queue(("ADD", second), ("UNDO", first.capture_id))
        self.disconnect_confirmation.return_value = True
        with patch.object(app, "_poll_picker", wraps=app._poll_picker) as poll:
            app.stop_picker()
            poll.assert_called_once_with(force=True)
        self.disconnect_confirmation.assert_called_once()
        self.assertFalse(app._picker_active())
        self.assertIsNone(app.picker_session)
        self.assertEqual(app.picker_button_text.get(), "CAD 取点")
        self.assertIn("已断开 CAD 取点", app.picker_text.get())
        self.assertIn(str(session.recovery_path), app.picker_text.get())
        self.assertEqual(app.points, [initial, second])
        self.assertEqual(app.points[-1].placement, second.placement)
        self.assertEqual(session.events, [])
        self.assertEqual(app.undo, [("CAD 取点：道路甲", [initial])])
        session.request_stop.assert_called_once_with()
        app.error.assert_not_called()

    def test_return_error_stops_session_and_keeps_already_confirmed_points(self):
        app = self.app
        session, _ = self.start()
        first = self.point()
        session.queue(("ADD", first))
        app._poll_picker(force=True)
        session.apply_error = ValueError("capture identity conflict")
        session.queue(("ADD", self.point(2)))
        app._poll_picker(force=True)
        self.assertEqual(app.points, [first])
        self.assertFalse(app._picker_active())
        session.request_stop.assert_called_once_with()
        self.assertIn("原始取点记录保留", app.picker_text.get())
        self.assertEqual(len(app.undo), 1)
        app.error.assert_called_once()

    def test_render_only_connects_points_within_their_own_road(self):
        app = self.app
        points = [replace(self.point(i, FakeSession(f"road-{road}")),
                          e=1000.*road+i, n=2000.*road+i)
                  for road in range(3) for i in (1, 2)]
        app._changed_points(points, record=False)
        self.assertFalse(app.draw_line.get())
        self.draw_now()
        lines = next(artist for artist in app.artists if isinstance(artist, LineCollection))
        self.assertEqual(len(lines.get_segments()), 4*len(points))
        app.draw_line.set(True)
        self.draw_now()
        lines = next(artist for artist in app.artists if isinstance(artist, LineCollection))
        self.assertEqual(len(lines.get_segments()), 4*len(points)+3)
        for road, segment in enumerate(lines.get_segments()[-3:]):
            self.assertEqual(segment.tolist(), [[1000.*road+1, 2000.*road+1],
                                                [1000.*road+2, 2000.*road+2]])

    def test_edit_moves_manual_annotation_with_point_and_preserves_capture_identity(self):
        app = self.app
        point = self.point()
        app._changed_points([point], record=False)
        app.undo = []
        app.tree.selection_set("0")
        app.edit_point()
        dialog = next(child for child in app.root.winfo_children() if isinstance(child, tk.Toplevel))
        dialog.withdraw()
        entries = sorted((child for child in dialog.winfo_children() if isinstance(child, ui.ttk.Entry)),
                         key=lambda child: int(child.grid_info()["row"]))
        for entry, value in zip(entries, ("道路甲-改名", point.e+5., point.n+7., 2.5)):
            entry.delete(0, "end")
            entry.insert(0, str(value))
        save = next(child for child in dialog.winfo_children()
                    if isinstance(child, ui.ttk.Button) and child.cget("text") == "保存")
        save.invoke()
        self.dialog_error.assert_not_called()
        edited = app.points[0]
        self.assertEqual((edited.name, edited.e, edited.n, edited.z), ("道路甲-改名", point.e+5., point.n+7., 2.5))
        self.assertEqual((edited.capture_id, edited.group_id), (point.capture_id, point.group_id))
        self.assertEqual(edited.placement, replace(point.placement, e=point.placement.e+5.,
                                                  n=point.placement.n+7., horizontal_e=point.placement.horizontal_e+5.))
        app.undo_points()
        self.assertEqual(app.points, [point])


if __name__ == "__main__":
    unittest.main()
