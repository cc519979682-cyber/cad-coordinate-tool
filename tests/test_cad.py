"""Round-trip drawing tests and evaluation of the emitted Lisp transform."""
import copy
from dataclasses import replace
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import ezdxf
from ezdxf.math import OCS

from coordtool import cad
from coordtool.core import Point, Settings, calc_labels


class Symbol(str):
    pass


def parse_lisp(code):
    """Small S-expression reader; rejects broken parentheses/quoted strings."""
    tokens = re.findall(r';[^\n]*|\(|\)|\x27|"(?:\\.|[^"\\])*"|[^\s()\x27"]+', code)
    tokens = [t for t in tokens if not t.startswith(";")]
    index = 0
    def read():
        nonlocal index
        if index >= len(tokens):
            raise ValueError("unclosed expression")
        token = tokens[index]
        index += 1
        if token == "(":
            value = []
            while index < len(tokens) and tokens[index] != ")":
                value.append(read())
            if index >= len(tokens):
                raise ValueError("missing closing parenthesis")
            index += 1
            return value
        if token == ")":
            raise ValueError("unexpected closing parenthesis")
        if token == "'":
            return [Symbol("quote"), read()]
        if token.startswith('"'):
            return re.sub(r'\\(.)', lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m[1], m[1]), token[1:-1])
        try:
            return float(token) if any(c in token for c in ".eE") else int(token)
        except ValueError:
            return Symbol(token)
    forms = []
    while index < len(tokens):
        forms.append(read())
    return forms


def run_lisp_world(code, center, base, insert, extrusion=(0, 0, 1)):
    """Evaluate the actual emitted V22:world and V22:val, with OCS via ezdxf.

    This is not an AutoCAD runtime smoke test. It exercises the exact arithmetic
    and instruction order emitted into the AutoLISP export.
    """
    forms = parse_lisp(code)
    functions = {str(f[1]): f for f in forms if isinstance(f, list) and f and f[0] == "defun"}
    primitives = {
        "+": lambda a, b: a + b, "-": lambda a, b: a - b,
        "*": lambda a, b: a * b,
        "car": lambda x: x[0], "cadr": lambda x: x[1], "caddr": lambda x: x[2],
        "cdr": lambda x: x[1] if isinstance(x, tuple) else x[1:],
        "assoc": lambda k, data: next((p for p in data if p[0] == k), None),
        "cos": math.cos, "sin": math.sin, "list": lambda *x: list(x),
        "trans": lambda pt, ent, zero: list(OCS(extrusion).to_wcs(pt)),
    }
    def call(name, args):
        if name == "mapcar":
            name, *seqs = args
            return [call(name, list(items)) for items in zip(*seqs)]
        if name in primitives:
            return primitives[name](*args)
        form = functions[name]
        parameters = form[2]
        slash = parameters.index("/") if "/" in parameters else len(parameters)
        env = dict(zip(parameters[:slash], args))
        for local in parameters[slash+1:]:
            env[local] = None
        result = None
        for expression in form[3:]:
            result = evaluate(expression, env)
        return result
    def evaluate(expr, env):
        if isinstance(expr, Symbol):
            return env[expr]
        if not isinstance(expr, list):
            return expr
        op = expr[0]
        if op == "quote":
            return expr[1]
        if op == "if":
            return evaluate(expr[2] if evaluate(expr[1], env) else expr[3], env)
        if op == "setq":
            for i in range(1, len(expr), 2):
                env[expr[i]] = evaluate(expr[i+1], env)
            return env[expr[-2]]
        return call(op, [evaluate(arg, env) for arg in expr[1:]])
    return call("V22:world", [center, base, list(insert.items()), "entity"])


