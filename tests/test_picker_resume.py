from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coordtool.core import Point, Settings
from coordtool.picker import PickerSession, PickerError
from coordtool.project import load_project
from tests import test_picker as support


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source.dxf'
        self.settings = Settings()
        self.initial = [Point(f'道路1-{i:02}', float(i), 20.) for i in range(1, 4)]
        self.previous = PickerSession.create(None, '道路1', 4, self.settings,
                                             root=self.root, initial_points=self.initial)
        self.points = self.previous.apply(self.initial, [dict(type='READY'),
            support.add(1, 4, road_id=0), dict(type='DONE')])

    def resume(self, **changes):
        args = dict(previous=self.previous, road='道路1', start=5, settings=self.settings,
                    root=self.root, initial_points=self.points)
        args.update(changes)
        return PickerSession.resume(**args)

    def fake_com(self, session, **kwargs):
        pc, client, app, doc = support.PickerTests.fake_com(self, session, **kwargs)
        doc.Layers.Item.side_effect = lambda name: SimpleNamespace(Name=self.previous.native_prefix)
        doc.Groups.Item.side_effect = lambda name: SimpleNamespace(Count=6)
        return pc, client, app, doc

    def test_new_journal_reuses_workfile_and_has_no_seed_or_drawing_copy(self):
        old_recovery = self.previous.recovery_path.read_bytes()
        old_drawing = self.previous.drawing_path.read_bytes()
        with patch('coordtool.picker.shutil.copyfile') as copy, patch('coordtool.cad.export_dxf') as export:
            session = self.resume()
        copy.assert_not_called()
        export.assert_not_called()
        self.assertEqual(session.drawing_path, self.previous.drawing_path)
        self.assertNotEqual(session.session_id, self.previous.session_id)
        self.assertNotEqual(session.native_prefix, self.previous.native_prefix)
        self.assertIsNone(session.seed_path)
        self.assertFalse(list(session.directory.glob('*.dxf')))
        self.assertFalse(session.event_path.exists())
        self.assertFalse(session.done)
        self.assertEqual(load_project(session.recovery_path)[0], self.points)
        self.assertEqual(self.previous.recovery_path.read_bytes(), old_recovery)
        self.assertEqual(self.previous.drawing_path.read_bytes(), old_drawing)

    def test_resume_add_and_undo_preserve_previous_round_points(self):
        session = self.resume()
        first = session.apply(self.points, [dict(type='READY'), support.add(1, 5, road_id=0)])
        self.assertEqual([p.name for p in first], [p.name for p in self.points]+['道路1-05'])
        self.assertEqual(first[:-1], self.points)
        self.assertNotEqual(first[-1].group_id, first[-2].group_id)
        undone = session.apply(first, [dict(type='UNDO', serial=1)])
        self.assertEqual(undone, self.points)
        final = session.apply(undone, [support.add(2, 5, road_id=0), dict(type='DONE')])
        self.assertEqual(load_project(session.recovery_path)[0], final)
        second = self.resume(previous=session, initial_points=final, start=6)
        self.assertEqual(second.drawing_path, self.previous.drawing_path)
        self.assertEqual(second.road_numbers['道路1'], 6)

    def test_incomplete_or_failed_previous_round_cannot_resume(self):
        for changes in (dict(done=False), dict(ready=False), dict(last_error='native error'), dict(native_prefix='')):
            with self.subTest(changes=changes), patch.multiple(self.previous, **changes):
                with self.assertRaisesRegex(PickerError, '尚未正常结束'):
                    self.resume()

    def test_modified_points_or_global_settings_are_rejected(self):
        for changes in (dict(initial_points=self.points[:-1]),
                        dict(initial_points=[replace(self.points[0], e=300.)]+self.points[1:]),
                        dict(settings=replace(self.settings, color=2))):
            with self.subTest(changes=changes), self.assertRaisesRegex(PickerError, '已在上轮结束后改变'):
                self.resume(**changes)
        # A new picker text height is allowed without changing the project settings.
        changed = self.resume(settings=replace(self.settings, text_height=3.), snapshot_settings=self.settings)
        self.assertEqual(changed.settings.text_height, 3.)
        self.assertEqual(changed.snapshot_settings, self.settings)

    def test_reuses_native_document_without_open_copy_or_new_application(self):
        session = self.resume()
        pc, client, app, doc = self.fake_com(session)
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            session.launch()
        app.Documents.Open.assert_not_called()
        doc.CopyObjects.assert_not_called()
        doc.Close.assert_not_called()
        client.DispatchEx.assert_not_called()
        doc.Activate.assert_called_once()
        self.assertEqual(session.poll(), [dict(type='READY')])
        pc.CoUninitialize.assert_called_once()

    def test_save_as_document_is_found_by_previous_native_layer(self):
        session = self.resume()
        pc, client, app, doc = self.fake_com(session)
        new_path = self.root/'另存后的工作图.dwg'
        doc.FullName = str(new_path)
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            session.launch()
        self.assertEqual(session.drawing_path, new_path.resolve())
        self.assertEqual(json.loads((session.directory/'session.json').read_text(encoding='utf-8'))['drawing_path'], str(new_path.resolve()))
        app.Documents.Open.assert_not_called()

    def test_missing_or_ambiguous_drawing_does_not_open_or_send(self):
        for ambiguous in (False, True):
            session = self.resume()
            pc, client, app, doc = self.fake_com(session)
            if ambiguous:
                app.Documents.Count = 2
            else:
                doc.Layers.Item.side_effect = KeyError('no previous layer')
                doc.Layers.Count = 0
            with patch('coordtool.picker._com_modules', return_value=(pc, client)):
                with self.assertRaisesRegex(PickerError, '唯一找到'):
                    session.launch()
            app.Documents.Open.assert_not_called()
            doc.SendCommand.assert_not_called()
            doc.Activate.assert_not_called()

    def test_deleted_previous_annotation_blocks_resume(self):
        session = self.resume()
        pc, client, app, doc = self.fake_com(session)
        doc.Groups.Item.side_effect = lambda name: SimpleNamespace(Count=5)
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, '标注已改变'):
                session.launch()
        doc.SendCommand.assert_not_called()

    def test_closed_cad_is_not_relaunched_for_resume(self):
        session = self.resume()
        pc, client, app, doc = self.fake_com(session)
        client.GetActiveObject.side_effect = support.ComFailure(0x800401E3)
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, '原 AutoCAD 已关闭'):
                session.launch()
        client.DispatchEx.assert_not_called()
        app.Documents.Open.assert_not_called()

    def test_requested_stop_after_ready_does_not_turn_completed_round_into_launch_error(self):
        session = self.resume()
        pc, client, app, doc = self.fake_com(session)
        def stopped(command):
            if '(c:CGPICK)' in command:
                session.event_path.write_text('READY\nDONE\n', encoding='ascii')
                session.request_stop()
        doc.SendCommand.side_effect = stopped
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            session.launch()
        points = session.apply(self.points, session.poll())
        self.assertTrue(session.done)
        self.assertEqual(session.last_error, '')
        self.assertEqual(self.resume(previous=session, initial_points=points).road_numbers['道路1'], 5)

    def test_busy_cad_or_changed_document_is_not_interrupted(self):
        for busy in (True, False):
            session = self.resume()
            pc, client, app, doc = self.fake_com(session, busy=busy)
            if not busy:
                doc.SendCommand.side_effect = lambda _: setattr(app, 'ActiveDocument', SimpleNamespace(FullName='other.dwg'))
            with patch('coordtool.picker._com_modules', return_value=(pc, client)):
                with self.assertRaises(PickerError):
                    session.launch()
            app.Documents.Open.assert_not_called()
            if busy:
                doc.SendCommand.assert_not_called()
            else:
                doc.SendCommand.assert_called_once()


if __name__ == '__main__':
    unittest.main()
