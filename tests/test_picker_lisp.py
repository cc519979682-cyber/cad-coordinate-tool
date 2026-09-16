import math
from pathlib import Path
import re
import tempfile
import unittest

from coordtool.picker_lisp import generate_picker_lsp, parse_event, picker_expression, picker_commands


class PickerProtocolTests(unittest.TestCase):
    def test_new_add_includes_segment_but_legacy_record_remains_compatible(self):
        line = "ADD\t1\t3\t1\t2\t-3\t4\t5\t6\t2\t1"
        self.assertNotIn("road_id", parse_event(line))
        self.assertEqual(parse_event(line + "\t0")["road_id"], 0)
        self.assertEqual(parse_event(line + "\t12")["road_id"], 12)
        for road_id in ("-1", "00", "1.2", "2147483648", "text"):
            with self.subTest(road_id=road_id), self.assertRaises(ValueError):
                parse_event(line + "\t" + road_id)

    def test_unicode_road_uses_ascii_scalar_codes_with_exact_roundtrip(self):
        road = '示例,道路 "引号" \\ \\双\\\\路😀'
        codes = ",".join(str(ord(c)) for c in road)
        record = "ROAD\t3\t27\t" + codes
        self.assertTrue(record.isascii())
        self.assertEqual(parse_event(record), {"type": "ROAD", "road_id": 3, "start": 27, "road": road})

    def test_road_records_reject_invalid_scalar_names_and_numbering(self):
        for record in ["ROAD\t0\t1\t65", "ROAD\t1\t0\t65", "ROAD\t1\t1\t",
                       "ROAD\t1\t1\t0", "ROAD\t1\t1\t9", "ROAD\t1\t1\t127",
                       "ROAD\t1\t1\t133", "ROAD\t1\t1\t55357,56832",
                       "ROAD\t1\t1\t1114112", "ROAD\t1\t1\t-1",
                       "ROAD\t1\t1\t65,", "ROAD\t1\t1\t065", "ROAD\t1\t1\t32,65",
                       "ROAD\t1\t1\t65,32", "ROAD\t1\t1\t" + ",".join(["65"] * 101)]:
            with self.subTest(record=record), self.assertRaises(ValueError):
                parse_event(record)

    def test_add_preserves_signed_wcs_elevation_and_manual_geometry(self):
        event = parse_event("ADD\t12\t3\t-12345.678\t-23456.75\t-1.875\t-12340.123456789\t-23450.5\t-12320.25\t2\t.5\r\n")
        self.assertEqual((event["serial"], event["index"]), (12, 3))
        self.assertEqual(event["z"], -1.875)
        self.assertEqual(event["lx"], -12340.123456789)
        self.assertEqual(event["arrow"], .5)

    def test_lifecycle_events(self):
        for line, expected in [("READY\n", {"type": "READY"}),
                               ("UNDO\t25", {"type": "UNDO", "serial": 25}),
                               ("DONE\r\n", {"type": "DONE"}),
                               ("ERROR\tCAD is busy.", {"type": "ERROR", "message": "CAD is busy."})]:
            with self.subTest(line=line):
                self.assertEqual(parse_event(line), expected)

    def test_native_error_codes_remain_ascii_and_are_displayed_in_chinese(self):
        cases = {
            "Please activate modelspace before picking.": "请先切换到模型空间",
            "Unable to create picker layer or text style.": "无法创建取点图层或文字样式",
            "Unable to draw point annotation.": "无法绘制本次坐标标注",
            "Unable to create point group.": "无法创建本次坐标标注组",
            "Native picking failed; existing accepted points were preserved.": "取点发生错误",
        }
        for code, message in cases.items():
            record = "ERROR\t" + code
            with self.subTest(code=code):
                self.assertTrue(record.isascii())
                self.assertIn(message, parse_event(record)["message"])

    def test_invalid_event_never_reaches_coordinate_model(self):
        base = "ADD\t1\t1\t1\t2\t3\t4\t5\t6\t2\t1"
        bad = ["", "READY\textra", "DONE\nREADY", "ERROR\t", "ERROR\t中文",
               "UNKNOWN", "UNDO\t0", "UNDO\t-1", "UNDO\t1.0", "UNDO\t 1",
               "UNDO\t2147483648", base + "\textra", base.replace("ADD\t1", "ADD\t01", 1)]
        fields = base.split("\t")
        for index in range(3, 11):
            for value in ["nan", "inf", "-inf", "1e9999", "1_000", " 1", "1 ", ""]:
                candidate = fields.copy()
                candidate[index] = value
                bad.append("\t".join(candidate))
        for index in (9, 10):
            candidate = fields.copy()
            candidate[index] = "0"
            bad.append("\t".join(candidate))
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    parse_event(line)


