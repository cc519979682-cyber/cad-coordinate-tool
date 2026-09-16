"""Native conversion safeguards; fake converters never launch real AutoCAD."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import ezdxf

from coordtool import native_dwg as native


class NativeDwgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-dwg-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "原图(2) & name.dwg"
        self.source.write_bytes(b"AC1032 source fixture")
        self.console = self.folder / "AutoCAD 2026" / "accoreconsole.exe"
        self.console.parent.mkdir()
        self.console.write_bytes(b"not executable: test fixture")
        self.cache = self.folder / "cache"

    def fake_conversion(self, executable, work, timeout):
        self.assertEqual((work / "native_input.dwg").read_bytes(), self.source.read_bytes())
        self.assertNotEqual(work / "native_input.dwg", self.source)
        doc = ezdxf.new("R2018", units=0)
        doc.modelspace().add_line((10, 20), (30, 40))
        doc.layouts.get("Layout1").add_viewport((0, 0), (10, 10), (5, 5), 10)
        doc.saveas(work / "native_output.dxf")
        log = work / "native_console.log"
        log.write_bytes(b"native converter completed")
        return 0, log

    def convert(self):
        return native.convert_native_dwg(self.source, cache_dir=self.cache, executable=self.console)

    def test_find_console_beside_registered_autocad(self):
        with patch.object(native, "_registered_autocad", return_value=str(self.console.parent / "acad.exe")):
            self.assertEqual(native.find_core_console(), self.console.resolve())

    def test_source_unchanged_output_valid_and_cache_hits(self):
        before = self.source.read_bytes()
        with patch.object(native, "_run_console", side_effect=self.fake_conversion) as run:
            output = self.convert()
            same = self.convert()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(output, same)
        self.assertEqual(self.source.read_bytes(), before)
        doc = ezdxf.readfile(output)
        self.assertEqual(len(doc.modelspace()), 1)
        self.assertEqual(len(doc.layouts.get("Layout1").query("VIEWPORT")), 1)
        self.assertEqual(doc.units, 0)
        meta = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_sha256"], native._sha256(self.source))
        self.assertEqual(meta["dxf_sha256"], native._sha256(output))
        self.assertEqual(list(self.cache.glob("native-*")), [])

    def test_corrupt_cache_is_rebuilt(self):
        with patch.object(native, "_run_console", side_effect=self.fake_conversion) as run:
            output = self.convert()
            output.write_bytes(b"corrupt cached DXF")
            self.convert()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(ezdxf.readfile(output).modelspace()), 1)

    def test_changed_source_gets_distinct_cache(self):
        with patch.object(native, "_run_console", side_effect=self.fake_conversion) as run:
            first = self.convert()
            self.source.write_bytes(b"AC1032 changed fixture")
            second = self.convert()
        self.assertNotEqual(first, second)
        self.assertEqual(run.call_count, 2)

    def test_native_failure_preserves_source_and_diagnostic(self):
        before = self.source.read_bytes()
        def fail(executable, work, timeout):
            log = work / "native_console.log"
            log.write_bytes(b"error: license unavailable")
            return 7, log
        with patch.object(native, "_run_console", side_effect=fail):
            with self.assertRaisesRegex(native.NativeDwgError, "退出码 7"):
                self.convert()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(len(list(self.cache.glob("*.failure.log"))), 1)
        self.assertEqual(list(self.cache.glob("*.dxf")), [])
        self.assertEqual(list(self.cache.glob("native-*")), [])

    def test_malformed_converter_output_is_not_published(self):
        def malformed(executable, work, timeout):
            (work / "native_output.dxf").write_bytes(b"bad DXF" * 50)
            log = work / "native_console.log"
            log.write_bytes(b"completed but invalid")
            return 0, log
        with patch.object(native, "_run_console", side_effect=malformed):
            with self.assertRaises(native.NativeDwgError):
                self.convert()
        self.assertEqual(list(self.cache.glob("*.dxf")), [])

    def test_native_process_uses_private_copy_constant_script_and_isolation(self):
        work = self.folder / "process work"
        work.mkdir()
        process = MagicMock()
        process.wait.return_value = 0
        with patch.object(native.subprocess, "Popen", return_value=process) as popen:
            code, log = native._run_console(self.console, work, 25)
        args = popen.call_args.args[0]
        self.assertEqual(args[0], str(self.console))
        self.assertEqual(args[args.index("/i") + 1], str(work / "native_input.dwg"))
        self.assertIn("/readonly", args)
        self.assertIn("/isolate", args)
        self.assertEqual(popen.call_args.kwargs["shell"], False)
        self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        script = (work / "native_convert.scr").read_text(encoding="ascii")
        self.assertEqual(script, native.SCRIPT)
        self.assertNotIn(str(self.source), script)
        self.assertNotIn("SECURELOAD", script)
        self.assertNotIn("TRUSTEDPATHS", script)
        self.assertIn("_.QUIT\n_Yes\n", script)

    def test_timeout_kills_only_created_process(self):
        work = self.folder / "timeout"
        work.mkdir()
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("console", 2), 1]
        with patch.object(native.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(native.NativeDwgError, "超过 2 秒"):
                native._run_console(self.console, work, 2)
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list[-1].kwargs, {"timeout": 10})

    def test_wrong_input_and_missing_console_rejected(self):
        with self.assertRaises(native.NativeDwgError):
            native.convert_native_dwg(self.console, executable=self.console, cache_dir=self.cache)
        with self.assertRaises(native.NativeDwgError):
            native.convert_native_dwg(self.source, executable=self.folder / "missing.exe", cache_dir=self.cache)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                native.convert_native_dwg(self.source, executable=self.console, cache_dir=self.cache, timeout=timeout)

    def test_work_directory_accepts_different_path_spelling_for_same_parent(self):
        logical_root = self.folder / "logical"
        physical_root = self.folder / "physical"
        logical_root.mkdir()
        physical_root.mkdir()
        logical = logical_root / "native-abcdefgh"
        physical = physical_root / logical.name
        physical.mkdir()
        resolve, samefile = Path.resolve, Path.samefile

        def redirected_resolve(path, *args, **kwargs):
            return physical if path == logical else resolve(path, *args, **kwargs)

        def redirected_samefile(path, other):
            if path == physical_root and Path(other) == logical_root:
                return True
            return samefile(path, other)

        with patch.object(Path, "resolve", redirected_resolve), patch.object(Path, "samefile", redirected_samefile):
            self.assertEqual(native._checked_work_directory(logical, logical_root), physical)
        self.assertFalse(physical.is_relative_to(logical_root))

    def test_work_directory_rejects_outside_nested_and_renamed_targets(self):
        self.cache.mkdir()
        outside = self.folder / "native-outside"
        nested = self.cache / "nested" / "native-child"
        outside.mkdir()
        nested.mkdir(parents=True)
        for path in (outside, nested, self.cache):
            with self.subTest(path=path), self.assertRaises(native.NativeDwgError):
                native._checked_work_directory(path, self.cache)
        logical = self.cache / "native-original"
        physical = self.cache / "native-renamed"
        physical.mkdir()
        resolve = Path.resolve
        with patch.object(Path, "resolve", lambda p, *a, **kw: physical if p == logical else resolve(p, *a, **kw)):
            with self.assertRaises(native.NativeDwgError):
                native._checked_work_directory(logical, self.cache)

    def test_work_directory_identity_failure_is_explicit(self):
        self.cache.mkdir()
        child = self.cache / "native-private"
        child.mkdir()
        with patch.object(Path, "samefile", side_effect=PermissionError("unavailable")):
            with self.assertRaisesRegex(native.NativeDwgError, "身份无法确认"):
                native._checked_work_directory(child, self.cache)


if __name__ == "__main__":
    unittest.main()
