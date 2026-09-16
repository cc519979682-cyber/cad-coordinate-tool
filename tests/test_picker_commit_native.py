"""Isolated Core Console fault injection at ADD/UNDO commit boundaries.

These are deterministic native interruptions, not timing claims about a GUI
Escape key. No user CAD process is connected or original drawing modified.
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


@unittest.skipUnless(os.environ.get("COORDTOOL_NATIVE_COMMIT_TEST") == "1",
                     "Set COORDTOOL_NATIVE_COMMIT_TEST=1 for isolated native commit checks.")
class NativeCommitTests(unittest.TestCase):
    def test_interruptions_reconcile_entities_state_and_complete_journal(self):
        console = find_core_console()
        if not console:
            self.skipTest("AutoCAD Core Console is unavailable.")
        # Each hook cancels only once, allowing the real error handler to
        # replay an unfinished state update or entity deletion idempotently.
        cases = [
            ("add_before_write", "entmod", "data", "(setq qa-target *cgp-preview* qa-target-group (nth 3 (cadr *cgp-transaction*)))",
             "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))", 1, 0, False),
            ("add_after_write", "cgp-record-add", "record", "(setq qa-target (nth 2 record) qa-target-group (nth 3 record))",
             "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))", 2, 6, False),
            ("undo_before_write", "cgp-emit", "row", "",
             "(cgp-undo)", 2, 6, False),
            ("undo_after_write", "cgp-record-undo", "record", "",
             "(cgp-undo)", 1, 0, True),
            ("undo_mid_delete", "entdel", "entity", "(apply 'qa-original (list entity))",
             "(cgp-undo)", 1, 0, True),
            # cgp-write throws after closing its file. Recovery must inspect
            # the complete durable row, rather than trusting the return flag.
            ("add_after_close", "cgp-write", "row",
             "(setq qa-target *cgp-preview* qa-target-group (nth 3 (cadr *cgp-transaction*))) (apply 'qa-original (list row))",
             "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))", 2, 6, False),
            ("undo_after_close", "cgp-write", "row", "(apply 'qa-original (list row))",
             "(cgp-undo)", 1, 0, True),
            ("unverified_after_write", "cgp-record-add", "record",
             "(setq qa-target (nth 2 record) qa-target-group (nth 3 record)) (defun cgp-row-written (row) 'UNREADABLE)",
             "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))", 1, 6, False),
            ("add_write_error", "cgp-write", "row",
             "(setq qa-target *cgp-preview* qa-target-group (nth 3 (cadr *cgp-transaction*))) (/ 1 0)",
             "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))", 1, 0, False),
        ]
        with tempfile.TemporaryDirectory(prefix="cgp-commit-native-") as temporary:
            seed = synthetic_seed(temporary)
            original_hash = hashlib.sha256(seed.read_bytes()).hexdigest()
            for name, function, argument, during, trigger, kept, target_count, has_undo in cases:
                with self.subTest(name=name):
                    directory = Path(temporary) / name
                    directory.mkdir()
                    (directory / "profile").mkdir()
                    shutil.copyfile(seed, directory / "input.dxf")
                    source = generate_picker_lsp(directory / "events.tsv", directory / "stop",
                                                 "道路", 1, 1, 0, 0, 2)
                    commands = picker_commands(source, auto_start=False)
                    if function in ("entmod", "entdel"):
                        # Instrument only the mutation call site. AutoLISP
                        # user-function SUBR values cannot be applied after
                        # redefining that symbol, so no captured function proxy.
                        owner = "cgp-accept" if function == "entmod" else "cgp-delete-group"
                        commands = [command.replace(f"({function} ", f"(qa-{function} ")
                                    if command.startswith(f"(defun {owner} ") else command
                                    for command in commands]
                        original = f"(defun qa-original ({argument}) ({function} {argument}))\n"
                        hooked = "qa-" + function
                    else:
                        original = next(command for command in commands
                                        if command.startswith(f"(defun {function} "))
                        original = original.replace(f"(defun {function} ", "(defun qa-original ", 1)
                        hooked = function
                    main = next(command for command in commands if command.startswith("(defun c:CGPICK "))
                    handler = picker_commands(main[main.index(")") + 1:-2], auto_start=False)[0]
                    setup = (f"(defun qa-{function} ({argument}) ({function} {argument}))\n"
                             if function in ("entmod", "entdel") else "")
                    setup += """(cgp-begin)
