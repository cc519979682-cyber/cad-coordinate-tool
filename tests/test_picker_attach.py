"""Focused functional coverage for taking points in the user's selected drawing."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordtool.core import Point, Settings
from coordtool.picker import PickerError, PickerSession
from coordtool.project import load_project
from tests.test_picker import add


class AttachedPickerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "用户选中的道路原图.dwg"
        self.source.write_bytes(b"AC1032 selected drawing stays unchanged\x00")
        self.settings = Settings(scale=1000., offset_e=1., offset_n=-2.)
        self.target = dict(app_hwnd=1001, doc_hwnd=2001, name=self.source.name,
                           path=str(self.source), units=4, active=True)

    def attach(self, **changes):
        options = dict(target=self.target, road="田间道路", start=1,
                       settings=self.settings, root=self.root / "sessions")
        options.update(changes)
        return PickerSession.attach(**options)

    def fake_com(self, *, target=None, saved=True, opened=True, app_hwnd=1001):
        target = target or self.target
        doc = Mock()
        doc.HWND = target.get("doc_hwnd", 2001)
        doc.Name = target["name"]
        doc.Path = str(Path(target["path"]).parent) if saved else ""
        doc.FullName = target["path"] if saved else target["name"]
        doc.GetVariable.side_effect = lambda name: 4 if name == "INSUNITS" else 0
        other = Mock()
        other.HWND, other.Name = 2999, "另外打开的图纸.dwg"
        other.Path, other.FullName = str(self.root), str(self.root / other.Name)
        other.GetVariable.return_value = 0
        items = [doc] if opened else [other]
        documents = Mock()
        documents.Count = len(items)
        documents.Item.side_effect = lambda index: items[index]
        app = Mock()
        app.HWND, app.Documents, app.ActiveDocument = app_hwnd, documents, items[0]
        app.GetAcadState.return_value = SimpleNamespace(IsQuiescent=True)
        doc.Activate.side_effect = lambda: setattr(app, "ActiveDocument", doc)

        def open_original(path, readonly):
            self.assertEqual(path, str(self.source))
            self.assertFalse(readonly)
            if doc not in items:
                items.append(doc)
                documents.Count = len(items)
            app.ActiveDocument = doc
            return doc

        documents.Open.side_effect = open_original
        client = Mock()
        client.GetActiveObject.return_value = app
        client.DispatchEx.return_value = app
        pc = Mock()
        return SimpleNamespace(pc=pc, client=client, app=app, doc=doc, other=other,
                               documents=documents, items=items)

    def launch(self, session, com):
        def acknowledge(command):
            if "(c:CGPICK)" in command:
                session.event_path.write_bytes(b"READY\n")
        com.doc.SendCommand.side_effect = acknowledge
        with patch("coordtool.picker._com_modules", return_value=(com.pc, com.client)):
            session.launch()

    def assert_no_drawing_artifacts(self, session):
        self.assertEqual([path for path in session.directory.rglob("*")
                          if path.suffix.lower() in {".dwg", ".dxf"}], [])
        self.assertIsNone(session.seed_path)
        self.assertEqual(session.seed_entity_count, 0)

    def test_native_start_error_is_shown_in_chinese(self):
        session = self.attach()
        session.event_path.write_bytes(b"ERROR\tPlease activate modelspace before picking.\n")
        with self.assertRaisesRegex(PickerError, "请先切换到模型空间"):
            session._wait_ready()

    def assert_no_copy_or_save(self, com):
        com.documents.Add.assert_not_called()
        com.doc.CopyObjects.assert_not_called()
        com.other.CopyObjects.assert_not_called()
        com.doc.Save.assert_not_called()
        com.doc.SaveAs.assert_not_called()
        com.doc.Close.assert_not_called()

    @contextmanager
    def redirected_session_directory(self, physical_parent, *, same_parent):
        """Model MSIX resolving only the child into a different-looking path."""
        logical_parent = self.root / "sessions"
        physical_parent.mkdir()
        original_resolve, original_samefile = Path.resolve, Path.samefile

        def resolve(path, *args, **kwargs):
            result = original_resolve(path, *args, **kwargs)
            if path.parent == logical_parent:
                result = physical_parent / path.name
                result.mkdir(exist_ok=True)
            return result

        def samefile(path, other):
            if path == physical_parent and Path(other) == logical_parent:
                return same_parent
            return original_samefile(path, other)

        with patch.object(Path, "resolve", resolve), \
                patch.object(Path, "samefile", autospec=True, side_effect=samefile) as identity:
            yield logical_parent, identity

    def test_session_parent_alias_accepts_same_directory_despite_different_text_paths(self):
        physical_parent = self.root / "MSIX-LocalCache-picks"
        with self.redirected_session_directory(physical_parent, same_parent=True) as (base, identity):
            session = self.attach()
            self.assertNotEqual(session.directory.parent, base)
            self.assertFalse(session.directory.is_relative_to(base))
            self.assertEqual(session.directory.parent, physical_parent)
            identity.assert_any_call(physical_parent, base)
            self.assertTrue(session.lsp_path.is_file())
            self.assertEqual(load_project(session.recovery_path)[0], [])
        self.assert_no_drawing_artifacts(session)

    def test_session_resolving_to_unrelated_parent_is_rejected(self):
        physical_parent = self.root / "unrelated-directory"
        with self.redirected_session_directory(physical_parent, same_parent=False) as (base, identity):
            with self.assertRaisesRegex(PickerError, "工作目录校验失败"):
                self.attach()
            identity.assert_any_call(physical_parent, base)
        self.assertEqual(list(physical_parent.rglob("picker.lsp")), [])
        self.assertEqual(list(physical_parent.rglob("recovery.coordproj")), [])

    def test_open_selected_drawing_only_activates_and_sends_without_seeding(self):
        original = self.source.read_bytes()
        old_points = [Point("已有道路-01", 1., 2.)]
        with patch("coordtool.picker.shutil.copyfile") as copy, \
                patch("coordtool.cad.export_dxf") as export, patch("ezdxf.new") as new:
            session = self.attach(initial_points=old_points)
            com = self.fake_com()
            self.launch(session, com)
        copy.assert_not_called()
        export.assert_not_called()
        new.assert_not_called()
        com.documents.Open.assert_not_called()
        com.doc.Activate.assert_called_once()
        self.assertGreater(com.doc.SendCommand.call_count, 1)
        self.assert_no_copy_or_save(com)
        self.assert_no_drawing_artifacts(session)
        self.assertEqual(session.drawing_path, self.source)
        self.assertEqual(load_project(session.recovery_path)[0], old_points)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(session.poll(), [{"type": "READY"}])

    def test_unsaved_selected_document_does_not_require_or_create_a_disk_drawing(self):
        target = {**self.target, "name": "Drawing1.dwg", "path": None}
        session = self.attach(target=target)
        com = self.fake_com(target=target, saved=False)
        self.launch(session, com)
        com.documents.Open.assert_not_called()
        self.assert_no_copy_or_save(com)
        self.assert_no_drawing_artifacts(session)
        self.assertEqual(session.attached_target["doc_hwnd"], target["doc_hwnd"])
        self.assertIsNone(load_project(session.recovery_path)[2])
        self.assertEqual(session.poll(), [{"type": "READY"}])

    def test_closed_selected_document_does_not_fall_back_to_a_same_named_document(self):
        session = self.attach()
        com = self.fake_com()
        # A newly opened proxy may have the exact old filename, but not its HWND.
        com.doc.HWND = 7777
        with patch("coordtool.picker._com_modules", return_value=(com.pc, com.client)):
            with self.assertRaises(PickerError):
                session.launch()
        com.documents.Open.assert_not_called()
        com.doc.Activate.assert_not_called()
        com.doc.SendCommand.assert_not_called()
        com.client.DispatchEx.assert_not_called()
        self.assert_no_copy_or_save(com)

    def test_different_cad_application_never_receives_selected_drawing_commands(self):
        session = self.attach()
        com = self.fake_com(app_hwnd=9009)
        with patch("coordtool.picker._com_modules", return_value=(com.pc, com.client)):
            with self.assertRaises(PickerError):
                session.launch()
        com.documents.Open.assert_not_called()
        com.doc.Activate.assert_not_called()
        com.doc.SendCommand.assert_not_called()
        com.client.DispatchEx.assert_not_called()

    def test_disk_selection_opens_exact_original_file_without_a_working_copy(self):
        target = dict(path=str(self.source), name=self.source.name)
        session = self.attach(target=target)
        com = self.fake_com(opened=False)
        self.launch(session, com)
        com.documents.Open.assert_called_once_with(str(self.source), False)
        self.assertEqual(session.drawing_path, self.source)
        self.assert_no_drawing_artifacts(session)
        self.assert_no_copy_or_save(com)
        self.assertEqual(session.poll(), [{"type": "READY"}])

    def test_disk_selection_reuses_that_file_when_it_is_already_open(self):
        session = self.attach(target=dict(path=str(self.source), name=self.source.name))
        com = self.fake_com()
        self.launch(session, com)
        com.documents.Open.assert_not_called()
        com.doc.Activate.assert_called_once()
        self.assert_no_drawing_artifacts(session)
        self.assert_no_copy_or_save(com)

    def test_second_round_keeps_original_document_and_continues_without_duplicate_points(self):
        first = self.attach()
        com = self.fake_com()
        self.launch(first, com)
        points = first.apply([], first.poll() + [add(), {"type": "DONE"}])
        self.assertTrue(first.ready and first.done)
        com.doc.Layers.Item.side_effect = lambda name: SimpleNamespace(Name=first.native_prefix)
        com.doc.Groups.Item.side_effect = lambda name: SimpleNamespace(Count=6)
        second = PickerSession.resume(first, "田间道路", 2, self.settings,
                                      root=self.root / "sessions", initial_points=points)
        self.assertEqual(second.attached_target["app_hwnd"], self.target["app_hwnd"])
        self.assertEqual(second.attached_target["doc_hwnd"], self.target["doc_hwnd"])
        self.assertNotEqual(first.directory, second.directory)
        self.assertNotEqual(first.native_prefix, second.native_prefix)
        self.launch(second, com)
        points = second.apply(points, second.poll() + [add(index=2), {"type": "DONE"}])
        self.assertEqual([point.name for point in points], ["田间道路-01", "田间道路-02"])
        self.assertEqual(len({point.capture_id for point in points}), 2)
        self.assertTrue(second.ready and second.done)
        self.assertEqual(load_project(second.recovery_path)[0], points)
        self.assertEqual(second.drawing_path, self.source)
        com.documents.Open.assert_not_called()
        self.assertEqual(com.doc.Activate.call_count, 2)
        self.assert_no_copy_or_save(com)
        self.assert_no_drawing_artifacts(first)
        self.assert_no_drawing_artifacts(second)

    def test_existing_road_number_is_rejected_before_creating_a_session(self):
        for spelling in ("田间道路-01", "田间道路-1", "田间道路-0001"):
            with self.subTest(spelling=spelling), self.assertRaises(PickerError):
                self.attach(initial_points=[Point(spelling, 1., 2.)])
        self.assertFalse((self.root / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
