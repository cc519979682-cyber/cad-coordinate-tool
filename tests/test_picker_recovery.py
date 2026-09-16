"""Recovery must validate the saved prefix before replaying durable CAD events."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordtool.core import Placement, Point, Settings
from coordtool.picker import PickerError, PickerSession
from coordtool.project import load_project
from tests.test_picker import add


def event_line(event):
    """Write the public native protocol without using the recovery decoder."""
    kind = event['type']
    if kind in ('READY', 'DONE'):
        values = [kind]
    elif kind == 'ADD':
        values = ['ADD'] + [event[key] for key in
                           ('serial', 'index', 'x', 'y', 'z', 'lx', 'ly', 'hx', 'height', 'arrow')]
        if 'road_id' in event:
            values.append(event['road_id'])
    elif kind == 'ROAD':
        values = ['ROAD', event['road_id'], event['start'],
                  ','.join(str(ord(character)) for character in event['road'])]
    elif kind == 'UNDO':
        values = ['UNDO', event['serial']]
    else:
        raise AssertionError('test fixture uses an unsupported event')
    return '\t'.join(map(str, values)) + '\n'


class PickerRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / '恢复测试原图.dwg'
        self.source.write_bytes(b'AC1032 recovery must not rewrite this drawing\x00')
        self.settings = Settings(scale=1000., offset_e=1., offset_n=-2., text_height=2.)
        self.snapshot_settings = replace(self.settings, text_height=1.)
        self.initial = [Point('田间道路-06', 10., 20., -1.),
                        Point('支路-04', 30., 40., capture_id='older:9', group_id='older')]
        self.target = dict(app_hwnd=1001, doc_hwnd=2001, name=self.source.name,
                           path=str(self.source), units=4, active=True)
        self.events = [dict(type='READY'), add(1, 7),
                       dict(type='ROAD', road_id=1, road='支路', start=5),
                       add(2, 5, road_id=1, x=102000.),
                       dict(type='UNDO', serial=2),
                       add(3, 5, road_id=1, x=103000.), dict(type='DONE')]

    def attach(self, initial=None, **changes):
        options = dict(target=self.target, road='田间道路', start=7,
                       settings=self.settings, snapshot_settings=self.snapshot_settings,
                       initial_points=self.initial if initial is None else initial,
                       root=self.root / 'sessions')
        options.update(changes)
        # Preparing a recovery fixture must never discover or connect to CAD.
        with patch('coordtool.picker._com_modules', side_effect=AssertionError('unexpected COM access')):
            return PickerSession.attach(**options)

    def journal(self, session, events=None, tail=b''):
        text = ''.join(map(event_line, self.events if events is None else events))
        session.event_path.write_bytes(text.encode('ascii') + tail)

    def fingerprint(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()}

    def recover(self, path, **options):
        from coordtool.picker import recover_picker_snapshot
        with patch('coordtool.picker._com_modules', side_effect=AssertionError('unexpected COM access')):
            return recover_picker_snapshot(path, **options)

    def expected(self, session):
        placement = Placement(105., -16., 115., 2., 1.)
        return self.initial + [
            Point('田间道路-07', 100., -20., -3.25, placement,
                  f'{session.session_id}:1', session.session_id),
            Point('支路-05', 102., -20., -3.25, placement,
                  f'{session.session_id}:3', f'{session.session_id}:road:1')]

    def edit_json(self, path, change):
        payload = json.loads(path.read_text(encoding='utf-8'))
        change(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def assert_rejected_without_writes(self, path):
        before = self.fingerprint()
        with self.assertRaises(PickerError):
            self.recover(path)
        self.assertEqual(self.fingerprint(), before)

    def test_tail_replay_keeps_initial_points_road_numbering_and_is_read_only_idempotent(self):
        session = self.attach()
        session.apply(self.initial, self.events[:2])
        self.journal(session)
        before = self.fingerprint()
        warnings = []
        result = self.recover(session.recovery_path, warnings=warnings)
        self.assertEqual(result[0], self.expected(session))
        self.assertEqual(result[1], self.snapshot_settings)
        self.assertEqual(result[2], load_project(session.recovery_path)[2])
        self.assertEqual(len({point.capture_id for point in result[0] if point.capture_id}), 3)
        self.assertEqual(warnings, [])
        self.assertEqual(self.recover(session.recovery_path), result)
        self.assertEqual(self.fingerprint(), before)

    def test_tail_undo_removes_point_already_present_in_snapshot(self):
        session = self.attach()
        snapshot_points = session.apply(self.initial, self.events[:4])
        self.assertEqual(snapshot_points[-1].capture_id, f'{session.session_id}:2')
        self.journal(session)
        before = self.fingerprint()
        recovered, _, _ = self.recover(session.recovery_path)
        self.assertEqual(recovered, self.expected(session))
        self.assertNotIn(f'{session.session_id}:2', [point.capture_id for point in recovered])
        self.assertEqual(self.fingerprint(), before)

    def test_initial_empty_session_without_journal_can_recover_zero_points(self):
        session = self.attach(initial=[])
        self.assertFalse(session.event_path.exists())
        before = self.fingerprint()
        points, settings, _ = self.recover(session.recovery_path)
        self.assertEqual(points, [])
        self.assertEqual(settings, self.snapshot_settings)
        self.assertEqual(self.fingerprint(), before)

    def test_initial_session_without_journal_preserves_preexisting_points(self):
        session = self.attach()
        self.assertFalse(session.event_path.exists())
        self.assertEqual(self.recover(session.recovery_path)[0], self.initial)

    def test_picker_snapshot_requires_its_session_metadata(self):
        session = self.attach()
        (session.directory / 'session.json').unlink()
        self.assert_rejected_without_writes(session.recovery_path)

    def test_session_metadata_with_foreign_session_id_is_rejected(self):
        session = self.attach()
        self.edit_json(session.directory / 'session.json',
                       lambda payload: payload.update(session_id='0' * 32))
        self.assert_rejected_without_writes(session.recovery_path)

    def test_snapshot_with_foreign_session_id_is_rejected(self):
        session = self.attach()
        self.edit_json(session.recovery_path,
                       lambda payload: payload['picker'].update(session_id='0' * 32))
        self.assert_rejected_without_writes(session.recovery_path)

    def test_snapshot_cannot_redirect_to_another_real_session_directory(self):
        first, other = self.attach(), self.attach()
        self.edit_json(first.recovery_path,
                       lambda payload: payload['picker'].update(directory=str(other.directory)))
        self.assert_rejected_without_writes(first.recovery_path)

    def test_conflicting_capture_settings_are_rejected(self):
        session = self.attach()
        self.edit_json(session.directory / 'session.json',
                       lambda payload: payload['settings'].update(offset_e=8.))
        self.assert_rejected_without_writes(session.recovery_path)

    def test_conflicting_snapshot_settings_are_rejected(self):
        session = self.attach()
        self.edit_json(session.recovery_path,
                       lambda payload: payload['settings'].update(text_height=9.))
        self.assert_rejected_without_writes(session.recovery_path)

    def test_complete_corrupt_journal_line_must_not_fall_back_to_snapshot(self):
        session = self.attach()
        session.apply(self.initial, self.events[:2])
        self.journal(session, self.events[:2], tail=b'ADD\tnot-a-valid-event\n')
        self.assert_rejected_without_writes(session.recovery_path)

    def test_unterminated_tail_is_ignored_with_warning_without_changing_source(self):
        session = self.attach()
        session.apply(self.initial, self.events[:2])
        self.journal(session, tail=b'ADD\t4\t6\t103')
        before = self.fingerprint()
        warnings = []
        result = self.recover(session.recovery_path, warnings=warnings)
        self.assertEqual(result[0], self.expected(session))
        self.assertTrue(warnings)
        self.assertTrue(all(isinstance(warning, str) and warning.strip() for warning in warnings))
        self.assertEqual(self.fingerprint(), before)

    def test_snapshot_ahead_of_truncated_journal_is_rejected(self):
        session = self.attach()
        session.apply(self.initial, self.events[:4])
        self.journal(session, self.events[:2])
        self.assert_rejected_without_writes(session.recovery_path)

    def test_snapshot_ahead_of_missing_journal_is_rejected(self):
        session = self.attach()
        session.apply(self.initial, self.events[:2])
        self.assertFalse(session.event_path.exists())
        self.assert_rejected_without_writes(session.recovery_path)

    def test_snapshot_point_conflicting_with_journal_prefix_is_rejected(self):
        session = self.attach()
        session.apply(self.initial, self.events[:2])
        self.journal(session)
        self.edit_json(session.recovery_path,
                       lambda payload: payload['points'][-1].update(e=999.))
        self.assert_rejected_without_writes(session.recovery_path)

    def test_ordinary_saved_project_uses_project_loader_without_picker_metadata(self):
        path = self.root / '普通坐标项目.coordproj'
        payload = dict(format='coordtool-project', version=1,
                       points=[asdict(point) for point in self.initial],
                       settings=asdict(self.snapshot_settings), drawing=self.source.name)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        before = self.fingerprint()
        self.assertEqual(self.recover(path), load_project(path))
        self.assertEqual(self.fingerprint(), before)


if __name__ == '__main__':
    unittest.main()
