from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordtool.core import Point, Settings
from coordtool.project import atomic_json, load_project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory/"坐标项目.json"
        self.points = [Point("001", -12345.6789012345, 23456.7890123456), Point("控制点二", 0.125, -0.375)]
        self.settings = Settings(offset_e=12.125, offset_n=-3.375, scale=1000, closed=True)
        self.payload = {"format": "coordtool-project", "version": 1,
                        "points": [asdict(point) for point in self.points],
                        "settings": asdict(self.settings), "drawing": None}

    def write_raw(self, payload):
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_full_precision_settings_and_numeric_names_roundtrip(self):
        atomic_json(self.path, self.payload)
        points, settings, drawing = load_project(self.path)
        self.assertEqual(points, self.points)
        self.assertEqual(settings, self.settings)
        self.assertIsNone(drawing)

    def test_elevation_roundtrip_and_legacy_projects_without_z(self):
        self.payload["points"][0]["z"] = -2.295123456789
        self.payload["points"][1].pop("z", None)
        atomic_json(self.path, self.payload)
        points, _, _ = load_project(self.path)
        self.assertEqual(points[0], Point("001", self.points[0].e, self.points[0].n, -2.295123456789))
        self.assertIsNone(points[1].z)

    def test_explicit_legacy_connection_choice_is_preserved(self):
        self.payload["points"][0]["z"] = -2.295
        for enabled in (True, False):
            with self.subTest(draw_line=enabled):
                self.payload["settings"]["draw_line"] = enabled
                self.payload["settings"]["closed"] = True
                atomic_json(self.path, self.payload)
                points, settings, _ = load_project(self.path)
                self.assertEqual(settings.draw_line, enabled)
                self.assertTrue(settings.closed)
                self.assertEqual(points[0], Point("001", self.points[0].e, self.points[0].n, -2.295))

    def test_missing_connection_setting_defaults_to_discrete_points(self):
        self.payload["settings"].pop("draw_line")
        atomic_json(self.path, self.payload)
        points, settings, _ = load_project(self.path)
        self.assertEqual(points, self.points)
        self.assertFalse(settings.draw_line)
        self.assertTrue(settings.closed)

    def test_invalid_project_elevation_is_rejected(self):
        for value in (True, float("inf"), float("nan"), "bad"):
            with self.subTest(value=value):
                payload = deepcopy(self.payload)
                payload["points"][1]["z"] = value
                self.write_raw(payload)
                with self.assertRaisesRegex(ValueError, "2.*高程"):
                    load_project(self.path)

    def test_relative_drawing_resolves_against_project_not_working_directory(self):
        self.payload["drawing"] = "图纸/建筑底图.dxf"
        atomic_json(self.path, self.payload)
        drawing = load_project(self.path)[2]
        self.assertEqual(Path(drawing), (self.directory/"图纸"/"建筑底图.dxf").resolve())

    def test_absolute_drawing_is_retained(self):
        original = (self.directory/"other"/"底图.dxf").resolve()
        self.payload["drawing"] = str(original)
        atomic_json(self.path, self.payload)
        self.assertEqual(Path(load_project(self.path)[2]), original)

    def test_nonfinite_coordinates_rejected_with_point_number(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                payload = deepcopy(self.payload)
                payload["points"][1]["n"] = value
                self.write_raw(payload)
                with self.assertRaisesRegex(ValueError, "2.*坐标"):
                    load_project(self.path)

    def test_boolean_coordinates_rejected(self):
        self.payload["points"][0]["e"] = True
        self.write_raw(self.payload)
        with self.assertRaisesRegex(ValueError, "1.*坐标"):
            load_project(self.path)

    def test_malformed_shapes_fail_with_valueerror(self):
        malformed = [[], None, {}, {"format": "different", "version": 1},
                     {"format": "coordtool-project", "version": 2},
                     {"format": "coordtool-project", "version": 1},
                     dict(self.payload, settings=[]), dict(self.payload, points={}),
                     dict(self.payload, points=[{"name": "a", "e": 1}]),
                     dict(self.payload, points=[None]), dict(self.payload, drawing=42)]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.write_raw(payload)
                with self.assertRaises(ValueError):
                    load_project(self.path)

    def test_unsupported_and_invalid_settings_fail(self):
        for changes in ({"radius": -1}, {"offset_e": float("nan")}, {"scale": 10}, {"color": 20}, {"layer": "bad/name"}):
            with self.subTest(changes=changes):
                payload = deepcopy(self.payload)
                payload["settings"].update(changes)
                self.write_raw(payload)
                with self.assertRaises(ValueError):
                    load_project(self.path)

    def test_formula_like_names_are_plain_text(self):
        self.payload["points"][0]["name"] = "=1+1"
        atomic_json(self.path, self.payload)
        self.assertEqual(load_project(self.path)[0][0].name, "=1+1")

    def test_invalid_names_rejected(self):
        for name in ("", " ", "a\nb", "a\tb", 123):
            with self.subTest(name=name):
                payload = deepcopy(self.payload)
                payload["points"][0]["name"] = name
                self.write_raw(payload)
                with self.assertRaises(ValueError):
                    load_project(self.path)

    def test_invalid_json_rejected(self):
        self.path.write_text('{"format":', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_project(self.path)

    def test_failed_save_preserves_existing_project(self):
        atomic_json(self.path, self.payload)
        before = self.path.read_bytes()
        with patch("coordtool.project.os.replace", side_effect=PermissionError("file in use")):
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {"modified": True})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_nonfinite_save_never_replaces_existing_project(self):
        atomic_json(self.path, self.payload)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            atomic_json(self.path, {"coordinate": float("nan")})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
