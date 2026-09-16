"""Export dependencies and opt-in native LSP execution in an isolated Core."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ezdxf

from coordtool import cad, native_dwg
from coordtool.core import Point, Placement, Settings
from tests.native_support import find_core_console


class ExternalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coord-export-ref-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source_folder = self.folder / "原图"
        self.source_folder.mkdir()
        self.output = self.folder / "另外目录" / "copy.dxf"
        self.output.parent.mkdir()
        self.points = [Point("点1", 10, 20)]

    def fixture(self):
        doc = ezdxf.new("R2018", units=6)
        references = []
        for name in ("图片.png", "外参.dwg", "底图.pdf", "底图.dwf", "底图.dgn"):
            (self.source_folder / name).write_bytes(b"file dependency fixture")
        image = doc.add_image_def("图片.png", (10, 10))
        block = doc.blocks.new("NestedImage")
        block.add_image(image, (0, 0), (10, 10))
        doc.modelspace().add_blockref("NestedImage", (0, 0))
        references.append((image.dxf.handle, "filename", "图片.png"))
        doc.add_xref_def("外参.dwg", "XREF_A")
        doc.modelspace().add_blockref("XREF_A", (1, 2))
        references.append((doc.blocks["XREF_A"].block.dxf.handle, "xref_path", "外参.dwg"))
        for kind in ("pdf", "dwf", "dgn"):
            underlay = doc.add_underlay_def("底图." + kind, fmt=kind)
            doc.layouts.get("Layout1").add_underlay(underlay)
            references.append((underlay.dxf.handle, "filename", "底图." + kind))
        source = self.source_folder / "base.dxf"
        doc.saveas(source)
        return doc, source, references

    def test_relocates_image_xref_and_all_underlays_without_changing_source(self):
        _, source, references = self.fixture()
        before = source.read_bytes()
        base, _ = cad.load_drawing(source)
        cad.export_dxf(self.output, self.points, Settings(), base)
        output = ezdxf.readfile(self.output)
        self.assertFalse(output.audit().has_errors)
        for handle, attribute, relative in references:
            self.assertEqual(base.entitydb[handle].dxf.get(attribute), relative)
            actual = output.entitydb[handle].dxf.get(attribute)
            self.assertEqual(Path(actual), (self.source_folder / relative).resolve())
            self.assertTrue(Path(actual).is_file())
        self.assertEqual(source.read_bytes(), before)

    def test_native_converted_drawing_uses_original_folder_not_cache_folder(self):
        base, _, references = self.fixture()
        source = self.source_folder / "original.dwg"
        source.write_bytes(b"original DWG fixture")
        cache = self.folder / "cache"
        cache.mkdir()
        base.saveas(cache / "converted.dxf")
        base._coordtool_source = str(source)
        cad.export_dxf(self.output, self.points, Settings(), base)
        output = ezdxf.readfile(self.output)
        for handle, attribute, relative in references:
            self.assertEqual(Path(output.entitydb[handle].dxf.get(attribute)),
                             (self.source_folder / relative).resolve())

    def test_missing_dependency_preserves_existing_output_for_each_kind(self):
        base, _, references = self.fixture()
        self.output.write_bytes(b"previous output")
        for _, _, relative in references:
            missing = self.source_folder / relative
            missing.unlink()
            try:
                with self.subTest(file=relative), self.assertRaisesRegex(cad.DrawingError, "外部参照文件无法读取"):
                    cad.export_dxf(self.output, self.points, Settings(), base)
                self.assertEqual(self.output.read_bytes(), b"previous output")
                self.assertEqual(list(self.output.parent.glob(".coord-*")), [])
            finally:
                missing.write_bytes(b"file dependency fixture")

    def test_relative_reference_without_source_is_rejected(self):
        base, _, _ = self.fixture()
        base.filename = None
        with self.assertRaisesRegex(cad.DrawingError, "底图来源不明"):
            cad.export_dxf(self.output, self.points, Settings(), base)
        self.assertFalse(self.output.exists())

    def test_absolute_dependency_does_not_need_source_filename(self):
        doc = ezdxf.new("R2018", units=6)
        external = self.source_folder / "绝对路径.png"
        external.write_bytes(b"dependency fixture")
        definition = doc.add_image_def(str(external), (1, 1))
        doc.modelspace().add_image(definition, (0, 0), (1, 1))
        cad.export_dxf(self.output, self.points, Settings(), doc)
        self.assertEqual(ezdxf.readfile(self.output).objects.query("IMAGEDEF")[0].dxf.filename,
                         str(external.resolve()))

    def test_dependency_disappearing_during_write_does_not_replace_output(self):
        base, _, _ = self.fixture()
        self.output.write_bytes(b"previous output")
        saveas = ezdxf.document.Drawing.saveas

        def interrupted(doc, filename, *args, **kwargs):
            saveas(doc, filename, *args, **kwargs)
            (self.source_folder / "图片.png").unlink()

        with patch.object(ezdxf.document.Drawing, "saveas", interrupted):
            with self.assertRaisesRegex(cad.DrawingError, "外部参照校验失败"):
                cad.export_dxf(self.output, self.points, Settings(), base)
        self.assertEqual(self.output.read_bytes(), b"previous output")
        self.assertEqual(list(self.output.parent.glob(".coord-*")), [])

    def test_changed_serialized_reference_does_not_replace_output(self):
        base, _, references = self.fixture()
        self.output.write_bytes(b"previous output")
        saveas = ezdxf.document.Drawing.saveas

        def changed(doc, filename, *args, **kwargs):
            handle, attribute, relative = references[0]
            setattr(doc.entitydb[handle].dxf, attribute, relative)
            saveas(doc, filename, *args, **kwargs)

        with patch.object(ezdxf.document.Drawing, "saveas", changed):
            with self.assertRaisesRegex(cad.DrawingError, "外部参照校验失败"):
                cad.export_dxf(self.output, self.points, Settings(), base)
        self.assertEqual(self.output.read_bytes(), b"previous output")


@unittest.skipUnless(os.environ.get("COORDTOOL_NATIVE_TESTS") == "1", "opt-in isolated AutoCAD Core")
class NativeExportTests(unittest.TestCase):
    def test_lsp_model_guard_style_failure_geometry_and_native_dwg_conversion(self):
        console = find_core_console()
        if console is None:
            self.skipTest("AutoCAD Core Console unavailable")
        with tempfile.TemporaryDirectory(prefix="coord-native-export-test-") as folder:
            work = Path(folder)
            doc = ezdxf.new("R2018", units=6)
            doc.modelspace().add_line((1, 2, -1), (3, 4, -1))
            doc.saveas(work / "input.dxf")
            points = [Point('测点A"\\,', 10, 20), Point("道路-01", 30, 40, 3,
                      Placement(32, 42, 40, 1, .5), "capture:1", "road")]
            production = cad.generate_lsp(points, Settings())
            # Core has no desktop ActiveX application. Only document/Undo/Regen
            # calls are adapted; real STYLE/BLOCK/geometry and space guard run.
            adapted = production
            for old, new in (("(setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))", "(setq doc nil)"),
                             ("(vla-StartUndoMark doc)", "(princ)"),
                             ("(vla-EndUndoMark doc)", "(princ)"), ("(vla-Regen doc 1)", "(princ)")):
                adapted = adapted.replace(old, new)
            (work / "production.lsp").write_text(production, encoding="utf-8-sig")
            (work / "model.lsp").write_text(adapted, encoding="utf-8-sig")
            failed = adapted.replace("(entmake (list '(0 . \"STYLE\")", "(qa-entmake (list '(0 . \"STYLE\")")
            (work / "failed_style.lsp").write_text("(defun qa-entmake (data) nil)\n" + failed,
                                                  encoding="utf-8-sig")
            commands = ["FILEDIA", "0", f'(load "{(work / "production.lsp").as_posix()}")']

            def dxf(name):
                commands.extend(["_.DXFOUT", name + ".dxf", "_Version", "2018", "16"])

            dxf("loaded")
            commands.extend(['(setvar "TILEMODE" 0)', "(c:BINDLINE)"])
            dxf("paper")
            commands.extend([f'(load "{(work / "model.lsp").as_posix()}")',
                             '(setvar "TILEMODE" 1)', "(c:BINDLINE)"])
            dxf("model")
            commands.extend([f'(load "{(work / "failed_style.lsp").as_posix()}")', "(c:BINDLINE)"])
            dxf("failed")
            commands.extend(["_.SAVEAS", "2018", "source.dwg", "_.QUIT", "_Yes"])
            (work / "verify.scr").write_text("\n".join(commands) + "\n", encoding="ascii")
            (work / "profile").mkdir()
            with (work / "native.log").open("wb") as log:
                child = subprocess.Popen([str(console), "/i", str(work / "input.dxf"),
                    "/s", str(work / "verify.scr"), "/isolate", "CGP_EXPORT_" + work.name,
                    str(work / "profile")], cwd=work, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    code = child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
                    raise
            self.assertEqual(code, 0)
            for name in ("loaded", "paper"):
                check = ezdxf.readfile(work / (name + ".dxf"))
                self.assertEqual(sum(len(layout.query("INSERT")) for layout in check.layouts), 0)
                self.assertEqual(len(check.styles), 1)
                self.assertEqual(len(check.modelspace().query("LINE")), 1)
            check = ezdxf.readfile(work / "model.dxf")
            inserts = list(check.modelspace().query("INSERT"))
            self.assertEqual(len(inserts), 2)
            self.assertEqual(len(check.layouts.get("Layout1").query("INSERT")), 0)
            self.assertEqual(tuple(inserts[1].dxf.insert), (30, 40, 3))
            text = [e.dxf.text for insert in inserts for e in check.blocks[insert.dxf.name].query("TEXT")]
            self.assertEqual(text, [points[0].name, points[1].name, "E=30.000", "N=40.000"])
            self.assertFalse(check.audit().has_errors)
            self.assertEqual(len(ezdxf.readfile(work / "failed.dxf").modelspace().query("INSERT")), 2)
            log = (work / "native.log").read_bytes().decode("utf-16-le", errors="replace")
            self.assertIn("布局空间未修改", log)
            self.assertIn("无法创建标注文字样式", log)
            source = work / "source.dwg"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            # Convert only the generated DWG; keep cache data inside this test directory.
            converted = native_dwg.convert_native_dwg(source, timeout=45, executable=console,
                                                        cache_dir=work / "conversion-cache")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(len(ezdxf.readfile(converted).modelspace().query("INSERT")), 2)


if __name__ == "__main__":
    unittest.main()