class CadTests(unittest.TestCase):
    def setUp(self):
        no_native = patch.object(cad, "find_core_console", return_value=None)
        no_native.start()
        self.addCleanup(no_native.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="coord-cad-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.points = [Point('测点A"\\,', -12340.123456, -23450.5),
                       Point("测点B", -53680, -12990), Point("测点C", -53665, -13020)]

    def test_native_dwg_keeps_paperspace_objects_and_original_source(self):
        source = self.folder / "input.dwg"
        source.write_bytes(b"DWG fixture")
        dxf = self.folder / "converted.dxf"
        doc = ezdxf.new("R2018", units=0)
        doc.modelspace().add_line((10, 20), (30, 40))
        layout = doc.layouts.new("paper_layout")
        layout.add_viewport(center=(2, 3), size=(4, 5), view_center_point=(10, 20), view_height=50)
        doc.saveas(dxf)
        with patch.object(cad, "find_core_console", return_value=Path("accoreconsole.exe")), patch.object(cad, "convert_native_dwg", return_value=dxf) as convert:
            loaded, warnings = cad.load_drawing(source)
        convert.assert_called_once_with(source.resolve())
        self.assertEqual(loaded.units, 0)
        self.assertEqual(len(loaded.layouts.get("paper_layout").query("VIEWPORT")), 1)
        self.assertEqual(loaded._coordtool_quality, "native_autocad")
        self.assertEqual(loaded._coordtool_source, str(source.resolve()))
        self.assertEqual(source.read_bytes(), b"DWG fixture")

    def test_native_failure_is_visible_without_silent_simplification(self):
        source = self.folder / "input.dwg"
        source.write_bytes(b"DWG fixture")
        with patch.object(cad, "find_core_console", return_value=Path("accoreconsole.exe")), patch.object(cad, "convert_native_dwg", side_effect=TimeoutError("native timeout")):
            with self.assertRaisesRegex(cad.DrawingError, "native timeout"):
                cad.load_drawing(source)

    def test_dxf_roundtrip_local_blocks_and_real_lineweight(self):
        settings = Settings(offset_e=12.3, offset_n=-4.5, scale=1000, draw_line=True, closed=True, lineweight=70)
        output = cad.export_dxf(self.folder / "points.dxf", self.points, settings)
        doc = ezdxf.readfile(output)
        self.assertEqual(doc.units, 4)
        self.assertFalse(doc.audit().has_errors)
        self.assertTrue(doc.modelspace().query("LWPOLYLINE")[0].closed)
        inserts = list(doc.modelspace().query("INSERT"))
        self.assertEqual(len(inserts), 3)
        for point, entity in zip(self.points, inserts):
            self.assertAlmostEqual(entity.dxf.insert.x, (point.e + 12.3) * 1000)
            self.assertAlmostEqual(entity.dxf.insert.y, (point.n - 4.5) * 1000)
            block = doc.blocks[entity.dxf.name]
            self.assertEqual(tuple(block.base_point), (0, 0, 0))
            self.assertEqual(tuple(block.query("CIRCLE")[0].dxf.center), (0, 0, 0))
            self.assertEqual(block.query("CIRCLE")[0].dxf.radius, 1500)
            self.assertEqual(block.query("TEXT")[0].dxf.text, point.name)
            self.assertEqual(block.query("TEXT")[0].dxf.height, 2000)
            for element in block:
                self.assertEqual(element.dxf.lineweight, 70)
            virtual_circle = next(e for e in entity.virtual_entities() if e.dxftype() == "CIRCLE")
            self.assertEqual(virtual_circle.dxf.center, entity.dxf.insert)

    def test_base_preserved_and_repeat_export_uses_unique_blocks(self):
        base = ezdxf.new("R2013", units=4)
        base.layers.new("底图", dxfattribs={"color": 3})
        line = base.modelspace().add_line((1, 2), (3, 4), dxfattribs={"layer": "底图"})
        base.layouts.new("图框").add_circle((5, 6), 2)
        base.saveas(self.folder / "base.dxf")
        source = (self.folder / "base.dxf").read_bytes()
        loaded, warnings = cad.load_drawing(self.folder / "base.dxf")
        self.assertEqual(warnings, [])
        first = cad.export_dxf(self.folder / "copy.dxf", self.points, Settings(scale=1000), loaded)
        self.assertEqual(len(loaded.modelspace()), 1)
        self.assertEqual((self.folder / "base.dxf").read_bytes(), source)
        a = ezdxf.readfile(first)
        self.assertEqual(a.units, 4)
        self.assertEqual(tuple(a.entitydb[line.dxf.handle].dxf.end), (3, 4, 0))
        self.assertEqual(len(a.layouts.get("图框").query("CIRCLE")), 1)
        second = cad.export_dxf(self.folder / "copy2.dxf", self.points, Settings(scale=1000), a)
        b = ezdxf.readfile(second)
        names = [e.dxf.name for e in b.modelspace().query("INSERT")]
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)
        self.assertFalse(b.audit().has_errors)

    def test_cannot_overwrite_source(self):
        doc = ezdxf.new()
        path = self.folder / "base.dxf"
        doc.saveas(path)
        loaded, _ = cad.load_drawing(path)
        before = path.read_bytes()
        with self.assertRaisesRegex(cad.DrawingError, "不能覆盖"):
            cad.export_dxf(path, self.points, Settings(), loaded)
        self.assertEqual(before, path.read_bytes())

    def test_failed_write_keeps_previous_destination(self):
        path = self.folder / "target.dxf"
        path.write_bytes(b"original file")
        with patch.object(ezdxf.document.Drawing, "saveas", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cad.export_dxf(path, self.points, Settings())
        self.assertEqual(path.read_bytes(), b"original file")
        self.assertEqual(list(self.folder.glob(".coord-*")), [])

    def test_invalid_layer_empty_points_and_scale_rejected(self):
        for setting in (Settings(layer='bad"layer'), Settings(scale=float("inf")), Settings(lineweight=42)):
            with self.assertRaises(ValueError):
                cad.export_dxf(self.folder / "bad.dxf", self.points, setting)
        with self.assertRaises(ValueError):
            cad.export_dxf(self.folder / "empty.dxf", [], Settings())

    def test_no_connecting_line_option_and_unitless_scale(self):
        output = cad.export_dxf(self.folder / "no-line.dxf", self.points,
                                Settings(draw_line=False, scale=7))
        doc = ezdxf.readfile(output)
        self.assertEqual(doc.units, 0)
        self.assertEqual(len(doc.modelspace()), len(self.points))

    @staticmethod
    def separated_points():
        """32 synthetic unordered points in three unrelated, distant areas."""
        return [Point(f"area{area + 1}-{i + 1}", -54000 + area*900 + i*2.5,
                      -13200 - area*350 + (i % 3)*3, -2.295 - i*.125)
                for area, count in enumerate((12, 7, 13)) for i in range(count)]

    def lsp_lines_by_space(self, code):
        parse_lisp(code)
        model_lines, block_lines = [], []
        in_block = False
        for row in code.splitlines():
            if row.strip() == "(setq blockopen T)":
                in_block = True
            elif row.strip() == "(setq blockopen nil)":
                in_block = False
            elif "(entmake (list '(0 . \"LINE\")" in row:
                (block_lines if in_block else model_lines).append(row)
        return model_lines, block_lines

    def test_default_discrete_points_create_only_annotations_in_dxf_and_lsp(self):
        points = self.separated_points()
        original = list(points)
        self.assertEqual(len(points), 32)
        self.assertFalse(Settings().draw_line)
        for settings in (Settings(), Settings(closed=True)):
            with self.subTest(closed=settings.closed):
                output = cad.export_dxf(self.folder / "separate.dxf", points, settings)
                doc = ezdxf.readfile(output)
                self.assertFalse(doc.audit().has_errors)
                self.assertEqual({e.dxftype() for e in doc.modelspace()}, {"INSERT"})
                inserts = list(doc.modelspace())
                self.assertEqual(len(inserts), 32)
                for point, insert in zip(points, inserts):
                    self.assertEqual(tuple(insert.dxf.insert), (point.e, point.n, 0))
                    block = doc.blocks[insert.dxf.name]
                    self.assertEqual(len(block.query("CIRCLE")), 1)
                    self.assertEqual(len(block.query("LINE")), 4)
                    self.assertEqual(block.query("TEXT")[0].dxf.text, point.name)
                code = cad.generate_lsp(points, settings)
                model_lines, block_lines = self.lsp_lines_by_space(code)
                self.assertEqual(model_lines, [])
                self.assertEqual(len(block_lines), 32*4)
                self.assertEqual(code.count("(entmake (list '(0 . \"INSERT\")"), 32)
                self.assertEqual(code.count("(entmake (list '(0 . \"TEXT\")"), 32)
        self.assertEqual(points, original)  # Includes the original elevation.

    def test_explicit_list_connection_preserves_order_and_closed_option(self):
        points = self.separated_points()
        for closed in (False, True):
            with self.subTest(closed=closed):
                settings = Settings(draw_line=True, closed=closed)
                output = cad.export_dxf(self.folder / "connected.dxf", points, settings)
                doc = ezdxf.readfile(output)
                paths = list(doc.modelspace().query("LWPOLYLINE"))
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].closed, closed)
                self.assertEqual(list(paths[0].get_points("xy")), [(p.e, p.n) for p in points])
                model_lines, block_lines = self.lsp_lines_by_space(cad.generate_lsp(points, settings))
                pairs = list(zip(points, points[1:]))
                if closed:
                    pairs.append((points[-1], points[0]))
                self.assertEqual(len(model_lines), len(pairs))
                self.assertEqual(len(block_lines), 32*4)
                for row, (a, b) in zip(model_lines, pairs):
                    self.assertIn(f"(cons 10 (list {a.e:.15g} {a.n:.15g} 0.0))", row)
                    self.assertIn(f"(cons 11 (list {b.e:.15g} {b.n:.15g} 0.0))", row)

    def test_default_annotations_preserve_existing_drawing_lines(self):
        base = ezdxf.new("R2010", units=6)
        line = base.modelspace().add_line((0, 1), (2, 3))
        polyline = base.modelspace().add_lwpolyline([(10, 10), (15, 15), (20, 10)], close=True)
        output = cad.export_dxf(self.folder / "existing-lines.dxf", self.separated_points(), Settings(), base)
        doc = ezdxf.readfile(output)
        self.assertEqual(len(base.modelspace()), 2)
        self.assertEqual(len(doc.modelspace().query("LINE")), 1)
        self.assertEqual(len(doc.modelspace().query("LWPOLYLINE")), 1)
        self.assertEqual(tuple(doc.entitydb[line.dxf.handle].dxf.end), (2, 3, 0))
        self.assertEqual(list(doc.entitydb[polyline.dxf.handle].get_points()), list(polyline.get_points()))
        self.assertTrue(doc.entitydb[polyline.dxf.handle].closed)
        self.assertEqual(len(doc.modelspace().query("INSERT")), 32)

    def test_hidden_annotation_layer_rejected(self):
        base = ezdxf.new()
        base.layers.new("COORD_POINTS").off()
        with self.assertRaisesRegex(cad.DrawingError, "关闭、冻结或锁定"):
            cad.export_dxf(self.folder / "hidden.dxf", self.points, Settings(), base)

    def test_declared_base_unit_mismatch_rejected(self):
        base = ezdxf.new(units=4)
        with self.assertRaisesRegex(cad.DrawingError, "单位不一致"):
            cad.export_dxf(self.folder / "mismatch.dxf", self.points, Settings(), base)

    def test_text_anchor_matches_leader_direction(self):
        points = [Point(f"密集点{i}", 0, i*2) for i in range(20)]
        settings = Settings()
        labels = calc_labels(points, settings)
        self.assertTrue(any(p.direction < 0 for p in labels))
        output = cad.export_dxf(self.folder / "anchors.dxf", points, settings)
        doc = ezdxf.readfile(output)
        for p, insert in zip(labels, doc.modelspace().query("INSERT")):
            text = doc.blocks[insert.dxf.name].query("TEXT")[0]
            self.assertEqual(text.dxf.halign, 2 if p.direction < 0 else 0)
            anchor = text.dxf.align_point if p.direction < 0 else text.dxf.insert
            self.assertAlmostEqual(anchor.x, p.lx - p.x)
            self.assertAlmostEqual(anchor.y, p.ly - p.y + settings.text_height*0.05)

    def test_reverse_script_confirms_units_then_exports_standard_metre_header(self):
        code = cad.generate_reverse_lsp()
        parse_lisp(code)
        self.assertIn('units (getvar "INSUNITS")', code)
        self.assertIn('((= units 6) 1.0)', code)
        self.assertIn('((= units 4) 1000.0)', code)
        self.assertIn('((= units 5) 100.0)', code)
        self.assertIn('(initget 7)', code)  # required positive value for unknown units
        self.assertIn('(/ (car world) factor)', code)
        self.assertIn('(/ (cadr world) factor)', code)
        self.assertIn('(write-line "点名,东E,北N" fp)', code)
        self.assertNotIn('CAD单位,', code)

    def test_r12_upgraded_copy_retains_geometry(self):
        base = ezdxf.new("R12")
        base.modelspace().add_circle((50, 30), 2)
        output = cad.export_dxf(self.folder / "r12-new.dxf", self.points, Settings(), base)
        doc = ezdxf.readfile(output)
        self.assertGreaterEqual(doc.dxfversion, "AC1015")
        self.assertEqual(base.dxfversion, "AC1009")
        self.assertEqual(doc.modelspace().query("CIRCLE")[0].dxf.radius, 2)

    def test_dwg_fallback_returns_explicit_limit_and_passes_strict_flags(self):
        from types import SimpleNamespace
        import ezdwg
        source = self.folder / "simple.dwg"
        source.write_bytes(b"AC1015")
        def convert(source, target, **kwargs):
            self.assertEqual(kwargs["strict"], True)
            self.assertEqual(kwargs["include_unsupported"], True)
            doc = ezdxf.new()
            doc.modelspace().add_line((0, 0), (1, 1))
            doc.saveas(target)
            return SimpleNamespace(skipped_entities=0)
        with patch("ezdxf.addons.odafc.is_installed", return_value=False), patch.object(ezdwg, "to_dxf", side_effect=convert), patch.object(ezdwg, "read", return_value=SimpleNamespace(insunits=0)):
            doc, warnings = cad.load_drawing(source)
        self.assertEqual(len(doc.modelspace()), 1)
        self.assertEqual(doc.units, 0)
        self.assertIn("不保证", "\n".join(warnings))
        self.assertEqual(source.read_bytes(), b"AC1015")

    def test_dwg_skipped_geometry_rejected(self):
        from types import SimpleNamespace
        import ezdwg
        source = self.folder / "partial.dwg"
        source.write_bytes(b"AC1015")
        with patch("ezdxf.addons.odafc.is_installed", return_value=False), patch.object(ezdwg, "to_dxf", return_value=SimpleNamespace(skipped_entities=1, skipped_by_type={"PROXY": 1})):
            with self.assertRaisesRegex(cad.DrawingError, "无法确认图形完整"):
                cad.load_drawing(source)

    def test_lsp_is_balanced_escaped_and_only_loads_commands(self):
        code = cad.generate_lsp(self.points, Settings())
        forms = parse_lisp(code)
        commands = [f for f in forms if isinstance(f, list) and f and f[0] == "defun"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1], "C:BINDLINE")
        self.assertNotIn("(C:BINDLINE)", code)
        self.assertIn(r'测点A\"\\,', code)
        self.assertIn("(370 . 50)", code)
        self.assertIn("(10 0.0 0.0 0.0)", code)
        self.assertIn("vla-StartUndoMark", code)
        self.assertIn("vla-EndUndoMark", code)
        self.assertTrue(any(f[0] == "princ" for f in forms[-2:]))
        reverse = parse_lisp(cad.generate_reverse_lsp('测试层'))
        self.assertTrue(any(f[0] == "defun" and f[1] == "C:EXPORTCOORD" for f in reverse))

    def test_reverse_lisp_actual_transform_matches_ezdxf(self):
        code = cad.generate_reverse_lsp()
        for base, center, location, scales, rotation, extrusion in [
            ((100, 200, 0), (102, 203, 0), (-50000, -13000, 0), (2, 3, 1), 37, (0, 0, 1)),
            ((0, 0, 0), (0, 0, 0), (17, 8, 3), (-2, 2, 4), 90, (0, 0, -1)),
            ((3, -7, 2), (10, 2, 1), (40, 20, 30), (0.5, 2, -1), 126, (0.2, 0.4, 0.8)),
        ]:
            doc = ezdxf.new()
            block = doc.blocks.new("B", base_point=base)
            block.add_point(center)
            insert = doc.modelspace().add_blockref("B", location, dxfattribs={
                "xscale": scales[0], "yscale": scales[1], "zscale": scales[2],
                "rotation": rotation, "extrusion": extrusion,
            })
            expected = insert.matrix44().transform(center)
            actual = run_lisp_world(code, list(center), list(base), {
                10: list(location), 41: scales[0], 42: scales[1], 43: scales[2], 50: math.radians(rotation),
            }, extrusion)
            for a, b in zip(actual, expected):
                self.assertAlmostEqual(a, b, places=8)


if __name__ == "__main__":
    unittest.main()