class PickerProgramTests(unittest.TestCase):
    def make_source(self, **kwargs):
        defaults = dict(event_path=Path(tempfile.gettempdir()) / "事件;quote'\\journal.tsv",
                        stop_path=Path(tempfile.gettempdir()) / "stop.signal",
                        road='示例路; (quit) "引号" \\ A', start=3,
                        scale=1000, offset_e=12.5, offset_n=-37.0, height=2)
        defaults.update(kwargs)
        return generate_picker_lsp(**defaults)

    def test_compaction_preserves_injected_looking_label_as_one_string(self):
        source = self.make_source()
        command = picker_expression(source)
        self.assertTrue(command.startswith("(progn "))
        self.assertTrue(command.endswith(" (c:CGPICK))\n"))
        self.assertIn('"示例路; (quit) \\"引号\\" \\\\ A"', command)
        self.assertNotIn("@@", command)
        self.assertEqual(command.count("\n"), 1)

    def test_source_load_does_not_start_interactive_getpoint(self):
        command = picker_expression(self.make_source(), auto_start=False)
        self.assertNotIn("(c:CGPICK)", command)

    def test_command_prompts_use_chinese_and_preserve_shortcut_keywords(self):
        source = self.make_source(road="道路")
        self.assertIn('[撤销(U)/换路(N)/结束(Q)]', source)
        self.assertIn('[换路(N)/继续(C)/结束(Q)] <换路>', source)
        self.assertIn('(initget "Undo New Quit 撤销 换路 结束")', source)
        self.assertIn('(initget "New Continue Quit 换路 继续 结束")', source)
        self.assertIn('回车：结束本道路，进入道路菜单', source)
        for literal in re.findall(r'\((?:prompt|getpoint|getkword|getstring)(?: T)?\s+"([^"\n]*)"', source):
            with self.subTest(prompt=literal):
                self.assertRegex(literal, r'[\u4e00-\u9fff]')
        for record in re.findall(r'\(cgp-emit "(ERROR\\t[^"\n]*)"\)', source):
            self.assertTrue(record.isascii())

    def test_both_road_name_entries_share_temporary_pointer_input(self):
        source = self.make_source()
        commands = picker_commands(source, auto_start=False)
        reader = next(command for command in commands if command.startswith('(defun cgp-with-pointer '))
        name_input = next(command for command in commands if command.startswith('(defun cgp-input-road '))
        self.assertIn('(cgp-with-pointer ', name_input)
        for entry in ('cgp-get-road-name', 'cgp-zb-get-name'):
            command = next(command for command in commands if command.startswith(f'(defun {entry} '))
            self.assertIn('(cgp-input-road ', command)
            self.assertNotIn('(getstring ', command)
        self.assertIn('(getvar "CURSORTYPE")', reader)
        self.assertIn('(setvar "CURSORTYPE" 1)', reader)
        self.assertIn("(vl-catch-all-apply 'setvar (list \"CURSORTYPE\" cursor))", reader)
        self.assertNotIn('CURSORSIZE', reader)
        self.assertNotIn('DYNMODE', reader)

    def test_road_menu_keeps_pointer_for_the_whole_loop_and_classifies_cancel(self):
        commands = picker_commands(self.make_source(), auto_start=False)
        menu = next(command for command in commands if command.startswith('(defun cgp-road-menu '))
        loop = next(command for command in commands if command.startswith('(defun cgp-road-menu-loop '))
        self.assertIn("(cgp-with-pointer 'cgp-road-menu-loop nil)", menu)
        self.assertIn('(cgp-cancelled-p (vl-catch-all-error-message result))', menu)
        self.assertIn('(setq *cgp-abort* T)', menu)
        self.assertIn('(getkword ', loop)
        self.assertIn('(cgp-new-road)', loop)
        self.assertNotIn('CURSORTYPE', loop)

    def test_cancel_handler_recognizes_localized_exact_messages_without_errno_or_wildcards(self):
        commands = picker_commands(self.make_source(), auto_start=False)
        matcher = next(command for command in commands if command.startswith('(defun cgp-cancelled-p '))
        handler = next(command for command in commands if command.startswith('(defun c:CGPICK '))
        self.assertIn('"函数已取消"', matcher)
        self.assertIn('"FUNCTION CANCELLED"', matcher)
        self.assertIn('"QUIT / EXIT ABORT"', matcher)
        self.assertIn('(member ', matcher)
        self.assertNotIn('wcmatch', matcher)
        self.assertNotIn('ERRNO', matcher)
        self.assertIn('(not (cgp-cancelled-p message))', handler)
        self.assertNotIn('*CANCEL*', handler)

    def test_road_resembling_template_token_is_preserved(self):
        source = self.make_source(road="ROAD@@SCALE@@")
        self.assertIn('"ROAD@@SCALE@@"', source)

    def test_many_existing_roads_stay_within_native_command_buffer(self):
        roads = {f"已有道路{i}": i + 1 for i in range(600)}
        commands = picker_commands(self.make_source(road_numbers=roads), auto_start=False)
        self.assertGreater(len(commands), 600)
        self.assertLess(max(map(len, commands)), 3000)
        self.assertIn('"已有道路599" 600', "".join(commands))

    def test_initial_road_baselines_casefold_and_preserve_first_spelling(self):
        source = self.make_source(road="Road", start=25, road_numbers={"ROAD": 10, "road": 20, "Other": 3})
        self.assertIn('(list "ROAD" 25)', source)
        self.assertNotIn('(list "road" 20)', source)
        source = self.make_source(road_numbers={"Exhausted": 2147483648})
        self.assertIn('(list "Exhausted" 2147483648.0)', source)

    def test_optional_zb_bridge_defaults_remain_usable_by_existing_callers(self):
        source = self.make_source()
        commands = picker_commands(source, auto_start=False)
        self.assertIn("*cgp-zb-dir* nil *cgp-zb-token* nil", source)
        self.assertFalse(any(command.strip() == "(c:ZB)" for command in commands))

    def test_zb_bridge_configuration_is_literal_and_keeps_native_commands_bounded(self):
        directory = Path(tempfile.gettempdir()) / '续接;(folder)'
        token = "c962fa73580b4a38a3c7f5bdbe7bf251"
        source = self.make_source(road="道路@@ZBTOKEN@@", bridge_dir=directory, bridge_token=token)
        commands = picker_commands(source, auto_start=False)
        self.assertIn('"道路@@ZBTOKEN@@"', source)
        self.assertIn(f'*cgp-zb-token* "{token}"', source)
        self.assertIn('续接;(folder)', "".join(commands))
        self.assertLess(max(map(len, commands)), 3000)

    def test_zb_bridge_rejects_unpaired_or_non_ascii_protocol_configuration(self):
        directory = Path(tempfile.gettempdir()) / "coordinate-zb"
        for config in [dict(bridge_dir=directory), dict(bridge_token="test"),
                       dict(bridge_dir=directory, bridge_token=""),
                       dict(bridge_dir=directory, bridge_token="中文"),
                       dict(bridge_dir=directory, bridge_token="a\tb"),
                       dict(bridge_dir=directory, bridge_token="a\nb"),
                       dict(bridge_dir=directory, bridge_token="a b"),
                       dict(bridge_dir=directory, bridge_token="a\x7f"),
                       dict(bridge_dir=directory, bridge_token=False)]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.make_source(**config)

    def test_native_commands_split_at_expression_boundaries_only(self):
        commands = picker_commands(self.make_source())
        self.assertGreater(len(commands), 15)
        self.assertLess(max(map(len, commands)), 3000)
        self.assertEqual(commands[-1], "(c:CGPICK)\n")
        self.assertEqual(picker_commands('(princ "a;()\\\"b")\n(princ)', auto_start=False),
                         ['(princ "a;()\\\"b")\n', '(princ)\n'])
        for command in commands:
            picker_expression(command, auto_start=False)

    def test_comments_and_escaped_quote_are_not_confused(self):
        source = '; ignored (\n(princ "a;\\\"b\\\\c") ; ignored )\n(princ)'
        self.assertEqual(picker_expression(source, auto_start=False),
                         '(progn (princ "a;\\\"b\\\\c") (princ))\n')

    def test_compactor_rejects_incomplete_source(self):
        for source in ["", "; comment", "(princ", ")(", '(princ "unfinished)']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                picker_expression(source)

    def test_invalid_config_rejected_before_cad_command_creation(self):
        for config in [dict(road=""), dict(road="a\nb"), dict(road="a\tb"),
                       dict(start=0), dict(start=True), dict(start=1.5),
                       dict(scale=0), dict(scale=math.inf), dict(height=-1),
                       dict(offset_e=math.nan), dict(arrow_factor=0),
                       dict(road="a" * 101), dict(road="a\x7fb"), dict(road="a\ud800b"),
                       dict(road_numbers=[]), dict(road_numbers={"A": 0}),
                       dict(road_numbers={"A": True}), dict(road_numbers={"A": 2147483649}),
                       dict(event_path="x", stop_path="x")]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.make_source(**config)


if __name__ == "__main__":
    unittest.main()
