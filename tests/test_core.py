from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import unittest

from coordtool.core import (Point, Settings, CoordinateOrderRequired, calc_labels, parse_text,
                            read_points, transform_point, validate_settings,
                            write_points)


class ImportTests(unittest.TestCase):
    def test_numeric_point_names_and_no_magnitude_guess(self):
        result = parse_text("001,1,999999\n002,-2,-888888")
        self.assertEqual(result.points, [Point("001", 1, 999999), Point("002", -2, -888888)])
        self.assertEqual(parse_text("7,10,20", "NE").points, [Point("7", 20, 10)])

    def test_auto_follows_header_and_arbitrary_column_positions(self):
        result = parse_text("北 N (米),点名,东 E (米)\n20,控制点一,10", "auto")
        self.assertEqual(result.points, [Point("控制点一", 10, 20)])
        result = parse_text("Name,Easting,Northing\na,5,9", "auto")
        self.assertEqual(result.points, [Point("a", 5, 9)])

    def test_explicit_order_overrides_header_with_visible_warning(self):
        result = parse_text("点名,北N,东E\na,20,10", "EN")
        self.assertEqual(result.points, [Point("a", 20, 10)])
        self.assertTrue(any("不一致" in warning for warning in result.warnings))

    def test_auto_requires_unambiguous_header(self):
        for data in ("a,10,20", "Name,X,Y\na,10,20"):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "EN 或 NE"):
                parse_text(data, "auto")

    def test_blank_field_never_shifts_columns(self):
        with self.assertRaisesRegex(ValueError, "第 3 行.*坐标为空"):
            parse_text("点名,E,N\na,1,2\nb,,3")
        with self.assertRaisesRegex(ValueError, "第 1 行.*点名不能为空"):
            parse_text(",1,2")

    def test_bad_rows_reject_entire_input_and_report_each_line(self):
        with self.assertRaises(ValueError) as caught:
            parse_text("点名,E,N\na,1,2\nb,oops,3\nc,4,inf\nd,NaN,5")
        for number in (3, 4, 5):
            self.assertIn(f"第 {number} 行", str(caught.exception))
        self.assertIn("导入已取消", str(caught.exception))

    def test_numeric_overflow_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "有限数字"):
            parse_text("a,1e309,2")

    def test_duplicate_names_are_preserved_with_line_warning(self):
        result = parse_text("点名,E,N\na,1,2\na,3,4")
        self.assertEqual(len(result.points), 2)
        self.assertIn("第 3 行", result.warnings[0])
        self.assertIn("第 2 行", result.warnings[0])

    def test_quoted_names_and_two_column_data(self):
        self.assertEqual(parse_text('"桩,01",12.125,23.875').points, [Point("桩,01", 12.125, 23.875)])
        self.assertEqual(parse_text('"桩,01";12.125;23.875').points, [Point("桩,01", 12.125, 23.875)])
        self.assertEqual(parse_text("1 2\n\n3 4").points, [Point("点1", 1, 2), Point("点2", 3, 4)])
        self.assertEqual(parse_text("a\t1\t2").points, [Point("a", 1, 2)])

    def test_more_columns_require_header_and_warn_about_ignored_fields(self):
        with self.assertRaisesRegex(ValueError, "实际 5 列"):
            parse_text("a,1,2,3,note")
        result = parse_text("点名,E,N,备注\na,1,2,说明", "auto")
        self.assertEqual(result.points, [Point("a", 1, 2)])
        self.assertTrue(any("备注" in warning for warning in result.warnings))

    def test_row_width_is_consistent(self):
        with self.assertRaisesRegex(ValueError, "第 2 行.*列数"):
            parse_text("a,1,2\n3,4")

    def test_bom_and_gb18030_files(self):
        with tempfile.TemporaryDirectory() as directory:
            for encoding in ("utf-8-sig", "gb18030"):
                path = Path(directory)/"点.csv"
                path.write_bytes("点名,东E,北N\n点一,1,2".encode(encoding))
                self.assertEqual(read_points(path, "auto").points, [Point("点一", 1, 2)])

    def test_text_roundtrip_preserves_coordinate_precision(self):
        points = [Point("001", -12345.6789012345, 23456.7890123456), Point("桩,02", 0, 0)]
        with tempfile.TemporaryDirectory() as directory:
            for extension in ("csv", "txt", "tsv"):
                path = Path(directory)/f"坐标.{extension}"
                write_points(path, points)
                self.assertEqual(read_points(path, "auto").points, points)

    def test_invalid_points_do_not_truncate_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.csv"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_points(path, [Point("a", 1, float("nan"))])
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_failed_export_replace_preserves_existing_file_and_cleans_temp(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.csv"
            path.write_text("original", encoding="utf-8")
            with patch("coordtool.core.os.replace", side_effect=PermissionError("file in use")):
                with self.assertRaises(PermissionError):
                    write_points(path, [Point("a", 1, 2)])
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_xlsx_roundtrip_and_string_names(self):
        import openpyxl
        points = [Point("001", -100.125, 200.375), Point("=1+1", 3.25, 7.125)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.xlsx"
            write_points(path, points)
            self.assertEqual(read_points(path, "auto").points, points)
            book = openpyxl.load_workbook(path)
            self.assertEqual(book.active["A3"].data_type, "s")
            book.close()

    def test_xlsx_blank_column_and_formula_coordinates_reject(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.xlsx"
            book = openpyxl.Workbook()
            book.active.append(["点名", "E", "N"])
            book.active.append(["a", None, 2])
            book.save(path)
            with self.assertRaisesRegex(ValueError, "第 2 行.*坐标为空"):
                read_points(path, "auto")
            book.active["B2"] = "=1+1"
            book.save(path)
            with self.assertRaisesRegex(ValueError, "第 2 行.*有效数字"):
                read_points(path, "auto")
            book.close()


class GeometryTests(unittest.TestCase):
    def test_models_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            Point("a", 1, 2).e = 4

    def test_offset_in_metres_then_scale(self):
        self.assertEqual(transform_point(Point("a", 1, 2), Settings(offset_e=10, offset_n=-5, scale=1000)), (11000, -3000))

    def test_invalid_settings_are_rejected(self):
        for changes in ({"radius": 0}, {"leader": -1}, {"text_height": float("nan")},
                        {"scale": float("inf")}, {"offset_e": float("nan")},
                        {"layer": "bad/name"}, {"layer": " bad"}, {"layer": ""},
                        {"color": 256}, {"color": True}, {"lineweight": 51},
                        {"radius": "1"}, {"offset_e": "1"}, {"lineweight": 50.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings(replace(Settings(), **changes))

    def test_straight_polyline_alternates_leader_sides(self):
        points = [Point(str(i), 0, i*100) for i in range(4)]
        labels = calc_labels(points, Settings())
        self.assertEqual([label.direction for label in labels], [1, -1, 1, -1])
        self.assertTrue(all(label.lx != label.x for label in labels))

    def test_geometric_layout_is_scale_and_translation_invariant(self):
        points = [Point(str(i), i*3, (i % 3)*2) for i in range(10)]
        originals = calc_labels(points, Settings())
        changed = calc_labels(points, Settings(offset_e=37, offset_n=-19, scale=1000))
        for original, transformed in zip(originals, changed):
            for attribute in ("x", "lx", "hx", "tx"):
                self.assertAlmostEqual(getattr(transformed, attribute), (getattr(original, attribute)+37)*1000)
            for attribute in ("y", "ly"):
                self.assertAlmostEqual(getattr(transformed, attribute), (getattr(original, attribute)-19)*1000)
            self.assertEqual(transformed.direction, original.direction)

    def test_style_settings_do_not_change_layout(self):
        points = [Point(str(i), i, 0) for i in range(5)]
        self.assertEqual(calc_labels(points, Settings()), calc_labels(points, Settings(color=3, lineweight=70, layer="OTHER", draw_line=True, radius=3)))

    def test_close_and_coincident_points_are_finite_and_deterministic(self):
        import math
        points = [Point(str(i), 0, 0) for i in range(12)]
        labels = calc_labels(points, Settings())
        self.assertEqual(labels, calc_labels(points, Settings()))
        self.assertEqual(len(labels), len(points))
        self.assertTrue(all(math.isfinite(getattr(label, field)) for label in labels for field in ("x", "y", "lx", "ly", "hx", "tx")))
        self.assertGreater(len({(label.lx, label.ly) for label in labels}), 2)

    def test_single_point_and_empty(self):
        self.assertEqual(calc_labels([], Settings()), [])
        label = calc_labels([Point("single", 100, 200)], Settings())[0]
        self.assertEqual((label.x, label.y), (100, 200))

    def test_large_radius_keeps_leader_end_outside_own_circle(self):
        import math
        label = calc_labels([Point("a", 0, 0)], Settings(radius=100, leader=5))[0]
        self.assertGreater(math.hypot(label.lx-label.x, label.ly-label.y), 100)


class AutomaticImportAndElevationTests(unittest.TestCase):
    def test_four_columns_preserve_optional_elevation_and_legacy_iteration(self):
        result = parse_text("001,10,20,-2.125\n002,11,21,", "EN")
        self.assertEqual(result.points, [Point("001", 10, 20, -2.125), Point("002", 11, 21)])
        self.assertEqual(tuple(result.points[0]), ("001", 10, 20))
        self.assertIn("高程", result.description)
        self.assertEqual(result.detected_order, "EN")

    def test_ne_header_precedes_conflicting_drawing_bounds_and_preserves_z(self):
        result = parse_text("高程Z,北N,点号,东E\n-2.125,10,001,20", "auto", reference_bounds=(0, 90, 5, 100))
        self.assertEqual(result.points, [Point("001", 20, 10, -2.125)])
        self.assertEqual(result.detected_order, "NE")
        self.assertEqual(result.confidence, 1)
        self.assertIn("表头", result.description)

    def test_headerless_ne_identified_only_from_drawing_region(self):
        result = parse_text("p1,10,100,-2\np2,11,101,-3", "auto", reference_bounds=(95, 5, 110, 15))
        self.assertEqual(result.points, [Point("p1", 100, 10, -2), Point("p2", 101, 11, -3)])
        self.assertEqual(result.detected_order, "NE")
        self.assertEqual(result.confidence, 1)
        self.assertIn("底图范围", result.description)

    def test_missing_reference_asks_order_with_short_samples(self):
        text = "001,1,999999,-1\n002,2,888888,-2\n003,3,777777,-3\n004,4,666666,-4"
        with self.assertRaises(CoordinateOrderRequired) as caught:
            parse_text(text, "auto")
        self.assertEqual(caught.exception.sample_rows, [["001", "1", "999999", "-1"],
                                                      ["002", "2", "888888", "-2"],
                                                      ["003", "3", "777777", "-3"]])
        self.assertTrue(caught.exception.reason)

    def test_ambiguous_both_and_neither_matching_ask_order(self):
        for bounds in ((0, 0, 100, 100), (100, 100, 200, 200)):
            with self.subTest(bounds=bounds), self.assertRaises(CoordinateOrderRequired):
                parse_text("p,10,20", "auto", reference_bounds=bounds)

    def test_exact_80_20_boundary_is_accepted_with_outside_warning(self):
        rows = [f"p{i},{100+i},5" for i in range(8)] + [f"p{i},5,{100+i}" for i in range(8, 10)]
        result = parse_text("\n".join(rows), "auto", reference_bounds=(100, 0, 200, 10))
        self.assertEqual(result.detected_order, "EN")
        self.assertEqual(result.confidence, .8)
        self.assertTrue(any("20%" in warning for warning in result.warnings))

    def test_70_30_boundary_does_not_guess(self):
        rows = [f"p{i},{100+i},5" for i in range(7)] + [f"p{i},5,{100+i}" for i in range(7, 10)]
        with self.assertRaises(CoordinateOrderRequired):
            parse_text("\n".join(rows), "auto", reference_bounds=(100, 0, 200, 10))

    def test_second_direction_above_20_percent_prevents_auto_selection(self):
        # Five rows match both directions and five only EN: EN=100%, NE=50%.
        rows = [f"p{i},15,15" for i in range(5)] + [f"p{i},100,15" for i in range(5, 10)]
        with self.assertRaises(CoordinateOrderRequired):
            parse_text("\n".join(rows), "auto", reference_bounds=(0, 0, 200, 20))

    def test_bounds_comparison_uses_offset_then_scale_but_keeps_source_coordinates(self):
        settings = Settings(offset_e=10, offset_n=-5, scale=1000)
        result = parse_text("p,12,101,-2.295", "auto", reference_bounds=(110000, 6000, 120000, 8000), settings=settings)
        self.assertEqual(result.points, [Point("p", 101, 12, -2.295)])
        self.assertEqual(result.detected_order, "NE")

    def test_xy_header_requires_reference_or_manual_choice(self):
        text = "点名,X,Y,H\np,10,100,-2"
        with self.assertRaises(CoordinateOrderRequired):
            parse_text(text, "auto")
        self.assertEqual(parse_text(text, "NE").points, [Point("p", 100, 10, -2)])
        result = parse_text(text, "auto", reference_bounds=(95, 5, 110, 15))
        self.assertEqual(result.points, [Point("p", 100, 10, -2)])

    def test_malformed_rows_fail_before_requesting_coordinate_order(self):
        for text in ("p,1,,3", "p,1,2,NaN", "p,1,2,inf", "p,1,2,not-elevation"):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                parse_text(text, "auto")
            self.assertNotIsInstance(caught.exception, CoordinateOrderRequired)
            self.assertIn("第 1 行", str(caught.exception))

    def test_optional_z_blank_does_not_allow_missing_plane_coordinate_or_column(self):
        with self.assertRaisesRegex(ValueError, "第 2 行.*坐标为空"):
            parse_text("a,1,2,3\nb,,4,")
        with self.assertRaisesRegex(ValueError, "第 2 行.*列数"):
            parse_text("a,1,2,3\nb,4,5")

    def test_elevation_roundtrip_csv_tsv_and_xlsx(self):
        points = [Point("001", 10.1234567890123, -20.2345678901234, -2.295), Point("002", 11, 21)]
        with tempfile.TemporaryDirectory() as directory:
            for extension in ("csv", "tsv", "xlsx"):
                with self.subTest(extension=extension):
                    path = Path(directory)/f"坐标.{extension}"
                    write_points(path, points)
                    self.assertEqual(read_points(path, "auto").points, points)

    def test_utf16_bom_little_and_big_endian_with_excel_sep(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.csv"
            text = 'sep=;\r\n点名;北N;东E;高程\r\n"点,一";10;100;-2.295\r\n'
            for encoding, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
                with self.subTest(encoding=encoding):
                    path.write_bytes(bom+text.encode(encoding))
                    self.assertEqual(read_points(path, "auto").points, [Point("点,一", 100, 10, -2.295)])

    def test_sep_directive_keeps_physical_error_line_numbers(self):
        with self.assertRaisesRegex(ValueError, "第 3 行.*坐标为空"):
            parse_text("sep=;\n点名;E;N;Z\na;1;;3", "auto")

    def test_invalid_elevation_cannot_overwrite_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"坐标.csv"
            path.write_text("original", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_points(path, [Point("a", 1, 2, float("inf"))])
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_z_does_not_change_plan_layout(self):
        plan = [Point("a", 1, 2), Point("b", 10, 20)]
        elevated = [Point("a", 1, 2, 1000), Point("b", 10, 20, -2000)]
        self.assertEqual(calc_labels(plan, Settings()), calc_labels(elevated, Settings()))


if __name__ == "__main__":
    unittest.main()