(cgp-accept '(100.0 200.0 -3.0) '(110.0 210.0 -3.0))
(setq qa-old (nth 2 (car *cgp-history*)))
(setq qa-old-group (nth 3 (car *cgp-history*)))
"""
                    if name.startswith("undo"):
                        setup += "(cgp-accept '(120.0 220.0 -3.0) '(130.0 230.0 -3.0))\n(setq qa-target (nth 2 (car *cgp-history*)) qa-target-group (nth 3 (car *cgp-history*)))\n"
                    setup += original + "(setq qa-inject T)\n"
                    setup += (f"(defun {hooked} ({argument}) (if qa-inject "
                              f"(progn (setq qa-inject nil) {during} (quit)) "
                              f"(apply 'qa-original (list {argument}))))\n")
                    # Calling recovery twice proves no duplicate registration
                    # and no entdel toggle resurrecting an already erased group.
                    checks = """(cgp-recover-transaction)
(cgp-recover-transaction)
(cgp-finish)
(progn (setq qa-group-file (open "groups.tsv" "w")) (write-line (strcat (if (and (entget (caddr qa-old-group)) (dictsearch (car qa-old-group) (cadr qa-old-group))) "OLD_OK" "OLD_LOST") "\\t" (if (entget (caddr qa-target-group)) "LIVE" "ERASED") "\\t" (if (dictsearch (car qa-target-group) (cadr qa-target-group)) "REGISTERED" "REMOVED")) qa-group-file) (close qa-group-file) (princ))
_.AUDIT
_No
(progn (setq qa-file (open "flags.tsv" "w")) (write-line (strcat (if (vl-every 'entget qa-old) "KEPT" "LOST") "\\t" (itoa (length *cgp-history*)) "\\t" (itoa (length (vl-remove-if-not 'entget qa-target))) "\\t" (itoa (length *cgp-stack*)) "\\t" (itoa *cgp-index*) "\\t" (if *cgp-transaction* "PENDING" "CLEAR") "\\t" (if *cgp-failed* "FAILED" "NORMAL")) qa-file) (close qa-file) (princ))
_.QUIT
_Yes
"""
                    (directory / "verify.scr").write_text("".join(commands) + handler + setup + trigger + "\n" + checks,
                                                           encoding="utf-8-sig")
                    with (directory / "native.log").open("wb") as log:
                        process = subprocess.Popen(
                            [str(console), "/i", str(directory / "input.dxf"), "/s", str(directory / "verify.scr"),
                             "/isolate", "CGP_COMMIT_" + name, str(directory / "profile")], cwd=directory,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        try:
                            returncode = process.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                            self.fail("Native commit test timed out: " + name)
                    log_text = (directory / "native.log").read_bytes().decode("utf-16-le", errors="replace")
                    self.assertEqual(returncode, 0, log_text[-6000:])
                    flags = (directory / "flags.tsv").read_text(encoding="ascii").strip()
                    pending = "PENDING" if name == "unverified_after_write" else "CLEAR"
                    failed = name in ("unverified_after_write", "add_write_error")
                    state = "FAILED" if failed else "NORMAL"
                    self.assertEqual(flags, f"KEPT\t{kept}\t{target_count}\t{kept}\t{kept+1}\t{pending}\t{state}", log_text[-6000:])
                    groups = (directory / "groups.tsv").read_text(encoding="ascii").strip()
                    self.assertEqual(groups, "OLD_OK\t" + ("LIVE\tREGISTERED" if target_count else "ERASED\tREMOVED"))
                    events = [parse_event(line) for line in (directory / "events.tsv").read_text(encoding="ascii").splitlines()]
                    expected_adds = 1 if name in ("add_before_write", "add_write_error") else 2
                    expected = ["READY"] + ["ADD"] * expected_adds + (["UNDO"] if has_undo else []) + (["ERROR"] if failed else []) + ["DONE"]
                    self.assertEqual([event["type"] for event in events], expected, log_text[-6000:])
            self.assertEqual(hashlib.sha256(seed.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
