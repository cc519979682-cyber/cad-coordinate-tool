"""Real journal/session -> UI polling checks while native CAD waits in a menu.

The widget shell and timer are inert; production poll/apply/recovery code runs.
No CAD process, COM connection, Tk window, or original user file is opened.
Native Enter/N/C/Q/Esc handling is covered separately by the AutoCAD trial.
"""
from concurrent.futures import Future
import json
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest.mock import Mock, patch

from coordtool.core import Point, Settings, calc_labels, connection_groups
from coordtool.picker import PickerSession
from coordtool.picker_ui import PickerUIMixin
from coordtool.project import load_project
from coordtool.ui import CoordinateApp


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class PollShell(PickerUIMixin):
    """Minimal widget shell using both production UI polling methods."""

    _poll = CoordinateApp._poll

    def __init__(self, session, initial):
        self.root = Mock()
        self.tree = Mock()
        self.error = Mock()
        self.closed = False
        self.loading_drawing = False
        self.settings = lambda: session.snapshot_settings
        self.picker_previous = None
        self.picker_zb_blocked_session = None
        self.drawing_revision = 0
        self.drawing_progress = Queue()
        self.results = Queue()
        self.points = initial[:]
        self.base_path = session.original_path
        self.base_bounds = (-60000000., -15000000., -50000000., -12000000.)
        self.picker_before = initial[:]
        self.picker_settings = session.settings
        self.picker_session = session
        self.picker_starting = False
        self.picker_discovering = False
        self.picker_dialog = None
        self.picker_road = session.current_road
        self.picker_started_at = 0.
        self.picker_polled_at = 0.
        self.picker_stop_requested = False
        self.picker_text = Value()
        self.picker_button_text = Value("结束取点")
        self.status = Value()
        self.undo = []
        self.changes = []

    def _changed_points(self, points, action, *, record, fit, from_picker):
        self.changes.append((action, record, fit, from_picker))
        self.points = list(points)


class RoadMenuFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coord-road-menu-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "owned-fixture.dwg"
        self.source.write_bytes(b"AC1032 owned road menu test fixture")
        self.settings = Settings(scale=1000., offset_e=10., offset_n=-20.)
        self.initial = [Point(f"旧控制点{i:02d}", -52500.-i*1.25,
                              -13200.-i*.8, -3.+i*.01) for i in range(1, 33)]
        self.session = self.create(self.initial)
        self.app = PollShell(self.session, self.initial)
        self.tick = 10.

    def create(self, points, road="道路1", start=1):
        return PickerSession.create(self.source, road, start, self.settings,
                                    root=self.directory / "sessions", initial_points=points)

    def add(self, serial, index, segment=0):
        e, n = -53000.-serial*2., -13300.-serial*3.
        x = (e+self.settings.offset_e)*self.settings.scale
        y = (n+self.settings.offset_n)*self.settings.scale
        return "\t".join(map(str, ["ADD", serial, index, x, y, -3250.,
                                    x+5000., y+4000., x+15000., 2000., 1000., segment]))

    @staticmethod
    def road(segment, name, start):
        return f"ROAD\t{segment}\t{start}\t" + ",".join(str(ord(c)) for c in name)

    def append(self, *lines, complete=True):
        data = "\n".join(lines) + ("\n" if complete else "")
        with self.session.event_path.open("ab") as stream:
            stream.write(data.encode("ascii"))

    def poll(self, delta=1.):
        self.tick += delta
        with patch("coordtool.picker_ui.monotonic", return_value=self.tick):
            self.app._poll()

    def three_points_then_menu(self):
        # Enter/menu waiting emits nothing: the last ADD can still be unread.
        self.append("READY", self.add(1, 1), self.add(2, 2), self.add(3, 3))
        self.poll()
        self.assertEqual(len(self.app.points), 35)
        self.assertFalse(self.session.done)
        return self.app.points[:]

    def recovery(self):
        return json.loads(self.session.recovery_path.read_text(encoding="utf-8"))

    def test_32_old_points_three_first_road_menu_new_road_and_return_to_04(self):
        app, session = self.app, self.session
        first = self.three_points_then_menu()
        recovery_before = session.recovery_path.read_bytes()
        changes_before = app.changes[:]
        for elapsed in (1., 61., 3600.):
            self.poll(elapsed)
            self.assertIs(app.picker_session, session)
            self.assertTrue(app._picker_active())
            self.assertEqual(app.points, first)
            self.assertFalse(session.done)
            self.assertEqual(app.undo, [])
        self.assertEqual(session.recovery_path.read_bytes(), recovery_before)
        self.assertEqual(app.changes, changes_before)
        self.assertTrue(app._picker_guard(), "The menu still belongs to the active session")

        self.append(self.road(1, "道路2", 1))
        self.poll()
        self.assertEqual(app.picker_road, "道路2")
        self.assertEqual(app.points, first)
        self.assertEqual(app.changes, changes_before, "A road-only event needs no geometry redraw")
        self.assertFalse(self.recovery()["picker"]["done"])
        self.append(self.add(4, 1, 1))
        self.poll()
        self.append(self.road(2, "道路1", 4), self.add(5, 4, 2))
        self.poll()
        self.assertEqual([p.name for p in app.points[32:]],
                         ["道路1-01", "道路1-02", "道路1-03", "道路2-01", "道路1-04"])
        self.assertEqual(app.points[:32], self.initial)
        self.assertTrue(all(a is b for a, b in zip(app.points[:32], self.initial)))
        self.assertEqual([p.capture_id for p in app.points[32:]],
                         [f"{session.session_id}:{i}" for i in range(1, 6)])
        self.assertEqual(len({p.group_id for p in app.points[32:]}), 3)
        labels = calc_labels(app.points[32:], self.settings)
        self.assertEqual([len(g) for g in connection_groups(app.points[32:], labels)], [3, 1, 1])
        self.assertTrue(all(change == ("CAD 取点", False, False, True) for change in app.changes))
        self.assertIn("已回传 5 个点", app.picker_text.get())
        self.assertEqual(load_project(session.recovery_path)[0], app.points)
        self.append("DONE")
        self.poll()
        self.assertFalse(app._picker_active())
        self.assertTrue(session.done)
        self.assertEqual(app.undo, [("CAD 取点：道路1", self.initial)])
        self.assertEqual(len(app.points), 37)
        self.assertEqual(self.source.read_bytes(), b"AC1032 owned road menu test fixture")
        app.error.assert_not_called()

    def test_menu_silence_allows_pending_add_and_partial_line_to_finish(self):
        self.append("READY", self.add(1, 1))
        self.append(self.add(2, 2), complete=False)
        self.poll()
        self.assertEqual(len(self.app.points), 33)
        self.assertEqual(self.recovery()["picker"]["last_serial"], 1)
        self.poll(300.)
        self.assertEqual(len(self.app.points), 33)
        self.assertTrue(self.app._picker_active())
        with self.session.event_path.open("ab") as stream:
            stream.write(b"\n")
        self.poll()
        self.assertEqual([p.name for p in self.app.points[-2:]], ["道路1-01", "道路1-02"])
        self.assertEqual(self.recovery()["picker"]["last_serial"], 2)
        self.assertFalse(self.session.done)
        self.app.error.assert_not_called()

    def test_continue_current_after_silent_menu_keeps_undo_stack_and_serials(self):
        self.three_points_then_menu()
        # C and cancelled new-road input have no journal event. Native tests
        # verify the keystrokes; here silence must preserve the consumer state.
        self.poll(600.)
        self.assertEqual(self.recovery()["picker"]["active"], [1, 2, 3])
        self.append("UNDO\t3", self.add(4, 3), self.add(5, 4))
        self.poll()
        self.assertEqual([p.name for p in self.app.points[32:]],
                         ["道路1-01", "道路1-02", "道路1-03", "道路1-04"])
        self.assertEqual([p.capture_id.rsplit(":", 1)[1] for p in self.app.points[32:]],
                         ["1", "2", "4", "5"])
        self.assertEqual(len({p.group_id for p in self.app.points[32:]}), 1)
        state = self.recovery()["picker"]
        self.assertEqual(state["current_segment"], 0)
        self.assertEqual(state["active"], [1, 2, 4, 5])
        self.assertEqual(state["undone"], [3])
        self.assertEqual(state["last_serial"], 5)
        self.assertEqual(self.app.points[:32], self.initial)
        self.assertFalse(self.session.done)

    def test_stop_from_menu_waits_for_complete_done_and_drains_final_add(self):
        self.three_points_then_menu()
        self.app.stop_picker()
        self.assertEqual(self.session.stop_path.read_text(encoding="ascii"), "STOP\n")
        self.assertTrue(self.app.picker_stop_requested)
        self.poll(3600.)
        self.assertIs(self.app.picker_session, self.session)
        self.assertFalse(self.session.done)
        self.assertEqual(self.app.undo, [])
        # A confirmed ADD may reach the reader just before the final reply.
        self.append(self.add(4, 4))
        self.append("DO", complete=False)
        self.poll()
        self.assertEqual(len(self.app.points), 36)
        self.assertFalse(self.session.done)
        with self.session.event_path.open("ab") as stream:
            stream.write(b"NE\n")
        self.poll()
        self.assertFalse(self.app._picker_active())
        self.assertEqual(self.app.points[:32], self.initial)
        self.assertEqual(self.app.points[-1].name, "道路1-04")
        self.assertEqual(load_project(self.session.recovery_path)[0], self.app.points)
        self.assertTrue(self.recovery()["picker"]["done"])
        self.assertEqual(self.app.undo, [("CAD 取点：道路1", self.initial)])
        self.app.error.assert_not_called()

    def test_timer_and_completed_background_jobs_keep_running_in_menu(self):
        self.three_points_then_menu()
        future, callback = Future(), Mock()
        future.set_result("independent completed job")
        self.app.results.put((future, callback))
        self.append(self.road(1, "道路2", 1), self.add(4, 1, 1))
        self.poll(.1)  # Normal 250 ms journal throttle still applies.
        callback.assert_called_once_with("independent completed job", None)
        self.assertEqual(self.app.picker_road, "道路1")
        self.assertEqual(len(self.app.points), 35)
        self.app.root.after.assert_called_with(80, self.app._poll)
        self.poll(.2)
        self.assertEqual(self.app.picker_road, "道路2")
        self.assertEqual(self.app.points[-1].name, "道路2-01")
        self.assertIs(self.app.picker_session, self.session)
        self.assertFalse(self.session.done)

    def test_menu_recovery_keeps_geometry_and_existing_points_for_next_session(self):
        self.three_points_then_menu()
        points, settings, drawing = load_project(self.session.recovery_path)
        self.assertEqual(points, self.app.points)
        self.assertEqual(settings, self.settings)
        self.assertTrue(Path(drawing).samefile(self.source))
        first = points[32]
        self.assertEqual((first.e, first.n, first.z), (-53002., -13303., -3.25))
        self.assertEqual((first.placement.e, first.placement.n,
                          first.placement.horizontal_e, first.placement.height,
                          first.placement.arrow), (-52997., -13299., -52987., 2., 1.))
        self.assertFalse(self.recovery()["picker"]["done"])
        # Project recovery restores coordinates; a fresh CAD session then uses
        # their numbering baseline rather than pretending to resume native input.
        following = self.create(points, road="道路2")
        self.assertEqual(following.road_numbers["道路1"], 4)
        self.assertEqual(following.road_numbers["道路2"], 1)
        self.assertEqual(load_project(following.recovery_path)[0], points)
        self.assertNotEqual(following.session_id, self.session.session_id)
        self.assertEqual(points[:32], self.initial)

    def test_failed_post_menu_batch_preserves_prior_snapshot_and_raw_journal(self):
        first = self.three_points_then_menu()
        before = self.session.recovery_path.read_bytes()
        self.append(self.road(1, "道路2", 1), self.add(4, 1, 1))
        journal = self.session.event_path.read_bytes()
        with patch("coordtool.picker.atomic_json", side_effect=OSError("disk full")):
            self.poll()
        self.assertEqual(self.app.points, first)
        self.assertEqual(self.session.recovery_path.read_bytes(), before)
        self.assertEqual(self.session.event_path.read_bytes(), journal)
        self.assertEqual(self.session.current_segment, 0)
        self.assertEqual(self.session.active_count, 3)
        self.assertFalse(self.app._picker_active())
        self.assertTrue(self.session.stop_path.exists())
        self.assertEqual(self.app.undo, [("CAD 取点：道路1", self.initial)])
        self.app.error.assert_called_once()

    def test_quit_without_a_pick_preserves_all_32_old_points_and_undo(self):
        self.append("READY")
        self.poll()
        self.poll(600.)
        self.append("DONE")
        self.poll()
        self.assertEqual(self.app.points, self.initial)
        self.assertEqual(self.app.undo, [])
        self.assertEqual(self.app.changes, [])
        self.assertTrue(self.session.done)
        self.assertFalse(self.app._picker_active())
        self.assertEqual(load_project(self.session.recovery_path)[0], self.initial)


if __name__ == "__main__":
    unittest.main()
