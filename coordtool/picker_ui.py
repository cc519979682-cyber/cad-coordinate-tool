"""Tk controls for an isolated, journalled AutoCAD picking session."""
from pathlib import Path
from time import monotonic
import tkinter as tk
from tkinter import messagebox, filedialog
import ttkbootstrap as ttk


class PickerUIMixin:
    def _init_picker(self):
        self.picker_session = None
        self.picker_starting = False
        self.picker_discovering = False
        self.picker_discovery_token = 0
        self.picker_dialog = None
        self.picker_before = []
        self.picker_settings = None
        self.picker_road = "道路1"
        self.picker_started_at = 0.0
        self.picker_polled_at = 0.0
        self.picker_stop_requested = False
        self.picker_previous = None
        self.picker_previous_points = []
        self.picker_previous_settings = None
        self.picker_previous_base = None
        self.picker_zb_blocked_session = None
        self.picker_resume_reason = "先完成一轮 CAD 取点，即可继续使用同一张 CAD 工作图。"
        self.picker_text = tk.StringVar(value="CAD 取点结束后：在 CAD 输入 ZB → 填写道路名 → 自动继续")
        self.picker_button_text = tk.StringVar(value="CAD 取点")
        self._restoring_picker_settings = False

    def _picker_active(self):
        return self.picker_starting or self.picker_session is not None

    def _picker_guard(self):
        if self._picker_active():
            self.status.set("正在 CAD 取点。请先在 CAD 输入 Q 结束全部取点，再修改坐标、设置或更换项目。")
            return True
        return False

    def _build_picker_bar(self, outer):
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, textvariable=self.picker_text, bootstyle="info", anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="恢复取点记录", command=self.recover_picker, bootstyle="light-outline", padding=(7, 3)).pack(side="right")
        self.picker_resume_button = ttk.Button(bar, text="继续 CAD 取点", command=self.continue_picker,
                                              bootstyle="success-outline", padding=(7, 3), state="disabled")
        self.picker_resume_button.pack(side="right", padx=(6, 8))

    def _invalidate_picker_resume(self, reason="坐标、设置或项目已改变，请使用 CAD 取点重新选择图纸。"):
        if self.picker_previous is not None:
            try:
                self.picker_previous.disable_zb(reason)
            except Exception as exc:
                self.status.set(f"CAD ZB 续接授权撤销未完成：{exc}；请勿继续取点。")
        self.picker_previous = None
        self.picker_previous_points = []
        self.picker_previous_settings = None
        self.picker_previous_base = None
        self.picker_zb_blocked_session = None
        self.picker_resume_reason = reason
        if hasattr(self, "picker_resume_button"):
            self.picker_resume_button.configure(state="disabled")

    def _picker_resume_available(self):
        if self.closed or self._picker_active() or self.loading_drawing or self.picker_previous is None:
            return False
        try:
            matches = (self.points == self.picker_previous_points
                       and self.settings() == self.picker_previous_settings
                       and self.base_path == self.picker_previous_base)
        except ValueError:
            matches = False
        if not matches:
            self._invalidate_picker_resume()
        return matches

    def _update_picker_resume(self):
        if hasattr(self, "picker_resume_button"):
            self.picker_resume_button.configure(state="normal" if self._picker_resume_available() else "disabled")

    def _poll_zb(self):
        if self.picker_discovering or self.picker_dialog is not None:
            return
        if not self._picker_resume_available():
            return
        previous = self.picker_previous
        if self.picker_zb_blocked_session is previous:
            return
        try:
            previous.publish_zb_ready()
            road = previous.take_zb_request()
            if road is None or not road.strip():
                return
            previous.disable_zb("已收到 CAD ZB 请求，正在续接。")
            start = previous.next_road_start(road, self.points)
            self._start_picker(road, start, previous.settings, resume_from=previous, from_cad=True)
        except Exception as exc:
            self.picker_zb_blocked_session = previous
            try:
                previous.disable_zb("CAD ZB 请求未完成。")
            except Exception:
                pass
            text = f"CAD ZB 续接未完成：{exc}；可使用“继续 CAD 取点”检查后重试。"
            self.picker_text.set(text)
            self.status.set(text)

    def continue_picker(self):
        if self._picker_guard():
            return
        if not self._picker_resume_available():
            self.status.set(self.picker_resume_reason)
            return
        self.open_picker(resume=True)

    def open_picker(self, resume=False):
        if self.closed:
            return
        if self._picker_active():
            if resume:
                self._picker_guard()
            else:
                self.stop_picker()
            return
        if self.picker_dialog is not None:
            self.picker_dialog.lift()
            return
        if self.picker_discovering:
            self._cancel_picker_discovery()
            self.status.set("已取消 CAD 图纸选择。")
            return
        if resume:
            self._show_picker_dialog(resume=True)
            return
        self.picker_discovering = True
        self.picker_discovery_token += 1
        token, revision = self.picker_discovery_token, self.file_revision
        self.picker_button_text.set("取消选图")
        self.status.set("正在读取 AutoCAD 当前打开的图纸…")
        def discover():
            from .cad_documents import list_open_drawings
            return list_open_drawings()
        def discovered(drawings, error):
            if self.closed or token != self.picker_discovery_token:
                return
            self._cancel_picker_discovery()
            if self._picker_active() or self.file_revision != revision:
                self.status.set("坐标或项目已改变，请重新点击 CAD 取点选择图纸。")
                return
            if error:
                self.error(error)
                return
            if not drawings:
                path = filedialog.askopenfilename(parent=self.root, title="选择要在 CAD 中取点的图纸",
                                                 filetypes=[("CAD 图纸", "*.dwg *.dxf")])
                if not path or self.closed or self._picker_active():
                    return
                drawings = [{"path": path, "name": Path(path).name}]
            self._show_picker_dialog(drawings=drawings)
        self._submit(discover, discovered)

    def _cancel_picker_discovery(self):
        self.picker_discovery_token += 1
        self.picker_discovering = False
        if not self._picker_active():
            self.picker_button_text.set("CAD 取点")

    def _show_picker_dialog(self, resume=False, drawings=None):
        previous = self.picker_previous if resume and self._picker_resume_available() else None
        if resume and previous is None:
            self.status.set(self.picker_resume_reason)
            return
        try:
            settings = self.settings()
        except ValueError as exc:
            self.error(exc)
            return
        dlg = ttk.Toplevel(self.root)
        self.picker_dialog = dlg
        def closed(event):
            if event.widget is dlg and self.picker_dialog is dlg:
                self.picker_dialog = None
        dlg.bind("<Destroy>", closed, add="+")
        dlg.title("继续 CAD 取点" if resume else "CAD 精确取点")
        dlg.transient(self.root)
        dlg.grab_set()
        body = ttk.Frame(dlg, padding=20)
        body.pack(fill="both", expand=True)
        heading = "继续当前 CAD 图纸，编号接续" if resume else "选择 CAD 图纸，直接取点并添加标注"
        ttk.Label(body, text=heading, font=("Microsoft YaHei", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        drawing_choices = [dict(item) for item in (drawings or [])]
        drawing_combo = None
        units = {6: "米（1 : 1）", 4: "毫米（乘 1000）", 5: "厘米（乘 100）"}
        source_frame = ttk.Frame(body)
        source_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=12)
        if resume:
            source = str(getattr(previous, "drawing_path", "沿用上一轮仍打开的 CAD 图纸"))
            ttk.Label(source_frame, text=f"图纸：{source}\n当前图纸单位：{self.unit.get()}；坐标表单位：米",
                      wraplength=530).pack(anchor="w")
        else:
            descriptions = [f"{i+1}. {'[当前] ' if item.get('active') else ''}{item.get('name') or Path(item.get('path') or '').name}"
                            for i, item in enumerate(drawing_choices)]
            drawing_combo = ttk.Combobox(source_frame, values=descriptions, state="readonly", width=66)
            drawing_combo.pack(fill="x")
            drawing_combo.current(0)
            detail = tk.StringVar()
            def selected_drawing(*_):
                target = drawing_choices[drawing_combo.current()]
                unit = units.get(target.get("units"))
                unit_text = f"图纸单位：{unit}" if unit else f"图纸单位未明确，使用标注设置：{self.unit.get()}"
                detail.set(f"{target.get('path') or '未保存的 CAD 图纸'}\n{unit_text}；坐标表单位：米\n标注直接添加到这张图纸，请在 CAD 中自行保存。")
            drawing_combo.bind("<<ComboboxSelected>>", selected_drawing)
            selected_drawing()
            ttk.Label(source_frame, textvariable=detail, wraplength=530).pack(anchor="w", pady=(6, 0))
        road = tk.StringVar(value=self.picker_road)
        start = tk.StringVar()
        height = tk.StringVar(value=f"{settings.text_height:g}")
        for row, (label, var) in enumerate((("道路名称 / 点名前缀", road), ("起始编号", start), ("取点标注字高（米）", height)), 2):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=7)
            ttk.Entry(body, textvariable=var, width=28).grid(row=row, column=1, sticky="ew", padx=(15, 0), pady=7)
        def next_number(*_):
            prefix = road.get().strip() + "-"
            indices = [int(p.name[len(prefix):]) for p in self.points if p.name[:len(prefix)].casefold() == prefix.casefold() and p.name[len(prefix):].isascii() and p.name[len(prefix):].isdigit()]
            start.set(str(max(indices, default=0) + 1))
        road.trace_add("write", next_number)
        next_number()
        ttk.Label(body, text="① 捕捉点位 → 移动鼠标摆放标注 → 左键或空格确认\n② 一条道路取完按 Enter，进入道路菜单\n③ 菜单 Enter / N 换路，C 继续当前路，Q 结束全部\n④ 等待点位时也可直接 N 换路，U 回退当前段的点\n⑤ 全部结束后，在 CAD 输入 ZB，填写道路名即可继续\n\n新道路从 01 开始；已有道路接续编号，已取点保留。\n换路时路名留空或 Esc，返回发起换路前的状态。\nZB 必须填写路名，留空或 Esc 取消本次续接。\n等待点位或道路菜单中按 Esc，将结束全部取点。", wraplength=530, bootstyle="light").grid(row=5, column=0, columnspan=2, sticky="w", pady=12)
        def begin():
            try:
                from dataclasses import replace
                from .core import validate_settings
                if resume and (self.picker_previous is not previous or not self._picker_resume_available()):
                    raise ValueError("当前坐标、设置或项目已改变，不能续接这张 CAD 图。请重新点击 CAD 取点。")
                name = road.get().strip()
                if not name or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise ValueError("请输入 1～100 字的道路名称，不能含换行或制表符。")
                first = int(start.get())
                if not 1 <= first <= 999999:
                    raise ValueError("起始编号应为 1～999999。")
                # Reserve the following numbering range, not just the first name.
                prefix = name + "-"
                if any(p.name[:len(prefix)].casefold() == prefix.casefold() and p.name[len(prefix):].isascii() and p.name[len(prefix):].isdigit() and int(p.name[len(prefix):]) >= first for p in self.points):
                    raise ValueError("这个编号范围已有坐标，请使用自动接续的编号或换一个道路名称。")
                target = None if resume else drawing_choices[drawing_combo.current()]
                if target is not None and target.get("units") in units:
                    self.unit.set(units[target["units"]])
                selected = replace(self.settings(), text_height=float(height.get()))
                validate_settings(selected)
            except (ValueError, OverflowError) as exc:
                messagebox.showerror("请检查取点设置", str(exc), parent=dlg)
                return
            dlg.destroy()
            self._start_picker(name, first, selected, resume_from=previous, target=target)
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=dlg.destroy, bootstyle="light-outline").pack(side="left", padx=6)
        ttk.Button(buttons, text="继续当前 CAD 图" if resume else "开始 CAD 取点", command=begin, bootstyle="success").pack(side="left")
        dlg.update_idletasks()
        width, height_px = max(590, dlg.winfo_reqwidth()), dlg.winfo_reqheight()
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width()-width)//2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height()-height_px)//2)
        dlg.geometry(f"{width}x{height_px}+{x}+{y}")
        dlg.resizable(False, False)

    def _start_picker(self, road, start, settings, resume_from=None, from_cad=False, target=None):
        from .picker import PickerSession
        if self._picker_active():
            return
        if resume_from is None and not target:
            self.status.set("请先选择当前 CAD 图纸，或选择要打开的 DWG/DXF 文件。")
            return
        if resume_from is not None and (self.picker_previous is not resume_from or not self._picker_resume_available()):
            self.status.set("续接条件已改变，请重新开始 CAD 取点。")
            return
        if resume_from is None:
            # A late preview result must not replace the chosen CAD context.
            if self.loading_drawing:
                self.drawing_revision += 1
                self.loading_drawing = False
                self._sync_base_controls()
            if self.base_path:
                import os
                if (not target.get("path") or
                        os.path.normcase(str(Path(self.base_path).resolve())) != os.path.normcase(str(Path(target["path"]).resolve()))):
                    self.clear_base()
        self._invalidate_picker_resume("本轮尚未正常结束，暂时不能继续 CAD 取点。")
        self.file_revision += 1  # invalidate any pending file import/editor
        self.pending_coordinates = None
        self.picker_starting = True
        self.picker_stop_requested = False
        self.picker_before = self.points[:]
        self.picker_settings = self.settings()
        self.picker_road = road
        self.picker_started_at = monotonic()
        self.picker_button_text.set("结束取点")
        self.picker_text.set(f"正在准备继续 {road}，沿用当前 CAD 工作图…" if resume_from is not None
                             else f"正在连接 CAD 图纸：{target.get('name') or target.get('path')}…")
        self._update_picker_resume()
        points = self.points[:]
        snapshot_settings = self.picker_settings
        def report_start_error(error):
            if from_cad:
                self.status.set(f"CAD ZB 续接未完成：{error}")
            else:
                self.error(error)
        def prepared(session, error):
            if error:
                self._finish_picker_ui(f"CAD 取点启动失败：{error}")
                report_start_error(error)
                return
            if from_cad:
                session.wait_for_idle = True
            self.picker_session = session
            self.picker_starting = False
            self.picker_text.set(f"正在续接当前 CAD 工作图：{road}，请稍候…" if resume_from is not None
                                 else f"正在启动 AutoCAD：{road}，请稍候…")
            def launched(_, error):
                if self.picker_session is not session:
                    return
                if error:
                    # A send failure may be ambiguous: first drain durable events.
                    self._poll_picker(force=True)
                    if self.picker_session is session and not session.ready:
                        session.request_stop()
                        self._finish_picker_ui(f"CAD 启动未完成，恢复记录：{session.recovery_path}")
                    report_start_error(error)
            self._submit(session.launch, launched)
        if resume_from is None:
            self._submit(lambda: PickerSession.attach(target, road, start, settings, initial_points=points,
                                                     snapshot_settings=snapshot_settings), prepared)
        else:
            self._submit(lambda: PickerSession.resume(resume_from, road, start, settings, initial_points=points,
                                                     snapshot_settings=snapshot_settings), prepared)

    def stop_picker(self):
        if self.picker_starting:
            self.picker_text.set("正在准备图纸，请稍候；CAD 打开后可输入 Q 结束全部取点。")
            return
        if self.picker_session:
            if self.picker_stop_requested:
                session = self.picker_session
                if not messagebox.askyesno("断开 CAD 取点", "还没有收到 CAD 的结束回执。是否保留已回传的坐标并断开？\n已请求停止，请切回 CAD 按 Enter 或 Esc 响应；原始记录会保留，图纸请在 CAD 中保存。", parent=self.root):
                    return
                self._poll_picker(force=True)
                if self.picker_session is session:
                    self._finish_picker_ui(f"已断开 CAD 取点；恢复记录：{session.recovery_path}")
                return
            try:
                self.picker_session.request_stop()
            except OSError as exc:
                self.error(exc)
                return
            self.picker_stop_requested = True
            self.picker_button_text.set("断开取点")
            self.picker_text.set("已请求结束全部取点，请切回 CAD 按 Enter 或 Esc 响应；已确认的点会保留。")

    def _poll_picker(self, force=False):
        session = self.picker_session
        now = monotonic()
        if session is None:
            self._poll_zb()
            return
        if not force and now - self.picker_polled_at < .25:
            return
        self.picker_polled_at = now
        try:
            events = session.poll()
            if events:
                points = session.apply(self.points, events)
                if points != self.points:
                    self._changed_points(points, "CAD 取点", record=False, fit=not self.points and not self.base_bounds, from_picker=True)
                    if self.points:
                        self.tree.selection_set(str(len(self.points)-1))
                        self.tree.see(str(len(self.points)-1))
                self.picker_road = getattr(session, "current_road", self.picker_road)
                captured = sum(p.capture_id.startswith(session.session_id + ":") for p in self.points)
                if self.picker_stop_requested:
                    self.picker_text.set(f"已请求结束全部取点 · 已回传 {captured} 个点 · 请在 CAD 按 Enter 或 Esc 响应")
                else:
                    self.picker_text.set(f"正在 CAD 取点：{self.picker_road} · 已回传 {captured} 个点 · Enter 道路菜单 / N 换道路 / Q 结束全部")
                if session.done:
                    text = f"全部取点已结束，已回传 {captured} 个点。"
                    if session.ready and not session.last_error:
                        text += "在 CAD 输入 ZB，填写道路名即可继续取点。"
                    if session.last_error:
                        text = f"取点已停止：{session.last_error}；已确认的 {captured} 个点已保留。"
                    self._finish_picker_ui(text, allow_resume=True)
            elif not session.ready and now - self.picker_started_at > 60:
                self.picker_text.set("等待 AutoCAD 就绪；请检查 CAD 窗口是否有启动提示。已确认的点和恢复记录会保留。")
        except Exception as exc:
            try:
                session.request_stop()
            except OSError:
                pass
            self._finish_picker_ui(f"回传暂停；原始取点记录保留在 {session.directory}")
            self.error(ValueError(f"取点回传未完成：{exc}\n原始记录：{session.directory}"))

    def _finish_picker_ui(self, text, allow_resume=False):
        session = self.picker_session
        if allow_resume and session is not None and session.ready and session.done and not session.last_error:
            self.picker_previous = session
            self.picker_previous_points = self.points[:]
            self.picker_previous_settings = self.picker_settings
            self.picker_previous_base = self.base_path
            self.picker_resume_reason = "请保持软件和 CAD 工作图打开，在 CAD 输入 ZB 并填写道路名。"
        if self.points != self.picker_before:
            self.undo.append((f"CAD 取点：{self.picker_road}", self.picker_before[:]))
            self.undo = self.undo[-30:]
        self.picker_session = None
        self.picker_starting = False
        self.picker_button_text.set("CAD 取点")
        self.picker_text.set(text)
        self._update_picker_resume()
        self._poll_zb()

    def recover_picker(self):
        if self._picker_guard():
            return
        import os
        folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CoordinateGeneratorV22" / "picks"
        path = filedialog.askopenfilename(parent=self.root, title="恢复取点记录（每个会话文件夹内）", initialdir=folder if folder.exists() else None, filetypes=[("取点恢复 / 坐标项目", "*.coordproj *.json")])
        if path:
            self.open_project(path, recover_picker=True)
