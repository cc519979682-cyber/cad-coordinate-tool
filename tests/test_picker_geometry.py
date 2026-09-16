"""CAD capture positions remain exact through preview geometry and persistence."""
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import tempfile
import unittest

import ezdxf
from ezdxf.enums import TextEntityAlignment

from coordtool.core import (Point, Placement, Settings, calc_labels,
                            annotation_segments, annotation_texts, connection_groups)
from coordtool.cad import export_dxf, generate_lsp
from coordtool.project import atomic_json, load_project
from tests.test_cad import parse_lisp


class PickerGeometryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coord-picker-geometry-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.settings = Settings(scale=1000., offset_e=10., offset_n=1.)
        self.point = Point("道路甲-01", -50., -13., -2.295,
                           Placement(-40., -9., -30., .8, .5),
                           "session-a:1", "road-a")

    def test_manual_world_position_is_only_scaled_and_offset(self):
        label = calc_labels([self.point], self.settings)[0]
        self.assertEqual((label.x, label.y), (-40000., -12000.))
        self.assertEqual((label.lx, label.ly, label.hx), (-30000., -8000., -20000.))
        self.assertEqual((label.height, label.arrow, label.direction), (800., 500., 1))
        self.assertEqual(label.coordinate_text, ("E=-50.000", "N=-13.000"))
        # User style defaults and neighbouring points cannot move a manual label.
        settings = replace(self.settings, radius=17, leader=3, text_height=15)
        others = [Point("nearby", -40.1, -9.1), self.point, Point("other", -40.2, -9.2)]
        self.assertEqual(calc_labels(others, settings)[1], label)

    def test_hollow_arrow_wings_face_elbow_and_preserve_length(self):
        label = calc_labels([self.point], self.settings)[0]
        segments = annotation_segments(label, self.settings)
        self.assertEqual(len(segments), 4)
        angle = math.atan2(4000., 10000.)
        for (start, end), delta in zip(segments[:2], (-math.pi/6, math.pi/6)):
            self.assertEqual(start, (-40000., -12000.))
            self.assertAlmostEqual(math.dist(start, end), 500.)
            self.assertAlmostEqual(end[0], -40000.+500.*math.cos(angle+delta))
            self.assertAlmostEqual(end[1], -12000.+500.*math.sin(angle+delta))
        self.assertEqual(segments[2:], [((-40000., -12000.), (-30000., -8000.)),
                                       ((-30000., -8000.), (-20000., -8000.))])

    def test_text_baselines_and_both_alignments_match_manual_placement(self):
        label = calc_labels([self.point], self.settings)[0]
        self.assertEqual(annotation_texts(label, self.settings), [
            ("道路甲-01", -30000., -7760., 800., "left"),
            ("E=-50.000", -30000., -8960., 800., "left"),
            ("N=-13.000", -30000., -10160., 800., "left"),
        ])
        left_point = replace(self.point, placement=replace(self.point.placement, horizontal_e=-60))
        left_label = calc_labels([left_point], self.settings)[0]
        self.assertEqual(left_label.direction, -1)
        self.assertEqual(left_label.hx, -50000.)
        self.assertTrue(all(row[-1] == "right" for row in annotation_texts(left_label, self.settings)))

    def test_old_labels_keep_circle_marker_and_one_text(self):
        ordinary = Point("legacy", 1, 2, 3)
        label = calc_labels([ordinary], Settings())[0]
        self.assertIsNone(label.arrow)
        self.assertIsNone(label.height)
        self.assertIsNone(label.coordinate_text)
        self.assertEqual(len(annotation_segments(label, Settings())), 4)
        self.assertEqual(len(annotation_texts(label, Settings())), 1)
        self.assertEqual(tuple(self.point), ("道路甲-01", -50., -13.))

    def payload(self, points=None):
        return {"format": "coordtool-project", "version": 1, "drawing": None,
                "settings": asdict(self.settings),
                "points": [asdict(p) for p in (points or [self.point])]}

    def test_nested_placement_and_ids_roundtrip_with_old_v1_points(self):
        path = self.directory/"project.json"
        payload = self.payload()
        payload["points"].append({"name": "old point", "e": 1, "n": 2})
        atomic_json(path, payload)
        points, settings, drawing = load_project(path)
        self.assertEqual(points, [self.point, Point("old point", 1., 2.)])
        self.assertIsInstance(points[0].placement, Placement)
        self.assertEqual(settings, self.settings)
        self.assertIsNone(drawing)
        self.assertEqual(asdict(points[0]), payload["points"][0])

    def test_project_rejects_malformed_placement_and_metadata(self):
        invalid = [
            {"placement": []}, {"placement": {"e": 1}},
            {"placement": {**asdict(self.point.placement), "height": 0}},
            {"placement": {**asdict(self.point.placement), "arrow": -1}},
            {"placement": {**asdict(self.point.placement), "e": float("nan")}},
            {"placement": {**asdict(self.point.placement), "n": True}},
            {"placement": {**asdict(self.point.placement), "height": "1"}},
            {"capture_id": None}, {"capture_id": "bad\nid"},
            {"group_id": 1}, {"group_id": " trailing "},
        ]
        path = self.directory/"bad.json"
        for patch in invalid:
            with self.subTest(patch=patch):
                payload = self.payload()
                payload["points"][0].update(patch)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "第 1 个点.*无效"):
                    load_project(path)
        atomic_json(path, self.payload([self.point, self.point]))
        with self.assertRaisesRegex(ValueError, "取点标识重复"):
            load_project(path)

    def test_export_rejects_nonfinite_manual_geometry_before_writing(self):
        path = self.directory/"preserved.dxf"
        path.write_bytes(b"previous result")
        bad = replace(self.point, placement=replace(self.point.placement, arrow=float("inf")))
        with self.assertRaises(ValueError):
            export_dxf(path, [bad], self.settings)
        self.assertEqual(path.read_bytes(), b"previous result")

    def test_dxf_preserves_hollow_marker_three_texts_height_and_true_z(self):
        point = replace(self.point, placement=replace(self.point.placement, horizontal_e=-60.))
        path = export_dxf(self.directory/"captured.dxf", [point], self.settings)
        doc = ezdxf.readfile(path)
        self.assertFalse(doc.audit().has_errors)
        insert = doc.modelspace().query("INSERT")[0]
        self.assertEqual(tuple(insert.dxf.insert), (-40000., -12000., -2295.))
        block = doc.blocks[insert.dxf.name]
        self.assertEqual(len(block.query("CIRCLE")), 0)
        self.assertEqual(len(block.query("LINE")), 4)
        self.assertEqual(len(block.query("TEXT")), 3)
        label = calc_labels([point], self.settings)[0]
        texts = list(e for e in insert.virtual_entities() if e.dxftype() == "TEXT")
        for actual, expected in zip(texts, annotation_texts(label, self.settings)):
            value, x, y, height, _ = expected
            alignment, anchor, _ = actual.get_placement()
            self.assertEqual(alignment, TextEntityAlignment.RIGHT)
            self.assertEqual(actual.dxf.text, value)
            self.assertEqual(actual.dxf.height, height)
            self.assertEqual(tuple(anchor), (x, y, -2295.))

    def test_lsp_preserves_captured_geometry_and_elevation(self):
        code = generate_lsp([self.point], self.settings)
        parse_lisp(code)
        self.assertNotIn("'(0 . \"CIRCLE\")", code)
        self.assertEqual(code.count("'(0 . \"TEXT\")"), 3)
        self.assertIn("(cons 10 (list -40000 -12000 -2295))", code)
        self.assertIn('(cons 1 "E=-50.000")', code)
        self.assertIn('(cons 1 "N=-13.000")', code)
        self.assertIn("(cons 40 800)", code)

    def road_points(self):
        return [replace(self.point, name=f"road-{road}-{i}", e=road*100+i,
                        capture_id=f"session:{road}:{i}", group_id=f"road-{road}")
                for road in range(3) for i in range(3)]

    def test_explicit_connection_never_bridges_different_roads(self):
        points = self.road_points()
        settings = Settings(draw_line=True, closed=True)
        labels = calc_labels(points, settings)
        groups = connection_groups(points, labels)
        self.assertEqual([[label.name for label in group] for group in groups],
                         [[f"road-{road}-{i}" for i in range(3)] for road in range(3)])
        path = export_dxf(self.directory/"roads.dxf", points, settings)
        lines = ezdxf.readfile(path).modelspace().query("LWPOLYLINE")
        self.assertEqual(len(lines), 3)
        for road, line in enumerate(lines):
            self.assertTrue(line.closed)
            self.assertEqual([p[0] for p in line.get_points()], [road*100+i for i in range(3)])
        code = generate_lsp(points, settings)
        parse_lisp(code)
        model_lines = []
        in_block = False
        for row in code.splitlines():
            if row.strip() == "(setq blockopen T)":
                in_block = True
            elif row.strip() == "(setq blockopen nil)":
                in_block = False
            elif not in_block and "(entmake (list '(0 . \"LINE\")" in row:
                model_lines.append(row)
        self.assertEqual(len(model_lines), 9)

    def test_ungrouped_runs_and_resumed_road_remain_separate(self):
        road = self.road_points()
        points = [Point("a", 0, 0), Point("b", 1, 1), road[0], road[3],
                  Point("c", 2, 2), road[1]]
        groups = connection_groups(points, calc_labels(points, Settings()))
        self.assertEqual([[label.name for label in group] for group in groups],
                         [["a", "b"], ["road-0-0", "road-0-1"], ["road-1-0"], ["c"]])


if __name__ == "__main__":
    unittest.main()
