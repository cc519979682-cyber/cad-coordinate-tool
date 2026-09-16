"""All launch operations are mocked; these tests never open a CAD window."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from coordtool import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coord-launch-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.drawing = self.folder / "图纸 with spaces.dxf"
        self.drawing.write_text("drawing fixture", encoding="utf-8")
        self.exe = self.folder / "AutoCAD 2026" / "acad.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"fixture only; never execute")

    def test_extract_unquoted_executable_with_spaces_discards_switches(self):
        self.assertEqual(launcher._executable_from_command(str(self.exe) + " /Automation"), str(self.exe))

    def test_extract_quoted_executable_discards_all_tail_text(self):
        self.assertEqual(launcher._executable_from_command('"' + str(self.exe) + '" /Automation & anything'), str(self.exe))

    def test_reject_relative_nonexistent_malformed_and_newline(self):
        for command in ("acad.exe /Automation", str(self.folder / "missing.exe"),
                        '"' + str(self.exe), str(self.exe) + "\nanything",
                        str(self.exe) + "x /Automation", "", None):
            self.assertIsNone(launcher._executable_from_command(command))

    def test_registry_read_only_resolves_registered_executable(self):
        registry = MagicMock()
        registry.KEY_READ = 1
        registry.KEY_WOW64_64KEY = 2
        registry.KEY_WOW64_32KEY = 4
        registry.QueryValueEx.side_effect = [
            ("{12345678-1234-1234-1234-123456789abc}", 1),
            (str(self.exe) + " /Automation", 1),
        ]
        with patch.object(launcher, "winreg", registry):
            self.assertEqual(launcher._registered_autocad(), str(self.exe))
        paths = [call.args[1] for call in registry.OpenKey.call_args_list]
        self.assertEqual(paths, [r"AutoCAD.Application\CLSID", r"CLSID\{12345678-1234-1234-1234-123456789abc}\LocalServer32"])
        registry.SetValueEx.assert_not_called()
        registry.CreateKey.assert_not_called()

    def test_missing_registration_returns_none(self):
        registry = MagicMock()
        registry.KEY_READ = 1
        registry.KEY_WOW64_64KEY = 2
        registry.KEY_WOW64_32KEY = 4
        registry.OpenKey.side_effect = FileNotFoundError("not registered")
        with patch.object(launcher, "winreg", registry):
            self.assertIsNone(launcher._registered_autocad())

    def test_registered_launch_passes_two_arguments_without_shell(self):
        with patch.object(launcher, "_registered_autocad", return_value=str(self.exe)), \
             patch.object(launcher.subprocess, "Popen") as spawn, \
             patch.object(launcher.os, "startfile") as association:
            self.assertEqual(launcher.open_in_cad(self.drawing), "AutoCAD")
        spawn.assert_called_once_with([str(self.exe), str(self.drawing.resolve())], shell=False)
        association.assert_not_called()

    def test_no_registered_application_uses_file_association(self):
        with patch.object(launcher, "_registered_autocad", return_value=None), \
             patch.object(launcher.subprocess, "Popen") as spawn, \
             patch.object(launcher.os, "startfile") as association:
            self.assertEqual(launcher.open_in_cad(self.drawing), "系统默认应用")
        spawn.assert_not_called()
        association.assert_called_once_with(str(self.drawing.resolve()))

    def test_launch_error_explains_manual_open_and_preserves_file(self):
        before = self.drawing.read_bytes()
        with patch.object(launcher, "_registered_autocad", return_value=str(self.exe)), \
             patch.object(launcher.subprocess, "Popen", side_effect=PermissionError("blocked")):
            with self.assertRaisesRegex(launcher.CadLaunchError, "手动打开"):
                launcher.open_in_cad(self.drawing)
        self.assertEqual(self.drawing.read_bytes(), before)

    def test_missing_or_wrong_extension_never_attempts_launch(self):
        with patch.object(launcher, "_registered_autocad") as lookup:
            for path in (self.folder / "missing.dxf", self.exe):
                with self.assertRaises(launcher.CadLaunchError):
                    launcher.open_in_cad(path)
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
