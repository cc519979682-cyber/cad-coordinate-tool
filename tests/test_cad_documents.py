"""Read-only document identity and COM lifetime regression checks."""
import json
import ntpath
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coordtool.cad_documents import find_open_document, list_open_drawings
from coordtool.picker import PickerError


class ComFailure(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__(hresult, "native read failure")


def drawing(hwnd, name, path=None, units=6):
    return SimpleNamespace(HWND=hwnd, Name=name,
                           Path=ntpath.dirname(path) if path else "",
                           FullName=path or name,
                           GetVariable=Mock(return_value=units))


def application(documents, active=None, hwnd=100):
    collection = Mock()
    collection.Count = len(documents)
    collection.Item.side_effect = lambda index: documents[index]
    app = Mock()
    app.HWND = hwnd
    app.Documents = collection
    app.ActiveDocument = active if active is not None else (documents[0] if documents else None)
    return app


def target(doc, app_hwnd=100):
    return {"app_hwnd": app_hwnd, "doc_hwnd": doc.HWND, "name": doc.Name,
            "path": doc.FullName if doc.Path else None, "units": 6, "active": False}


class CadDocumentsTests(unittest.TestCase):
    def list_using(self, app=None, failure=None):
        pc = Mock()
        client = Mock()
        client.GetActiveObject.return_value = app
        client.GetActiveObject.side_effect = failure
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            result = list_open_drawings()
        pc.CoInitialize.assert_called_once_with()
        pc.CoUninitialize.assert_called_once_with()
        client.GetActiveObject.assert_called_once_with("AutoCAD.Application")
        client.DispatchEx.assert_not_called()
        return result

    def test_no_running_cad_returns_empty_without_launch(self):
        self.assertEqual(self.list_using(failure=ComFailure(0x800401E3)), [])

    def test_non_missing_com_error_is_not_hidden_and_apartment_is_released(self):
        failure = ComFailure(0x80070005)
        pc, client = Mock(), Mock()
        client.GetActiveObject.side_effect = failure
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaises(ComFailure) as caught:
                list_open_drawings()
        self.assertIs(caught.exception, failure)
        pc.CoUninitialize.assert_called_once_with()
        client.DispatchEx.assert_not_called()

    def test_current_saved_document_first_unsaved_path_none_and_plain_data(self):
        first = drawing(201, "Drawing1.dwg", units=0)
        second = drawing(202, "道路.dwg", r"C:\工程\道路.dwg", 4)
        app = application([first, second], active=second)
        result = self.list_using(app)
        self.assertEqual(result, [dict(target(second), units=4, active=True), dict(target(first), units=0)])
        self.assertEqual(result[1]["units"], 0)
        self.assertIsNone(result[1]["path"])
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)
        self.assertTrue(all(type(value) in (int, str, bool, type(None))
                            for row in result for value in row.values()))
        app.Documents.Open.assert_not_called()
        app.Quit.assert_not_called()
        first.GetVariable.assert_called_once_with("INSUNITS")
        second.GetVariable.assert_called_once_with("INSUNITS")

    def test_unsaved_document_does_not_read_fullname(self):
        class Unsaved:
            HWND = 200
            Name = "Drawing1.dwg"
            Path = ""
            GetVariable = Mock(return_value=6)

            @property
            def FullName(self):
                raise AssertionError("Unsaved document has no saved path")
        doc = Unsaved()
        self.assertEqual(self.list_using(application([doc]))[0]["path"], None)

    def test_empty_running_cad_does_not_access_active_document(self):
        app = application([])
        del app.ActiveDocument
        self.assertEqual(self.list_using(app), [])
        app.Documents.Open.assert_not_called()

    def test_shared_mdi_handle_uses_name_and_path_for_active_and_target(self):
        first = drawing(100, "道路.dwg", r"C:\工程甲\道路.dwg")
        second = drawing(100, "道路.dwg", r"C:\工程乙\道路.dwg", 5)
        app = application([first, second], active=second)
        result = self.list_using(app)
        self.assertEqual([row["active"] for row in result], [True, False])
        self.assertEqual(result[0]["path"], second.FullName)
        self.assertIs(find_open_document(app, result[0]), second)
        self.assertIs(find_open_document(app, result[1]), first)
        app.Documents.Open.assert_not_called()

    def test_wrong_application_and_closed_document_never_fall_back_to_name_or_path(self):
        old = drawing(200, "道路.dwg", r"C:\工程\道路.dwg")
        other = drawing(201, old.Name, old.FullName)
        app = application([other])
        for selected in (target(old), target(other, app_hwnd=999)):
            with self.subTest(selected=selected), self.assertRaises(PickerError):
                find_open_document(app, selected)
        app.Documents.Open.assert_not_called()
        app.Activate.assert_not_called()

    def test_reused_handle_or_save_as_changes_require_refresh(self):
        old = drawing(200, "道路.dwg", r"C:\工程\道路.dwg")
        for changed in (drawing(200, "别的图.dwg", r"C:\工程\别的图.dwg"),
                        drawing(200, old.Name, r"D:\另存\道路.dwg"),
                        drawing(200, old.Name)):
            with self.subTest(changed=changed), self.assertRaisesRegex(PickerError, "刷新"):
                find_open_document(application([changed]), target(old))

    def test_ambiguous_handle_name_path_is_rejected(self):
        first = drawing(200, "道路.dwg", r"C:\工程\道路.dwg")
        second = drawing(200, first.Name, first.FullName)
        app = application([first, second])
        with self.assertRaisesRegex(PickerError, "唯一"):
            find_open_document(app, target(first))
        with self.assertRaisesRegex(PickerError, "唯一"):
            self.list_using(app)

    def test_windows_name_and_path_case_or_slashes_still_identify_same_document(self):
        doc = drawing(200, "ROAD.DWG", r"C:\Project\ROAD.DWG")
        selected = dict(target(doc), name="road.dwg", path="c:/project/road.dwg")
        self.assertIs(find_open_document(application([doc]), selected), doc)

    @unittest.skipUnless(os.name == "nt", "Windows short-path aliases require Win32")
    def test_existing_short_and_long_paths_identify_same_open_document(self):
        import ctypes
        from ctypes import wintypes

        get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        get_short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_short_path.restype = wintypes.DWORD
        with tempfile.TemporaryDirectory(prefix="coord-document-long-fixture-") as directory:
            source = Path(directory).resolve() / "road.dwg"
            source.write_bytes(b"synthetic document identity fixture")
            required = get_short_path(str(source), None, 0)
            if not required:
                self.skipTest("The temporary volume does not expose short-path aliases")
            buffer = ctypes.create_unicode_buffer(required)
            length = get_short_path(str(source), buffer, len(buffer))
            self.assertGreater(length, 0)
            self.assertLess(length, len(buffer))
            alias = buffer.value
            if ntpath.normcase(alias) == ntpath.normcase(str(source)):
                self.skipTest("8.3 aliases are disabled for the temporary volume")
            self.assertTrue(Path(alias).samefile(source))
            for actual, requested in ((alias, str(source)), (str(source), alias)):
                with self.subTest(actual=actual, requested=requested):
                    doc = drawing(200, source.name, actual)
                    app = application([doc])
                    selected = dict(target(doc), path=requested)
                    self.assertIs(find_open_document(app, selected), doc)
                    app.Documents.Open.assert_not_called()

    def test_transient_busy_read_retries_without_a_cad_mutation(self):
        doc = drawing(200, "道路.dwg", r"C:\工程\道路.dwg")
        doc.GetVariable.side_effect = [ComFailure(0x80010001), 6]
        app = application([doc])
        with patch("coordtool.picker.time.sleep"):
            result = self.list_using(app)
        self.assertEqual(result[0]["units"], 6)
        self.assertEqual(doc.GetVariable.call_count, 2)
        app.Documents.Open.assert_not_called()

    def test_invalid_selection_or_disconnected_document_raises_picker_error(self):
        doc = drawing(200, "Drawing1.dwg")
        app = application([doc])
        for selected in (None, {}, dict(target(doc), doc_hwnd=True),
                         dict(target(doc), app_hwnd=0), dict(target(doc), name=None)):
            with self.subTest(selected=selected), self.assertRaises(PickerError):
                find_open_document(app, selected)
        app.Documents.Item.side_effect = ComFailure(0x800401FD)
        with self.assertRaisesRegex(PickerError, "可能已关闭"):
            find_open_document(app, target(doc))


if __name__ == "__main__":
    unittest.main()
