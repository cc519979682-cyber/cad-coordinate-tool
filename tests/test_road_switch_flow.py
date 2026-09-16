"""Road-switch integration: ASCII journal -> points -> recovery/CSV/DXF.

No native CAD instance is opened. Native N/Esc/U interaction is checked by the
separate AutoCAD trial; this suite verifies its real journal consumer and data.
"""
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ezdxf

from coordtool.cad import export_dxf, generate_lsp
from coordtool.core import (Point, Settings, calc_labels, connection_groups,
                            annotation_texts, read_points, write_points)
from coordtool.picker import PickerError, PickerSession
from coordtool.picker_lisp import parse_event
from coordtool.project import atomic_json, load_project
from tests.test_cad import parse_lisp


def road_line(segment, name, start):
    codepoints = ",".join(str(ord(char)) for char in name)
    return f"ROAD\t{segment}\t{start}\t{codepoints}"


def add_line(serial, index, segment=0, *, legacy=False, x=None, z=-3250.):
    x = 101000.+serial*1000. if x is None else x
    values = ["ADD", serial, index, x, -22000.-serial*1000., z,
              x+5000., -18000.-serial*1000., x+15000., 2000., 1000.]
    if not legacy:
        values.append(segment)
    return "\t".join(map(str, values))


class RoadSwitchFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coord-road-flow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/"owned-fixture.dwg"
        # Session preparation copies DWG bytes; no CAD/native conversion runs.
        self.source.write_bytes(b"AC1032 owned road switch fixture")
        self.settings = Settings(scale=1000., offset_e=1., offset_n=-2.)
        self.initial = [Point("道路甲-07", 10., 20., -4.),
                        Point("道路乙-03", 30., 40., 5.),
                        Point("已有控制点", -3., -8., .25)]

    def create(self, **changes):
        args = dict(drawing_path=self.source, road="道路甲", start=8,
                    settings=self.settings, root=self.root/"sessions",
                    initial_points=self.initial)
        args.update(changes)
        return PickerSession.create(**args)

    @staticmethod
    def events(*lines):
        return [parse_event(line) for line in lines]

    def run_three_roads(self, session, *, done=True):
        lines = ["READY", add_line(1, 8), road_line(1, "道路乙", 4),
                 add_line(2, 4, 1), road_line(2, "道路甲", 9), add_line(3, 9, 2)]
        if done:
            lines.append("DONE")
        return session.apply(self.initial, self.events(*lines))

    def test_ascii_journal_partial_road_record_and_three_segments(self):
        session = self.create()
        first = ("READY\n"+add_line(1, 8)+"\n"+road_line(1, "道路乙", 4)).encode("ascii")
        session.event_path.write_bytes(first[:-4])
        points = session.apply(self.initial, session.poll())
        self.assertEqual([p.name for p in points], [p.name for p in self.initial]+["道路甲-08"])
        self.assertEqual(session.current_segment, 0)
        with session.event_path.open("ab") as stream:
            stream.write(first[-4:]+b"\n"+(add_line(2, 4, 1)+"\n"+road_line(2, "道路甲", 9)+"\n"+add_line(3, 9, 2)+"\nDONE\n").encode("ascii"))
        points = session.apply(points, session.poll())
        self.assertEqual([p.name for p in points[-3:]], ["道路甲-08", "道路乙-04", "道路甲-09"])
        self.assertEqual([p.capture_id for p in points[-3:]], [f"{session.session_id}:{i}" for i in (1, 2, 3)])
        self.assertEqual([p.group_id for p in points[-3:]], [session.session_id,
                         f"{session.session_id}:road:1", f"{session.session_id}:road:2"])
        self.assertEqual(session.current_segment, 2)
        self.assertEqual(session.road, "道路甲")
        self.assertEqual(session.active_count, 3)
        self.assertTrue(session.done)
        self.assertEqual(session.poll(), [])

    def test_unicode_road_name_survives_protocol_project_and_quoted_csv(self):
        road = '支路, "南"😀'
        line = road_line(1, road, 1)
        self.assertTrue(line.isascii())
        self.assertEqual(parse_event(line), {"type": "ROAD", "road_id": 1, "start": 1, "road": road})
        session = self.create()
        points = session.apply(self.initial, self.events(line, add_line(1, 1, 1), "DONE"))
        self.assertEqual(points[-1].name, road+"-01")
        self.assertEqual(load_project(session.recovery_path)[0], points)
        path = self.root/"quoted.csv"
        write_points(path, points)
        imported = read_points(path, order="auto").points
        self.assertEqual([(p.name, p.e, p.n, p.z) for p in imported],
                         [(p.name, p.e, p.n, p.z) for p in points])
        self.assertTrue(all(p.placement is None and p.group_id == "" for p in imported),
                        "CSV contains numeric coordinates; projects preserve annotation metadata")
        self.assertFalse(Settings().draw_line)

    def test_malformed_unicode_and_segment_protocol_is_rejected(self):
        for line in ("ROAD\t1\t1\t1114112", "ROAD\t1\t1\t55296",
                     "ROAD\t1\t1\t65,,66", "ROAD\t1\t1\t65,10,66",
                     "ROAD\t1\t1\t", "ROAD\t1\t1\t中文",
                     "ROAD\t-1\t1\t65", "ROAD\t1\t0\t65",
                     add_line(1, 8, -1)):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_event(line)

    def test_old_add_record_keeps_initial_segment_even_after_road_switch(self):
        session = self.create()
        old = parse_event(add_line(1, 8, legacy=True))
        self.assertNotIn("road_id", old)
        points = session.apply(self.initial, [old])
        points = session.apply(points, self.events(road_line(1, "道路乙", 4), add_line(2, 4, 1)))
        replayed = session.apply(points, [old])
        self.assertEqual(replayed, points)
        self.assertEqual(session.current_segment, 1)
        self.assertEqual(session.road, "道路乙")
        self.assertEqual(points[-2].name, "道路甲-08")
        self.assertEqual(points[-2].group_id, session.session_id)

    def test_repeated_add_road_undo_batch_does_not_rewind_current_road(self):
        session = self.create()
        events = self.events("READY", add_line(1, 8), road_line(1, "道路乙", 4),
                             add_line(2, 4, 1), "UNDO\t2", add_line(3, 4, 1),
                             road_line(2, "道路甲", 9), add_line(4, 9, 2), "DONE")
        points = session.apply(self.initial, events)
        for _ in range(2):
            self.assertEqual(session.apply(points, events), points)
            self.assertEqual(session.current_segment, 2)
            self.assertEqual(session.road, "道路甲")
            self.assertEqual(session.active_count, 3)
            self.assertTrue(session.done)
        self.assertEqual(points[-2].capture_id, f"{session.session_id}:3")

    def test_undo_cannot_cross_segment_boundary_and_can_reuse_current_number(self):
        session = self.create()
        points = session.apply(self.initial, self.events(add_line(1, 8), road_line(1, "道路乙", 4)))
        before = session.recovery_path.read_bytes()
        with self.assertRaises(PickerError):
            session.apply(points, self.events("UNDO\t1"))
        self.assertEqual(session.recovery_path.read_bytes(), before)
        self.assertEqual(load_project(session.recovery_path)[0], points)
        points = session.apply(points, self.events(add_line(2, 4, 1), "UNDO\t2", add_line(3, 4, 1)))
        self.assertEqual([p.name for p in points[-2:]], ["道路甲-08", "道路乙-04"])
        self.assertEqual(points[-1].capture_id, f"{session.session_id}:3")
        self.assertEqual(session.active_count, 2)

    def test_reenter_road_numbers_use_remaining_points_not_undone_history(self):
        session = self.create()
        events = self.events(add_line(1, 8), road_line(1, "道路乙", 4),
                             add_line(2, 4, 1), "UNDO\t2", road_line(2, "道路甲", 9),
                             add_line(3, 9, 2), road_line(3, "道路乙", 4), add_line(4, 4, 3))
        points = session.apply(self.initial, events)
        self.assertEqual([p.name for p in points[-3:]], ["道路甲-08", "道路甲-09", "道路乙-04"])
        self.assertEqual(len({p.group_id for p in points[-3:]}), 3)
        self.assertEqual(session.current_segment, 3)
        self.assertEqual(session._last_serial, 4)

    def test_initial_casefold_numbering_baseline_and_non_numeric_suffixes(self):
        initial = [Point("Road-A-09", 1, 2), Point("road-a-12", 3, 4),
                   Point("ROAD-A-非编号", 5, 6), Point("Road-A-１２３", 7, 8)]
        session = self.create(initial_points=initial, road="Other", start=1)
        points = session.apply(initial, self.events(road_line(1, "ROAD-A", 13), add_line(1, 13, 1)))
        self.assertEqual(points[-1].name, "ROAD-A-13")
        self.assertEqual(points[:-1], initial)
        snapshot = session.recovery_path.read_bytes()
        with self.assertRaises(PickerError):
            session.apply(points, self.events(road_line(2, "road-a", 13)))
        self.assertEqual(session.recovery_path.read_bytes(), snapshot)

    def test_save_failure_rolls_back_road_transition_points_and_counter(self):
        session = self.create()
        points = session.apply(self.initial, self.events(add_line(1, 8)))
        before = session.recovery_path.read_bytes()
        state = (session.road, session.current_segment, session.active_count,
                 session._last_serial, session._active[:])
        batch = self.events(road_line(1, "道路乙", 4), add_line(2, 4, 1))
        with patch("coordtool.picker.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(PickerError):
                session.apply(points, batch)
        self.assertEqual((session.road, session.current_segment, session.active_count,
                          session._last_serial, session._active), state)
        self.assertEqual(session.recovery_path.read_bytes(), before)
        result = session.apply(points, batch)
        self.assertEqual(result[-1].name, "道路乙-04")
        self.assertEqual(session.current_segment, 1)
        self.assertEqual(session.active_count, 2)

    def test_invalid_later_add_rolls_back_entire_road_batch(self):
        session = self.create()
        points = session.apply(self.initial, self.events(add_line(1, 8)))
        before = session.recovery_path.read_bytes()
        for bad in (add_line(2, 99, 1), add_line(2, 4, 0), add_line(3, 4, 1)):
            with self.subTest(bad=bad), self.assertRaises(PickerError):
                session.apply(points, self.events(road_line(1, "道路乙", 4), bad))
            self.assertEqual(session.road, "道路甲")
            self.assertEqual(session.current_segment, 0)
            self.assertEqual(session.active_count, 1)
            self.assertEqual(session.recovery_path.read_bytes(), before)
        self.assertEqual(session.apply(points, self.events(road_line(1, "道路乙", 4), add_line(2, 4, 1)))[-1].name, "道路乙-04")

    def test_conflicting_or_out_of_order_road_definition_is_rejected(self):
        session = self.create()
        points = session.apply(self.initial, self.events(road_line(1, "道路乙", 4), add_line(1, 4, 1)))
        before = session.recovery_path.read_bytes()
        invalid = [road_line(1, "别的道路", 4), road_line(1, "道路乙", 9), road_line(3, "道路丙", 1)]
        for line in invalid:
            with self.subTest(line=line), self.assertRaises(PickerError):
                session.apply(points, self.events(line))
            self.assertEqual(session.recovery_path.read_bytes(), before)
            self.assertEqual(session.current_segment, 1)

    def test_previous_segment_capture_is_still_protected_after_undo_stack_resets(self):
        session = self.create()
        points = session.apply(self.initial, self.events(add_line(1, 8), road_line(1, "道路乙", 4)))
        before = session.recovery_path.read_bytes()
        externally_changed = points[:-1]+[replace(points[-1], e=999.)]
        with self.assertRaises(PickerError):
            session.apply(externally_changed, self.events(add_line(2, 4, 1)))
        self.assertEqual(session.recovery_path.read_bytes(), before)
        self.assertEqual(load_project(session.recovery_path)[0], points)
        self.assertEqual(session.current_segment, 1)
        self.assertEqual(session.active_count, 1)

    def test_all_segments_recovery_and_new_session_keep_identity_geometry_and_numbering(self):
        session = self.create()
        points = self.run_three_roads(session)
        recovered, settings, drawing = load_project(session.recovery_path)
        self.assertEqual(recovered, points)
        self.assertEqual(settings, self.settings)
        self.assertEqual(Path(drawing), self.source)
        payload = json.loads(session.recovery_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["picker"]["current_segment"], 2)
        self.assertIn("segments", payload["picker"])
        self.assertIn("road_numbers", payload["picker"])
        manual = self.root/"manual-project.json"
        atomic_json(manual, {"format": "coordtool-project", "version": 1,
                    "points": [asdict(p) for p in points], "settings": asdict(settings),
                    "drawing": str(self.source)})
        self.assertEqual(load_project(manual)[0], points)
        following = self.create(initial_points=recovered, road="下一条", start=1)
        continued = following.apply(recovered, self.events(road_line(1, "道路甲", 10), add_line(1, 10, 1)))
        self.assertEqual(continued[:-1], points)
        self.assertEqual(continued[-1].name, "道路甲-10")
        self.assertNotEqual(continued[-1].capture_id, points[-1].capture_id)
        self.assertNotEqual(continued[-1].group_id, points[-1].group_id)

    def test_dxf_geometry_and_connection_groups_preserve_each_segment_and_true_z(self):
        session = self.create(initial_points=[])
        events = self.events(add_line(1, 8, z=-3250.), add_line(2, 9, z=4500.),
                             road_line(1, "道路乙", 1), add_line(3, 1, 1, z=-6250.),
                             add_line(4, 2, 1, z=7500.), road_line(2, "道路甲", 10),
                             add_line(5, 10, 2, z=8500.), add_line(6, 11, 2, z=-9500.), "DONE")
        points = session.apply([], events)
        settings = replace(self.settings, draw_line=True, closed=True)
        labels = calc_labels(points, settings)
        groups = connection_groups(points, labels)
        self.assertEqual([len(group) for group in groups], [2, 2, 2])
        output = export_dxf(self.root/"three-segments.dxf", points, settings)
        doc = ezdxf.readfile(output)
        polylines = list(doc.modelspace().query("LWPOLYLINE"))
        self.assertEqual(len(polylines), 3)
        self.assertTrue(all(not line.closed for line in polylines))
        for group, line in zip(groups, polylines):
            self.assertEqual([(xy[0], xy[1]) for xy in line.get_points()], [(p.x, p.y) for p in group])
        inserts = list(doc.modelspace().query("INSERT"))
        for point, label, insert in zip(points, labels, inserts):
            self.assertEqual(tuple(insert.dxf.insert), (label.x, label.y, point.z*settings.scale))
            texts = [entity for entity in insert.virtual_entities() if entity.dxftype() == "TEXT"]
            self.assertEqual(len(texts), 3)
            for actual, (value, x, y, height, _) in zip(texts, annotation_texts(label, settings)):
                self.assertEqual(actual.dxf.text, value)
                anchor = actual.get_placement()[1]
                self.assertTrue(all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(anchor, (x, y, point.z*settings.scale))))
                self.assertEqual(actual.dxf.height, height)
        # The annotation script uses the same road-aware connections and 3D inserts.
        script = generate_lsp(points, settings)
        parse_lisp(script)
        self.assertEqual(script.count("'(0 . \"TEXT\")"), 18)
        self.assertIn('(cons 1 "道路甲-10")', script)


if __name__ == "__main__":
    unittest.main()
