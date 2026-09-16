"""Opt-in native handler regression; the Chinese message was captured by real GUI Esc.

This tests the exact production handler in private Core Console drawings, not
an end-to-end GUI Escape/ZB flow. The drawing-error case raises an actual native
geometry exception. No user AutoCAD application is connected or activated.
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


@unittest.skipUnless(os.environ.get("COORDTOOL_NATIVE_CANCEL_TEST") == "1",
                     "Set COORDTOOL_NATIVE_CANCEL_TEST=1 to run isolated native CAD checks.")
class NativeCancelHandlerTests(unittest.TestCase):
    def test_localized_cancel_and_real_drawing_error_have_distinct_results(self):
        console = find_core_console()
        if not console:
            self.skipTest("AutoCAD Core Console is unavailable.")
        with tempfile.TemporaryDirectory(prefix="cgp-cancel-native-") as temporary:
            seed = synthetic_seed(temporary)
            original_hash = hashlib.sha256(seed.read_bytes()).hexdigest()
            cases = [
                # Exact GUI-captured code points: 20989,25968,24050,21462,28040.
                ("gui_chinese_message", '(*error* "函数已取消")', False),
                ("english_message", '(*error* "Function cancelled")', False),
                ("native_quit", '(quit)', False),
                ("native_drawing_error", "(cgp-accept '(1.0) '(2.0))", True),
                ("misleading_error", '(*error* "Cannot cancel an invalid drawing operation.")', True),
            ]
            for name, trigger, failure in cases:
                with self.subTest(name=name):
                    directory = Path(temporary) / name
                    directory.mkdir()
                    (directory / "profile").mkdir()
                    shutil.copyfile(seed, directory / "input.dxf")
                    source = generate_picker_lsp(directory / "events.tsv", directory / "stop",
                                                 "原道路", 1, 1, 0, 0, 2)
                    commands = picker_commands(source, auto_start=False)
                    main = next(command for command in commands
                                if command.startswith("(defun c:CGPICK "))
                    # Extract and execute the unchanged production local handler.
                    body = main[main.index(")") + 1:-2]
                    handler = picker_commands(body, auto_start=False)[0]
                    self.assertTrue(handler.startswith("(defun *error* (message)"))
                    setup = """(cgp-begin)
(cgp-accept '(100.0 200.0 -3.0) '(110.0 210.0 -3.0))
(setq qa-kept (nth 2 (car *cgp-history*)))
"""
                    checks = """(progn (setq qa-file (open "flags.tsv" "w")) (write-line (strcat (if *cgp-done* "DONE" "ACTIVE") "\\t" (if *cgp-failed* "FAILED" "NORMAL") "\\t" (if (and (= (length qa-kept) 6) (vl-every 'entget qa-kept)) "KEPT" "LOST")) qa-file) (close qa-file) (princ))
_.QUIT
_Yes
"""
                    script = "".join(commands) + handler + setup + trigger + "\n" + checks
                    (directory / "verify.scr").write_text(script, encoding="utf-8-sig")
                    with (directory / "native.log").open("wb") as log:
                        process = subprocess.Popen(
                            [str(console), "/i", str(directory / "input.dxf"),
                             "/s", str(directory / "verify.scr"), "/isolate", "CGP_CANCEL_" + name,
                             str(directory / "profile")], cwd=directory,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        try:
                            returncode = process.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                            self.fail(f"Native cancellation check timed out: {name}")
                    self.assertEqual(returncode, 0)
                    events = [parse_event(line) for line in
                              (directory / "events.tsv").read_text(encoding="ascii").splitlines()]
                    expected = ["READY", "ADD"] + (["ERROR"] if failure else []) + ["DONE"]
                    self.assertEqual([event["type"] for event in events], expected)
                    self.assertEqual((directory / "flags.tsv").read_text(encoding="ascii").strip(),
                                     "DONE\t" + ("FAILED" if failure else "NORMAL") + "\tKEPT")
            self.assertEqual(hashlib.sha256(seed.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
