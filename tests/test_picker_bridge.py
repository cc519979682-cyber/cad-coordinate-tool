from datetime import datetime, timedelta
from pathlib import Path
import errno
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

from coordtool.core import Point, Settings
from coordtool.picker import PickerSession, PickerError
from coordtool.picker_bridge import ZBBridge
from tests import test_picker as support


def request(token, road):
    return 'ZB1\t' + token + '\t' + ','.join(str(ord(c)) for c in road) + '\n'


def locked_file():
    error = PermissionError(13, 'file is briefly open in CAD')
    error.winerror = 32
    return error


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bridge = ZBBridge(self.directory, 'test-session-identifier')

    def send(self, name):
        self.bridge.request_path.write_text(request(self.bridge.token, name), encoding='ascii')

    def test_lease_is_fresh_ascii_and_throttled(self):
        with patch('coordtool.picker_bridge.time.monotonic', return_value=100.):
            self.bridge.publish()
            before = self.bridge.ready_path.read_bytes()
            marker, token, expiry = before.decode('ascii').splitlines()
            self.assertEqual((marker, token), ('CGP_ZB_V1', self.bridge.token))
            deadline = datetime.strptime(expiry, '%Y%m%d.%H%M%S')
            self.assertGreater(deadline, datetime.now() + timedelta(seconds=9))
            with patch('coordtool.picker_bridge.os.replace') as replace:
                self.bridge.publish()
                replace.assert_not_called()
        with patch('coordtool.picker_bridge.time.monotonic', return_value=103.):
            with patch('coordtool.picker_bridge.os.replace', wraps=__import__('os').replace) as replace:
                self.bridge.publish()
                replace.assert_called_once()

    def test_chinese_road_request_is_claimed_once_and_never_republished(self):
        self.bridge.publish()
        name = '道路2-东侧“引道”'
        self.send(name)
        self.assertEqual(self.bridge.take_request(), name)
        self.assertFalse(self.bridge.ready_path.exists())
        self.assertFalse(self.bridge.request_path.exists())
        self.assertTrue((self.directory/'zb.request.handled').is_file())
        self.assertIsNone(self.bridge.take_request())
        self.bridge.publish()
        self.assertFalse(self.bridge.ready_path.exists())

    def test_unfinished_temporary_request_is_ignored(self):
        self.bridge.publish()
        (self.directory/'zb.request.tmp').write_text('ZB1\tpartial', encoding='ascii')
        self.assertIsNone(self.bridge.take_request())
        self.assertTrue(self.bridge.ready_path.exists())

    def test_revoked_permission_rejects_pending_request(self):
        self.bridge.publish()
        self.send('道路2')
        self.bridge.disable()
        with self.assertRaisesRegex(ValueError, '已失效'):
            self.bridge.take_request()
        self.assertIsNone(self.bridge.take_request())

    def test_malformed_or_foreign_request_is_never_retried(self):
        invalid = [request('other-session', '道路2'),
                   request(self.bridge.token, ''),
                   request(self.bridge.token, '  路'),
                   request(self.bridge.token, '路\t二'),
                   request(self.bridge.token, '路\x85二'),
                   request(self.bridge.token, '路\ud800'),
                   request(self.bridge.token, '路'*101),
                   'ZB1\t'+self.bridge.token+'\t1114112\n',
                   request(self.bridge.token, '路') + 'EXTRA\n',
                   request(self.bridge.token, '路').rstrip('\n'),
                   'ZB1\t'+self.bridge.token+'\t'+'1'*4100+'\n']
        for i, data in enumerate(invalid):
            with self.subTest(i=i):
                path = self.directory/str(i)
                path.mkdir()
                bridge = ZBBridge(path, self.bridge.token)
                bridge.publish()
                bridge.request_path.write_text(data, encoding='ascii')
                with self.assertRaises(ValueError):
                    bridge.take_request()
                self.assertIsNone(bridge.take_request())
                self.assertFalse(bridge.ready_path.exists())

    def test_road_with_code_like_punctuation_is_only_returned_as_text(self):
        self.bridge.publish()
        name = '(道路2); "A" \\ B'
        self.send(name)
        self.assertEqual(self.bridge.take_request(), name)

    def test_temporary_ready_read_lock_defers_refresh_without_disabling_bridge(self):
        with patch('coordtool.picker_bridge.os.replace', side_effect=locked_file()):
            self.bridge.publish()
        self.assertFalse(self.bridge.ready_path.exists())
        self.bridge.publish()
        self.send('道路2')
        self.assertEqual(self.bridge.take_request(), '道路2')

    def test_request_claim_lock_keeps_request_for_next_tick(self):
        self.bridge.publish()
        self.send('道路2')
        with patch('coordtool.picker_bridge.os.replace', side_effect=locked_file()):
            self.assertIsNone(self.bridge.take_request())
        self.assertTrue(self.bridge.request_path.exists())
        self.assertTrue(self.bridge.ready_path.exists())
        self.assertEqual(self.bridge.take_request(), '道路2')
        self.assertIsNone(self.bridge.take_request())

    def test_lease_unlink_lock_does_not_lose_claimed_road_or_reenable(self):
        self.bridge.publish()
        self.send('道路2')
        with patch.object(Path, 'unlink', side_effect=locked_file()):
            self.assertEqual(self.bridge.take_request(), '道路2')
        self.bridge.publish()
        self.assertIsNone(self.bridge.take_request())
        self.bridge.disable()
        self.assertFalse(self.bridge.ready_path.exists())

    def test_claimed_read_lock_retries_original_request_exactly_once(self):
        for index, error in enumerate((locked_file(), PermissionError(errno.EACCES, 'locked'))):
            with self.subTest(error=error):
                directory = self.directory / str(index)
                directory.mkdir()
                bridge = ZBBridge(directory, self.bridge.token)
                bridge.publish()
                deadline = bridge._expires_at
                bridge.request_path.write_text(request(bridge.token, '原道路'), encoding='ascii')
                with patch.object(Path, 'open', side_effect=error):
                    self.assertIsNone(bridge.take_request())
                self.assertFalse(bridge.ready_path.exists())
                self.assertFalse(bridge.request_path.exists())
                self.assertFalse(bridge._consumed)
                # A second file must not overwrite the already claimed road.
                bridge.request_path.write_text(request(bridge.token, '后来的道路'), encoding='ascii')
                bridge.publish()
                self.assertFalse(bridge.ready_path.exists())
                self.assertEqual(bridge._expires_at, deadline)
                with patch('coordtool.picker_bridge.os.replace') as replace:
                    self.assertEqual(bridge.take_request(), '原道路')
                    self.assertIsNone(bridge.take_request())
                    replace.assert_not_called()
                bridge.publish()
                self.assertFalse(bridge.ready_path.exists())

    def test_disable_revokes_claimed_request_even_after_read_lock_clears(self):
        self.bridge.publish()
        self.send('道路2')
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'locked')):
            self.assertIsNone(self.bridge.take_request())
        self.bridge.disable()
        self.bridge.publish()
        with self.assertRaisesRegex(ValueError, '已失效'):
            self.bridge.take_request()
        self.assertIsNone(self.bridge.take_request())
        self.assertFalse(self.bridge.ready_path.exists())

    def test_claimed_request_expires_without_refresh_or_endless_permission_retry(self):
        self.bridge.publish()
        self.send('道路2')
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'denied')):
            self.assertIsNone(self.bridge.take_request())
        deadline = self.bridge._expires_at
        with patch('coordtool.picker_bridge.time.monotonic', return_value=deadline + 1):
            self.bridge.publish()
            with patch.object(Path, 'open') as read:
                with self.assertRaisesRegex(ValueError, '已失效'):
                    self.bridge.take_request()
                read.assert_not_called()
        self.assertIsNone(self.bridge.take_request())
        self.assertEqual(self.bridge._expires_at, deadline)
        self.assertFalse(self.bridge.ready_path.exists())

    def test_request_expiring_during_read_never_returns_a_road(self):
        self.bridge.publish()
        self.send('道路2')
        deadline = self.bridge._expires_at
        with patch('coordtool.picker_bridge.time.monotonic', side_effect=[deadline - 1, deadline + 1]):
            with self.assertRaisesRegex(ValueError, '已失效'):
                self.bridge.take_request()
        self.assertIsNone(self.bridge.take_request())

    def test_foreign_request_is_still_rejected_once_after_read_lock(self):
        self.bridge.publish()
        self.bridge.request_path.write_text(request('foreign-token', '道路2'), encoding='ascii')
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'locked')):
            self.assertIsNone(self.bridge.take_request())
        with self.assertRaisesRegex(ValueError, '不属于当前会话'):
            self.bridge.take_request()
        self.assertIsNone(self.bridge.take_request())
        self.bridge.publish()
        self.assertFalse(self.bridge.ready_path.exists())

    def test_non_permission_read_failure_remains_terminal(self):
        self.bridge.publish()
        self.send('道路2')
        with patch.object(Path, 'open', side_effect=OSError(errno.EIO, 'read failed')):
            with self.assertRaises(OSError):
                self.bridge.take_request()
        self.assertIsNone(self.bridge.take_request())
        self.bridge.publish()
        self.assertFalse(self.bridge.ready_path.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows file sharing')
    def test_native_windows_read_lock_without_winerror_can_retry_after_claim(self):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.bridge.publish()
        self.send('道路2')
        # Allow rename/delete but temporarily deny a new reader, as can happen
        # when a scanner opens the completed request between CAD and the UI.
        handle = create(str(self.bridge.request_path), 0x80000000, 0x4, None, 3, 0, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value, ctypes.get_last_error())
        try:
            self.assertIsNone(self.bridge.take_request())
            claimed = self.directory / 'zb.request.handled'
            self.assertTrue(claimed.exists())
            with self.assertRaises(PermissionError) as raised:
                claimed.read_bytes()
            self.assertEqual(raised.exception.errno, errno.EACCES)
            self.assertIsNone(getattr(raised.exception, 'winerror', None))
        finally:
            kernel32.CloseHandle(handle)
        self.assertEqual(self.bridge.take_request(), '道路2')
        self.assertIsNone(self.bridge.take_request())


class SessionZBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings()
        self.previous = PickerSession.create(None, '道路1', 1, self.settings, root=self.root)
        self.points = self.previous.apply([], [dict(type='READY'), support.add(1, 1), dict(type='DONE')])

    def resume(self, name='道路2'):
        return PickerSession.resume(self.previous, name, self.previous.next_road_start(name, self.points),
                                    self.settings, root=self.root, initial_points=self.points)

    def fake_com(self, session, **kwargs):
        pc, client, app, doc = support.PickerTests.fake_com(self, session, **kwargs)
        doc.Layers.Item.side_effect = lambda name: SimpleNamespace(Name=self.previous.native_prefix)
        doc.Groups.Item.side_effect = lambda name: SimpleNamespace(Count=6)
        return pc, client, app, doc

    def test_only_completed_successful_session_can_publish(self):
        for updates in (dict(done=False), dict(ready=False), dict(last_error='failure'), dict(native_prefix='')):
            with self.subTest(updates=updates), patch.multiple(self.previous, **updates):
                self.previous.publish_zb_ready()
                self.assertFalse(self.previous.zb_bridge.ready_path.exists())
        self.previous.publish_zb_ready()
        self.assertTrue(self.previous.zb_bridge.ready_path.exists())

    def test_new_road_starts_one_and_known_road_uses_max_retained_index(self):
        points = self.points + [Point('道路1-08', 1., 1.), Point('AbC-11', 2., 2.)]
        self.assertEqual(self.previous.next_road_start('道路2', points), 1)
        self.assertEqual(self.previous.next_road_start('道路1', points), 9)
        self.assertEqual(self.previous.next_road_start('abc', points), 12)

    def test_bridge_request_yields_new_session_and_distinct_capture_ids(self):
        self.previous.publish_zb_ready()
        self.previous.zb_bridge.request_path.write_text(request(self.previous.session_id, '道路2'), encoding='ascii')
        road = self.previous.take_zb_request()
        new = self.resume(road)
        self.assertEqual(new.drawing_path, self.previous.drawing_path)
        self.assertIsNone(new.seed_path)
        new_points = new.apply(self.points, [dict(type='READY'), support.add(1, 1), dict(type='DONE')])
        self.assertEqual([p.name for p in new_points], ['道路1-01', '道路2-01'])
        self.assertNotEqual(new_points[0].capture_id, new_points[1].capture_id)
        new.publish_zb_ready()
        self.assertTrue(new.zb_bridge.ready_path.exists())
        self.assertFalse(self.previous.zb_bridge.ready_path.exists())

    def test_zb_handoff_waits_for_idle_without_reopening_or_cancelling(self):
        session = self.resume()
        session.wait_for_idle = True
        pc, client, app, doc = self.fake_com(session)
        class AcadState:
            pass
        state = AcadState()
        app.GetAcadState.return_value = state
        with patch.object(type(state), 'IsQuiescent', new_callable=PropertyMock, create=True) as idle:
            idle.side_effect = [False, True, True]
            with patch('coordtool.picker._com_modules', return_value=(pc, client)), patch('coordtool.picker.time.sleep'):
                session.launch()
        app.Documents.Open.assert_not_called()
        self.assertTrue(doc.SendCommand.called)
        self.assertFalse(any('\x03' in call.args[0] for call in doc.SendCommand.call_args_list))

    def test_other_busy_command_is_not_interrupted(self):
        session = self.resume()
        session.wait_for_idle = True
        session.ZB_IDLE_TIMEOUT = 0
        pc, client, app, doc = self.fake_com(session, busy=True)
        with patch('coordtool.picker._com_modules', return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, '未结束的命令'):
                session.launch()
        doc.SendCommand.assert_not_called()
        app.Documents.Open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
