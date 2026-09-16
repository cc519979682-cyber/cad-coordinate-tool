from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ezdxf

from coordtool.core import Placement, Point, Settings
from coordtool.picker import PickerError, PickerSession, _read_com, _sha256, _copy_primary_objects
from coordtool.picker_lisp import picker_commands
from coordtool.project import load_project


def add(serial=1, index=1, **changes):
    event = dict(type="ADD", serial=serial, index=index, x=101000., y=-22000.,
                 z=-3250., lx=106000., ly=-18000., hx=116000.,
                 height=2000., arrow=1000.)
    return {**event, **changes}


class ComFailure(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__(hresult, "mock COM failure")


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "道路原图.dwg"
        self.source.write_bytes(b"AC1032 private-copy-test\x00")
        self.settings = Settings(scale=1000., offset_e=1., offset_n=-2.)

    def create(self, **kwargs):
        values = dict(drawing_path=self.source, road="田间道路", start=1,
                      settings=self.settings, root=self.root / "sessions")
        values.update(kwargs)
        return PickerSession.create(**values)

    def test_create_private_copy_initial_recovery_and_unique_directory(self):
        before = _sha256(self.source)
        first, second = self.create(), self.create()
        self.assertNotEqual(first.directory, second.directory)
        self.assertNotEqual(first.drawing_path, self.source)
        self.assertEqual(_sha256(first.drawing_path), before)
        self.assertEqual(_sha256(self.source), before)
        self.assertTrue(first.directory.parent.samefile(self.root / "sessions"))
        self.assertTrue(first.lsp_path.read_text(encoding="utf-8"))
        points, settings, drawing = load_project(first.recovery_path)
        self.assertEqual(points, [])
        self.assertEqual(settings, self.settings)
        self.assertTrue(Path(drawing).samefile(self.source))
        metadata = json.loads((first.directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["original_sha256"], before)

    def test_no_drawing_creates_owned_empty_dxf_in_selected_units(self):
        for scale, units in ((1., 6), (100., 5), (1000., 4)):
            session = self.create(drawing_path=None, settings=replace(self.settings, scale=scale))
            drawing = ezdxf.readfile(session.drawing_path)
            self.assertEqual(drawing.units, units)
            self.assertEqual(len(drawing.modelspace()), 0)
            self.assertIsNone(load_project(session.recovery_path)[2])

    def test_initial_points_seed_preserves_raw_background_and_base_object(self):
        base = ezdxf.new("R2018")
        base.units = 4  # millimetres, matching this test's scale=1000
        base.modelspace().add_line((0, 0), (2, 2))
        initial = [Point("已有-01", 1, 2)]
        with patch("coordtool.cad.load_drawing") as loader:
            session = self.create(initial_points=initial, base_doc=base)
        loader.assert_not_called()
        self.assertEqual(len(base.modelspace()), 1)
        self.assertEqual(_sha256(session.drawing_path), _sha256(self.source))
        self.assertEqual(session.drawing_path.suffix, ".dwg")
        result = ezdxf.readfile(session.seed_path)
        self.assertEqual(len(result.modelspace()), 1)
        self.assertEqual(session.seed_entity_count, 1)
        self.assertEqual(load_project(session.recovery_path)[0], initial)

    def test_picking_height_does_not_change_old_labels_or_recovery_settings(self):
        base = ezdxf.new("R2018")
        base.units = 4
        initial = [Point("已有-01", 1, 2)]
        picker_settings = replace(self.settings, text_height=3.)
        snapshot_settings = replace(self.settings, text_height=1.)
        session = self.create(settings=picker_settings, snapshot_settings=snapshot_settings,
                              initial_points=initial, base_doc=base)
        working = ezdxf.readfile(session.seed_path)
        text_heights = {text.dxf.height for block in working.blocks for text in block.query("TEXT")}
        self.assertEqual(text_heights, {1000.})
        points = session.apply(initial, [add(height=3000.)])
        self.assertEqual(points[-1].placement.height, 3.)
        restored, settings, _ = load_project(session.recovery_path)
        self.assertEqual(restored, points)
        self.assertEqual(settings.text_height, 1.)
        self.assertEqual(session.settings.text_height, 3.)
        metadata = json.loads((session.directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["settings"]["text_height"], 3.)
        self.assertEqual(metadata["snapshot_settings"]["text_height"], 1.)

    def test_seed_uses_unique_visible_layer_and_preserves_scaled_offset_coordinates(self):
        source_doc = ezdxf.new("R2018")
        source_doc.units = 4
        layer = source_doc.layers.new(self.settings.layer)
        layer.off()
        layer.freeze()
        layer.lock()
        source = self.root / "隐藏坐标层原图.dxf"
        source_doc.saveas(source)
        original_hash = _sha256(source)
        points = [Point("已有-01", 100., -20., -3.25)]
        first = self.create(drawing_path=source, initial_points=points, base_doc=source_doc)
        second = self.create(drawing_path=source, initial_points=points, base_doc=source_doc)
        self.assertNotEqual(first.seed_layer, self.settings.layer)
        self.assertNotEqual(first.seed_layer, second.seed_layer)
        seed = ezdxf.readfile(first.seed_path)
        seed_layer = seed.layers.get(first.seed_layer)
        self.assertFalse(seed_layer.is_off() or seed_layer.is_frozen() or seed_layer.is_locked())
        self.assertEqual(seed.units, 4)
        insert = list(seed.modelspace().query("INSERT"))[0]
        self.assertEqual(tuple(insert.dxf.insert)[:2], (101000., -22000.))
        self.assertEqual(insert.dxf.layer, first.seed_layer)
        self.assertEqual(insert.dxf.color, self.settings.color)
        self.assertEqual(insert.dxf.lineweight, self.settings.lineweight)
        self.assertEqual(first.snapshot_settings.layer, self.settings.layer)
        self.assertEqual(_sha256(first.drawing_path), original_hash)
        self.assertEqual(_sha256(source), original_hash)
        self.assertTrue(layer.is_off() and layer.is_frozen() and layer.is_locked())
        second_seed = ezdxf.readfile(second.seed_path)
        first_blocks = {e.dxf.name for e in seed.modelspace().query("INSERT")}
        second_blocks = {e.dxf.name for e in second_seed.modelspace().query("INSERT")}
        self.assertFalse(first_blocks & second_blocks)
        first_styles = {e.dxf.style for block in seed.blocks for e in block.query("TEXT")}
        second_styles = {e.dxf.style for block in second_seed.blocks for e in block.query("TEXT")}
        self.assertFalse(first_styles & second_styles)

    def test_invalid_configuration_rejected_before_session_creation(self):
        for changes in (dict(road=""), dict(road="A\nB"), dict(road=" A"),
                        dict(start=True), dict(start=0), dict(start=2**31),
                        dict(settings=replace(self.settings, scale=0)),
                        dict(snapshot_settings=replace(self.settings, offset_e=4)),
                        dict(snapshot_settings=replace(self.settings, text_height=0)),
                        dict(drawing_path=self.root / "missing.dwg"),
                        dict(initial_points=[Point("田间道路-01", 1, 2)]),
                        dict(initial_points=[Point("田间道路-1", 1, 2)])):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)
        self.assertFalse((self.root / "sessions").exists())

    def test_source_changes_during_copy_rejected(self):
        import shutil
        original_copy = shutil.copyfile
        def change_source(source, target):
            original_copy(source, target)
            Path(source).write_bytes(b"changed concurrently")
        with patch("coordtool.picker.shutil.copyfile", side_effect=change_source):
            with self.assertRaisesRegex(PickerError, "原图发生变化"):
                self.create()

    def test_apply_inverse_units_offsets_and_manual_geometry_roundtrip(self):
        session = self.create()
        initial = [Point("已有", 4, 5)]
        points = session.apply(initial, [{"type": "READY"}, add()])
        expected = Point("田间道路-01", 100., -20., -3.25,
                         Placement(105., -16., 115., 2., 1.),
                         f"{session.session_id}:1", session.session_id)
        self.assertEqual(points, initial + [expected])
        self.assertEqual(initial, [Point("已有", 4, 5)])
        self.assertTrue(session.ready)
        self.assertFalse(session.done)
        self.assertEqual(load_project(session.recovery_path)[0], points)

    def test_poll_handles_partial_records_and_consumes_complete_lines_once(self):
        session = self.create()
        self.assertEqual(session.poll(), [])
        session.event_path.write_bytes(b"READY\r\nADD\t1\t1\t101000\t-22000")
        self.assertEqual(session.poll(), [{"type": "READY"}])
        self.assertEqual(session.poll(), [])
        with session.event_path.open("ab") as stream:
            stream.write(b"\t-3250\t106000\t-18000\t116000\t2000\t1000\nDONE\n")
        self.assertEqual(session.poll(), [add(), {"type": "DONE"}])
        self.assertEqual(session.poll(), [])

    def test_poll_invalid_line_requests_stop_without_silent_data_loss(self):
        session = self.create()
        session.event_path.write_bytes(b"ADD\tnot-an-event\n")
        with self.assertRaises(PickerError):
            session.poll()
        self.assertTrue(session.stop_path.exists())
        self.assertIn("恢复文件", session.last_error)
        self.assertEqual(load_project(session.recovery_path)[0], [])

    def test_poll_detects_truncated_journal(self):
        session = self.create()
        session.event_path.write_bytes(b"READY\n")
        session.poll()
        session.event_path.write_bytes(b"")
        with self.assertRaisesRegex(PickerError, "截短"):
            session.poll()

    def test_replay_add_and_undo_are_idempotent_and_keep_other_roads(self):
        session = self.create(start=7)
        initial = [Point("其他道路-01", 4, 5, capture_id="other:1", group_id="other")]
        batch = [add(index=7), add(2, 8), {"type": "UNDO", "serial": 2}, add(3, 8, x=102000.)]
        points = session.apply(initial, batch)
        self.assertEqual([p.name for p in points], ["其他道路-01", "田间道路-07", "田间道路-08"])
        self.assertEqual(points[-1].capture_id, f"{session.session_id}:3")
        self.assertEqual(session.apply(points, batch), points)
        final = session.apply(points, [{"type": "UNDO", "serial": 3}, {"type": "DONE"}])
        self.assertEqual(final, points[:-1])
        self.assertTrue(session.done)

    def test_conflicting_event_batch_is_atomic(self):
        variants = [add(3, 2), add(2, 4), add(2, 2, height=0), add(2, 2, x=float("nan")),
                    add(1, 1, x=8), {"type": "UNDO", "serial": 3}, {"type": "UNKNOWN"}]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                session = self.create()
                snapshot = session.recovery_path.read_bytes()
                initial = [Point("已有", 5, 6)]
                with self.assertRaises(PickerError):
                    session.apply(initial, [add(), invalid])
                self.assertEqual(session.recovery_path.read_bytes(), snapshot)
                self.assertEqual(session._last_serial, 0)
                self.assertEqual(initial, [Point("已有", 5, 6)])

    def test_duplicate_name_or_id_is_rejected(self):
        for initial in ([Point("田间道路-01", 9, 9)],
                        [Point("A", 1, 2, capture_id="same"), Point("B", 1, 2, capture_id="same")]):
            session = self.create()
            with self.assertRaises(PickerError):
                session.apply(initial, [add()])

    def test_modified_active_capture_is_rejected(self):
        session = self.create()
        points = session.apply([], [add()])
        with self.assertRaises(PickerError):
            session.apply([replace(points[0], e=999)], [add(2, 2)])
        self.assertEqual(load_project(session.recovery_path)[0], points)

    def test_snapshot_write_failure_does_not_commit_batch(self):
        session = self.create()
        before = session.recovery_path.read_bytes()
        with patch("coordtool.picker.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(PickerError):
                session.apply([], [add()])
        self.assertEqual(session._last_serial, 0)
        self.assertEqual(session.recovery_path.read_bytes(), before)
        self.assertEqual(len(session.apply([], [add()])), 1)

    def test_error_and_stop_are_persistent_without_deleting_work(self):
        session = self.create()
        session.request_stop()
        session.request_stop()
        points = session.apply([], [{"type": "ERROR", "message": "failed"}])
        self.assertTrue(session.done)
        self.assertEqual(session.last_error, "failed")
        self.assertEqual(points, [])
        self.assertTrue(session.drawing_path.exists())
        self.assertTrue(session.stop_path.exists())

    def fake_com(self, session, *, busy=False, active=0, wrong_path=False):
        document = Mock()
        document.FullName = str(self.source if wrong_path else session.drawing_path)
        document.GetVariable.return_value = active
        def acknowledge(command):
            if "(c:CGPICK)" in command:
                session.event_path.write_bytes(b"READY\n")
        document.SendCommand.side_effect = acknowledge
        documents = Mock()
        documents.Count = 1
        documents.Open.return_value = document
        documents.Item.return_value = document
        app = Mock()
        app.GetAcadState.return_value = SimpleNamespace(IsQuiescent=not busy)
        app.Documents = documents
        app.ActiveDocument = document
        client = Mock()
        client.GetActiveObject.return_value = app
        client.DispatchEx.return_value = app
        pythoncom = Mock()
        return pythoncom, client, app, document

    def test_launch_owns_document_and_keeps_com_in_one_scope(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            session.launch()
        app.Documents.Open.assert_called_once_with(str(session.drawing_path), False)
        expected = picker_commands(session.lsp_path.read_text(encoding="utf-8"))
        self.assertEqual([call.args[0] for call in doc.SendCommand.call_args_list], expected)
        self.assertIn("(c:CGPICK)", expected[-1])
        self.assertTrue(all(command.endswith("\n") for command in expected))
        self.assertNotIn("SECURELOAD", "".join(expected).upper())
        self.assertEqual(session.poll(), [{"type": "READY"}])
        pc.CoInitialize.assert_called_once()
        pc.CoUninitialize.assert_called_once()
        self.assertFalse(any(value is app or value is doc for value in session.__dict__.values()))

    def test_launch_dispatches_new_instance_only_when_no_active_object(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        client.GetActiveObject.side_effect = ComFailure(0x800401E3)
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            session.launch()
        client.DispatchEx.assert_called_once_with("AutoCAD.Application")
        self.assertEqual(doc.SendCommand.call_count, len(picker_commands(session.lsp_path.read_text(encoding="utf-8"))))

    def test_spawned_application_waits_for_proxy_and_idle_startup(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        client.GetActiveObject.side_effect = ComFailure(0x800401E3)
        app.GetAcadState.side_effect = [AttributeError("GetAcadState not ready"),
                                       SimpleNamespace(IsQuiescent=False),
                                       SimpleNamespace(IsQuiescent=True)]
        with patch("coordtool.picker._com_modules", return_value=(pc, client)), patch("coordtool.picker.time.sleep"):
            session.launch()
        client.DispatchEx.assert_called_once()
        self.assertEqual(app.GetAcadState.call_count, 3)
        app.Documents.Open.assert_called_once()
        self.assertEqual(session.poll(), [{"type": "READY"}])
        pc.CoUninitialize.assert_called_once()

    def test_spawned_application_wait_can_be_stopped_without_opening(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        client.GetActiveObject.side_effect = ComFailure(0x800401E3)
        def warming_up():
            session.request_stop()
            return SimpleNamespace(IsQuiescent=False)
        app.GetAcadState.side_effect = warming_up
        with patch("coordtool.picker._com_modules", return_value=(pc, client)), patch("coordtool.picker.time.sleep"):
            with self.assertRaisesRegex(PickerError, "取消"):
                session.launch()
        client.DispatchEx.assert_called_once()
        app.Documents.Open.assert_not_called()
        doc.SendCommand.assert_not_called()
        pc.CoUninitialize.assert_called_once()

    def test_spawned_application_waits_for_initial_document_proxy(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        client.GetActiveObject.side_effect = ComFailure(0x800401E3)
        doc.GetVariable.side_effect = [AttributeError("document initializing"), 0, 0]
        with patch("coordtool.picker._com_modules", return_value=(pc, client)), patch("coordtool.picker.time.sleep"):
            session.launch()
        client.DispatchEx.assert_called_once()
        app.Documents.Open.assert_called_once()

    def test_spawned_application_warmup_timeout_preserves_cad(self):
        session = self.create()
        session.APP_READY_TIMEOUT = .01
        pc, client, app, doc = self.fake_com(session, busy=True)
        client.GetActiveObject.side_effect = ComFailure(0x800401E3)
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, "启动的 AutoCAD 就绪超过"):
                session.launch()
        client.DispatchEx.assert_called_once()
        app.Documents.Open.assert_not_called()
        app.Quit.assert_not_called()
        pc.CoUninitialize.assert_called_once()

    def test_launch_uses_fresh_document_when_open_proxy_is_not_ready(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        app.Documents.Open.return_value = SimpleNamespace()
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            session.launch()
        app.Documents.Open.assert_called_once()
        self.assertEqual(doc.SendCommand.call_count, len(picker_commands(session.lsp_path.read_text(encoding="utf-8"))))

    def test_launch_requires_ready_acknowledgement(self):
        session = self.create()
        session.READY_TIMEOUT = .01
        pc, client, app, doc = self.fake_com(session)
        doc.SendCommand.side_effect = None
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, "确认取点就绪"):
                session.launch()
        self.assertFalse(session.ready)

    def test_stop_between_chunks_does_not_send_remaining_program(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        doc.SendCommand.side_effect = lambda command: session.request_stop()
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaises(PickerError):
                session.launch()
        doc.SendCommand.assert_called_once()

    def test_active_document_change_between_chunks_stops_sending(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        doc.SendCommand.side_effect = lambda command: setattr(app, "ActiveDocument", SimpleNamespace(FullName=str(self.source)))
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, "活动图纸发生变化"):
                session.launch()
        doc.SendCommand.assert_called_once()

    def test_launch_busy_cad_is_not_interrupted(self):
        for changes in (dict(busy=True), dict(active=1)):
            session = self.create()
            pc, client, app, doc = self.fake_com(session, **changes)
            with patch("coordtool.picker._com_modules", return_value=(pc, client)):
                with self.assertRaises(PickerError):
                    session.launch()
            app.Documents.Open.assert_not_called()
            doc.SendCommand.assert_not_called()
            client.DispatchEx.assert_not_called()
            pc.CoUninitialize.assert_called_once()

    def test_launch_rejects_wrong_document_and_prior_stop(self):
        for stopped in (False, True):
            session = self.create()
            session.OPEN_TIMEOUT = .01
            pc, client, app, doc = self.fake_com(session, wrong_path=True)
            if stopped:
                session.request_stop()
            with patch("coordtool.picker._com_modules", return_value=(pc, client)):
                with self.assertRaises(PickerError):
                    session.launch()
            doc.SendCommand.assert_not_called()

    def test_uncertain_com_mutations_never_retried(self):
        for method in ("open", "send"):
            session = self.create()
            pc, client, app, doc = self.fake_com(session)
            target = app.Documents.Open if method == "open" else doc.SendCommand
            target.side_effect = ComFailure(0x80010001)
            with patch("coordtool.picker._com_modules", return_value=(pc, client)):
                with self.assertRaisesRegex(PickerError, "不会自动重复"):
                    session.launch()
                with self.assertRaises(PickerError):
                    session.launch()
            self.assertEqual(target.call_count, 1)
            pc.CoUninitialize.assert_called_once()

    def test_only_rejected_read_calls_are_retried(self):
        call = Mock(side_effect=[ComFailure(0x80010001), ComFailure(0x8001010A), 12])
        with patch("coordtool.picker.time.sleep"):
            self.assertEqual(_read_com(call), 12)
        self.assertEqual(call.call_count, 3)
        call = Mock(side_effect=ComFailure(0x80004005))
        with self.assertRaises(ComFailure):
            _read_com(call)
        self.assertEqual(call.call_count, 1)

    def test_transient_dynamic_attribute_reads_retry_and_permanent_failure_is_bounded(self):
        call = Mock(side_effect=[AttributeError("<unknown>.FullName"),
                                AttributeError("<unknown>.FullName"), "owned.dwg"])
        with patch("coordtool.picker.time.sleep"):
            self.assertEqual(_read_com(call), "owned.dwg")
        self.assertEqual(call.call_count, 3)
        call = Mock(side_effect=AttributeError("permanently missing"))
        with patch("coordtool.picker.time.monotonic", side_effect=[0., .1, 5.1]), patch("coordtool.picker.time.sleep"):
            with self.assertRaisesRegex(AttributeError, "permanently missing"):
                _read_com(call)
        self.assertEqual(call.call_count, 2)

    def test_launch_retries_fullname_read_after_activation_without_reactivating(self):
        session = self.create()
        pc, client, app, doc = self.fake_com(session)
        transient = {"armed": False, "failures": 0}
        doc.Activate.side_effect = lambda: transient.update(armed=True)
        def fullname():
            if transient["armed"]:
                transient["armed"] = False
                transient["failures"] += 1
                raise AttributeError("<unknown>.FullName")
            return str(session.drawing_path)
        class DocumentProxy:
            FullName = property(lambda self: fullname())
        proxy = DocumentProxy()
        proxy.Activate, proxy.GetVariable, proxy.SendCommand = doc.Activate, doc.GetVariable, doc.SendCommand
        app.ActiveDocument = proxy
        app.Documents.Open.return_value = proxy
        app.Documents.Item.return_value = proxy
        with patch("coordtool.picker._com_modules", return_value=(pc, client)), patch("coordtool.picker.time.sleep"):
            session.launch()
        doc.Activate.assert_called_once()
        self.assertEqual(transient["failures"], 1)
        self.assertEqual(session.poll(), [{"type": "READY"}])

    def seeded_com(self):
        session = self.create(initial_points=[Point("已有-01", 1, 2), Point("已有-02", 3, 4)])
        pc, client, app, main = self.fake_com(session)
        pc.VT_ARRAY, pc.VT_DISPATCH = 0x2000, 9
        client.VARIANT.side_effect = lambda vt, value: SimpleNamespace(vt=vt, value=value)
        seed = Mock()
        seed.FullName = str(session.seed_path)
        seed.ModelSpace.Count = session.seed_entity_count
        entities = [Mock(), Mock()]
        seed.ModelSpace.Item.side_effect = lambda i: entities[i]
        main.ModelSpace.Count = 100
        def copy_objects(objects, owner):
            self.assertIs(owner, main.ModelSpace)
            self.assertEqual(objects.value, tuple(entities))
            self.assertEqual(objects.vt, 0x2009)
            main.ModelSpace.Count += len(entities)
            return tuple(Mock() for _ in entities)
        seed.CopyObjects.side_effect = copy_objects
        main.Activate.side_effect = lambda: setattr(app, "ActiveDocument", main)
        def open_document(path, readonly):
            chosen = seed if path == str(session.seed_path) else main
            app.ActiveDocument = chosen
            return chosen
        app.Documents.Open.side_effect = open_document
        app.Documents.Count = 2
        app.Documents.Item.side_effect = lambda i: (main, seed)[i]
        return session, pc, client, app, main, seed

    def test_seed_native_copy_only_between_owned_docs_and_closes_seed_after_verify(self):
        session, pc, client, app, main, seed = self.seeded_com()
        before = _sha256(self.source)
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            session.launch()
        self.assertEqual([call.args for call in app.Documents.Open.call_args_list],
                         [(str(session.drawing_path), False), (str(session.seed_path), True)])
        seed.CopyObjects.assert_called_once()
        self.assertEqual(main.ModelSpace.Count, 102)
        seed.Close.assert_called_once_with(False)
        main.Close.assert_not_called()
        main.CopyObjects.assert_not_called()
        self.assertGreater(main.SendCommand.call_count, 1)
        self.assertIs(app.ActiveDocument, main)
        self.assertEqual(_sha256(self.source), before)

    def test_seed_accepts_pywin32_retval_and_idpairs_result(self):
        session, pc, client, app, main, seed = self.seeded_com()
        original_copy = seed.CopyObjects.side_effect
        seed.CopyObjects.side_effect = lambda objects, owner: (
            original_copy(objects, owner), tuple(Mock() for _ in range(40)))
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            session.launch()
        self.assertEqual(main.ModelSpace.Count, 102)
        seed.Close.assert_called_once_with(False)
        self.assertEqual(session.poll(), [{"type": "READY"}])

    def test_copyobjects_normalizes_real_32_entity_result_and_direct_two_entities(self):
        entities = tuple(object() for _ in range(32))
        mappings = tuple(object() for _ in range(356))
        self.assertEqual(_copy_primary_objects((entities, mappings)), entities)
        self.assertEqual(_copy_primary_objects((entities, None)), entities)
        self.assertEqual(_copy_primary_objects(entities[:2]), entities[:2])
        with self.assertRaises(PickerError):
            _copy_primary_objects(None)
        with self.assertRaises(PickerError):
            _copy_primary_objects((entities, "invalid mappings"))

    def test_road_number_baselines_merge_case_and_keep_hyphenated_prefix(self):
        initial = [Point("RoadA-09", 1, 2), Point("roada-0012", 3, 4),
                   Point("一号-支路-03", 5, 6), Point("ordinary", 7, 8)]
        session = self.create(initial_points=initial, start=100)
        self.assertEqual(session.road_numbers, {"RoadA": 13, "一号-支路": 4, "田间道路": 100})
        metadata = json.loads((session.directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["road_numbers"], session.road_numbers)

    def test_road_switch_public_state_replay_and_global_capture_count(self):
        session = self.create(start=7)
        batch = [add(index=7, road_id=0),
                 {"type": "ROAD", "road_id": 1, "start": 1, "road": "第二道路"},
                 add(2, 1, road_id=1)]
        points = session.apply([], batch)
        self.assertEqual([p.name for p in points], ["田间道路-07", "第二道路-01"])
        self.assertEqual((session.road, session.current_road, session.current_segment), ("第二道路", "第二道路", 1))
        self.assertEqual(session.active_count, 2)
        self.assertEqual(session._active, [2])
        self.assertNotEqual(points[0].group_id, points[1].group_id)
        self.assertEqual(session.apply(points, batch), points)
        self.assertEqual(session.current_segment, 1)
        self.assertEqual(session.apply(points, [add(index=7)]), points)  # legacy ADD stays road 0
        metadata = json.loads(session.recovery_path.read_text(encoding="utf-8"))["picker"]
        self.assertEqual(metadata["current_road"], "第二道路")
        self.assertEqual(metadata["segments"], {"0": {"road": "田间道路", "start": 7},
                                                "1": {"road": "第二道路", "start": 1}})
        self.assertEqual(metadata["seen_segments"], {"1": 0, "2": 1})
        self.assertEqual(metadata["active_count"], 2)

    def test_unvisited_initial_road_keeps_manual_start_when_returning(self):
        session = self.create(start=100)
        points = session.apply([], [
            {"type": "ROAD", "road_id": 1, "start": 1, "road": "另一条"},
            {"type": "ROAD", "road_id": 2, "start": 100, "road": "田间道路"},
            add(1, 100, road_id=2)])
        self.assertEqual([p.name for p in points], ["田间道路-100"])
        self.assertEqual(session.active_count, 1)

    def test_exhausted_road_number_is_recorded_but_cannot_wrap_or_switch(self):
        initial = [Point("已耗尽-2147483647", 1, 2)]
        session = self.create(initial_points=initial)
        self.assertEqual(session.road_numbers["已耗尽"], 2147483648)
        with self.assertRaises(PickerError):
            session.apply(initial, [{"type": "ROAD", "road_id": 1, "start": 1, "road": "已耗尽"}])
        with self.assertRaises(PickerError):
            session.apply(initial, [{"type": "ROAD", "road_id": 1, "start": 2147483648, "road": "已耗尽"}])

    def test_road_switch_and_undo_failure_roll_back_the_entire_batch(self):
        session = self.create()
        points = session.apply([], [add()])
        before = session.recovery_path.read_bytes()
        with self.assertRaises(PickerError):
            session.apply(points, [{"type": "ROAD", "road_id": 1, "start": 1, "road": "新路"},
                                   {"type": "UNDO", "serial": 1}])
        self.assertEqual(session.road, "田间道路")
        self.assertEqual(session.current_segment, 0)
        self.assertEqual(session._segments, {0: {"road": "田间道路", "start": 1}})
        self.assertEqual(session._active, [1])
        self.assertEqual(session.recovery_path.read_bytes(), before)

    def test_road_snapshot_failure_does_not_commit_switch_metadata(self):
        session = self.create()
        before = session.recovery_path.read_bytes()
        event = {"type": "ROAD", "road_id": 1, "start": 1, "road": "新路"}
        with patch("coordtool.picker.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(PickerError):
                session.apply([], [event, add(1, 1, road_id=1)])
        self.assertEqual(session.current_segment, 0)
        self.assertEqual(session.active_count, 0)
        self.assertEqual(session._seen_segments, {})
        self.assertEqual(session.recovery_path.read_bytes(), before)
        self.assertEqual(len(session.apply([], [event, add(1, 1, road_id=1)])), 1)

    def test_previous_road_captures_cannot_be_edited_during_later_road(self):
        session = self.create()
        points = session.apply([], [add(), {"type": "ROAD", "road_id": 1, "start": 1, "road": "新路"}])
        with self.assertRaises(PickerError):
            session.apply([replace(points[0], e=999)], [add(2, 1, road_id=1)])

    def test_conflicting_unknown_and_invalid_road_records_are_rejected(self):
        variants = [dict(road_id=0), dict(road_id=2), dict(road_id=True), dict(start=2),
                    dict(start=False), dict(road=" "), dict(road=" A"), dict(road="A\x85B"),
                    dict(road="bad\ud800"), dict(road="x"*101)]
        for changes in variants:
            session = self.create()
            event = {"type": "ROAD", "road_id": 1, "start": 1, "road": "新路", **changes}
            with self.subTest(changes=changes), self.assertRaises(PickerError):
                session.apply([], [event])
        session = self.create()
        event = {"type": "ROAD", "road_id": 1, "start": 1, "road": "新路"}
        session.apply([], [event])
        with self.assertRaises(PickerError):
            session.apply([], [{**event, "road": "另一条"}])
        with self.assertRaises(PickerError):
            session.apply([], [add(road_id=0)])
        with self.assertRaises(PickerError):
            session.apply([], [add(road_id=3)])

    def test_seed_uncertain_copy_never_retried_or_closed(self):
        session, pc, client, app, main, seed = self.seeded_com()
        seed.CopyObjects.side_effect = ComFailure(0x80010001)
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaises(PickerError):
                session.launch()
        seed.CopyObjects.assert_called_once()
        seed.Close.assert_not_called()
        main.Close.assert_not_called()
        main.SendCommand.assert_not_called()
        self.assertTrue(session.stop_path.exists())
        diagnostic = (session.directory / "launch_error.log").read_text(encoding="utf-8")
        self.assertIn("复制旧坐标到主副本", diagnostic)
        self.assertIn("Traceback", diagnostic)
        self.assertIn("CopyObjects", diagnostic)

    def test_seed_copy_count_mismatch_leaves_both_owned_documents_open(self):
        session, pc, client, app, main, seed = self.seeded_com()
        seed.CopyObjects.side_effect = lambda objects, owner: (Mock(), Mock())
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, "复制数量校验失败"):
                session.launch()
        seed.CopyObjects.assert_called_once()
        seed.Close.assert_not_called()
        main.Close.assert_not_called()
        main.SendCommand.assert_not_called()

    def test_seed_native_read_count_mismatch_does_not_copy(self):
        session, pc, client, app, main, seed = self.seeded_com()
        seed.ModelSpace.Count = 1
        with patch("coordtool.picker._com_modules", return_value=(pc, client)):
            with self.assertRaisesRegex(PickerError, "标注数量不完整"):
                session.launch()
        seed.CopyObjects.assert_not_called()
        seed.Close.assert_not_called()
        main.SendCommand.assert_not_called()


if __name__ == "__main__":
    unittest.main()
