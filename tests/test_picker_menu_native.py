"""Opt-in isolated native tests for the road menu's temporary pointer cursor.

Ordinary choices and road names use real getkword/getstring calls. Cancellation
injects native quit at the input boundary; this is not a GUI keyboard Esc test.
The error case calls getkword with an invalid argument to raise a native error.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.native_support import find_core_console, synthetic_seed
from coordtool.picker_lisp import generate_picker_lsp, parse_event, picker_commands


@unittest.skipUnless(os.environ.get("COORDTOOL_NATIVE_MENU_TEST") == "1",
                     "Set COORDTOOL_NATIVE_MENU_TEST=1 to run isolated native CAD checks.")
class NativeMenuCursorTests(unittest.TestCase):
    def test_menu_choices_restore_cursor_and_preserve_accepted_annotations(self):
        console = find_core_console()
        if not console:
            self.skipTest("AutoCAD Core Console is unavailable.")
        choices = {
            "continue": (["C", "101,201,-4", "Q"], ["ADD", "ADD"], 1, 0),
            "quit": (["Q"], ["ADD"], 1, 0),
            "enter_new": (["", "新道路", "Q"], ["ADD", "ROAD"], 1, 1),
            "new": (["N", "新道路", "Q"], ["ADD", "ROAD"], 1, 1),
            "blank_name": (["N", "", "C", "Q"], ["ADD"], 2, 1),
            "cancel": ([], ["ADD"], 1, 0),
            "error": ([], ["ADD", "ERROR"], 1, 0),
        }
        instrumentation = r'''
(defun qa-input-log (kind / stream)
  (setq stream (open (strcat qa-directory "/inputs.tsv") "a"))
  (write-line (strcat kind "\t" (itoa (getvar "CURSORTYPE")) "\t"
    (if (and (= qa-size (getvar "CURSORSIZE")) (= qa-dyn (getvar "DYNMODE"))) "SAME" "CHANGED") "\t"
    (if *cgp-done* "DONE" "WAITING")) stream)
  (close stream))
(defun qa-getpoint (message) (qa-input-log "POINT") (getpoint message))
(defun qa-getkword (message)
  (qa-input-log "MENU")
  (cond ((= qa-mode "cancel") (quit)) ((= qa-mode "error") (getkword 17)) (T (getkword message))))
(defun qa-getstring (cr message) (qa-input-log "NAME") (getstring cr message))
(defun cgp-place (pt)
  (cgp-accept pt (list (+ (car pt) 10.0) (+ (cadr pt) 10.0) (caddr pt)))
  (if (not qa-kept) (setq qa-kept (nth 2 (car *cgp-history*)))))
(defun qa-menu-check (/ stream)
  (setq stream (open (strcat qa-directory "/flags.tsv") "w"))
  (write-line (strcat (itoa (getvar "CURSORTYPE")) "\t"
    (if *cgp-done* "DONE" "ACTIVE") "\t" (if *cgp-failed* "FAILED" "NORMAL") "\t"
    (if (and (= (length qa-kept) 6) (vl-every 'entget qa-kept)) "KEPT" "LOST") "\t"
    (if (equal qa-old-data (entget qa-old)) "OLD_SAME" "OLD_CHANGED") "\t"
    (if (and (= qa-size (getvar "CURSORSIZE")) (= qa-dyn (getvar "DYNMODE"))) "SAME" "CHANGED")) stream)
  (close stream) (princ))
'''
        with tempfile.TemporaryDirectory(prefix="cgp-menu-cursor-native-") as temporary:
            seed = synthetic_seed(temporary)
            original_hash = hashlib.sha256(seed.read_bytes()).hexdigest()
            directory = Path(temporary)
            (directory / "profile").mkdir()
            shutil.copyfile(seed, directory / "input.dxf")
            script = []
            cases = []
            for cursor in (0, 1):
                for mode, (inputs, events, menus, names) in choices.items():
                    work = directory / f"{cursor}_{mode}"
                    work.mkdir()
                    cases.append((work, cursor, mode, events, menus, names))
                    source = generate_picker_lsp(work / "events.tsv", work / "stop",
                                                 "原道路", 1, 1, 0, 0, 2)
                    self.assertIn("(defun cgp-road-menu-loop ", source)
                    self.assertEqual(source.count("(getkword "), 1)
                    source = source.replace("(getpoint ", "(qa-getpoint ")
                    source = source.replace("(getkword ", "(qa-getkword ")
                    source = source.replace("(getstring ", "(qa-getstring ")
                    script.extend(picker_commands(source, auto_start=False))
                    script.extend(picker_commands(instrumentation, auto_start=False))
                    script.append(f'(setq qa-directory "{work.as_posix()}" qa-mode "{mode}" qa-kept nil)\n')
                    script.append(f'(setvar "CURSORTYPE" {cursor})\n')
                    script.append('(setq qa-size (getvar "CURSORSIZE") qa-dyn (getvar "DYNMODE"))\n')
                    script.append("(setq qa-old (entmakex '((0 . \"LINE\") (10 -1.0 -2.0 -3.0) (11 -4.0 -5.0 -6.0))))\n")
                    script.append('(setq qa-old-data (entget qa-old))\n')
                    script.append("\n".join(["CGPICK", "100,200,-3", ""] + inputs) + "\n")
                    script.append('(qa-menu-check)\n')
            script.append('_.QUIT\n_Yes\n')
            (directory / "verify.scr").write_text("".join(script), encoding="utf-8-sig")
            with (directory / "native.log").open("wb") as log:
                process = subprocess.Popen(
                    [str(console), "/i", str(directory / "input.dxf"),
                     "/s", str(directory / "verify.scr"), "/isolate", "CGP_MENU_CURSOR",
                     str(directory / "profile")], cwd=directory, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    returncode = process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    self.fail("Native menu input test timed out.")
            self.assertEqual(returncode, 0)
            for work, cursor, mode, expected, menus, names in cases:
                with self.subTest(cursor=cursor, mode=mode):
                    events = [parse_event(line) for line in
                              (work / "events.tsv").read_text(encoding="ascii").splitlines()]
                    self.assertEqual([event["type"] for event in events], ["READY"] + expected + ["DONE"])
                    if "ROAD" in expected:
                        self.assertEqual(next(event["road"] for event in events if event["type"] == "ROAD"), "新道路")
                    inputs = [line.split("\t") for line in
                              (work / "inputs.tsv").read_text(encoding="ascii").splitlines()]
                    self.assertEqual(sum(row[0] == "MENU" for row in inputs), menus)
                    self.assertEqual(sum(row[0] == "NAME" for row in inputs), names)
                    for kind, actual_cursor, variables, lifecycle in inputs:
                        self.assertEqual(actual_cursor, str(cursor if kind == "POINT" else 1))
                        self.assertEqual((variables, lifecycle), ("SAME", "WAITING"))
                    self.assertEqual((work / "flags.tsv").read_text(encoding="ascii").strip(),
                                     f"{cursor}\tDONE\t" + ("FAILED" if mode == "error" else "NORMAL") +
                                     "\tKEPT\tOLD_SAME\tSAME")
            self.assertEqual(hashlib.sha256(seed.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
