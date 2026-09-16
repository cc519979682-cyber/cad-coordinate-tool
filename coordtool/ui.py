"""Native desktop UI. File parsing/layout run off the Tk event thread."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from queue import Queue, Empty
import json
import math
import os
import tkinter as tk
from tkinter import filedialog, messagebox
import traceback
from time import monotonic

import ttkbootstrap as ttk
import matplotlib
import numpy as np
from matplotlib.figure import Figure
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.patches import Circle
from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk
from .interaction import ResponsiveCanvas

from .core import Point, Settings, read_points, parse_text, write_points, calc_labels, validate_settings, CoordinateOrderRequired, annotation_segments, annotation_texts, connection_groups
from .picker_ui import PickerUIMixin
from . import cad

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
COLORS = {"红色": (1, "#ff666d"), "黄色": (2, "#f0cc59"), "绿色": (3, "#65d699"),
          "青色": (4, "#5bd3e8"), "蓝色": (5, "#80a7ff"), "紫色": (6, "#d49cff"), "白色": (7, "#eeeeee")}
ORDERS = {"自动识别（表头 / 底图匹配）": "auto", "点名 / 东 E / 北 N": "EN", "点名 / 北 N / 东 E": "NE"}
UNITS = {"米（1 : 1）": 1.0, "毫米（乘 1000）": 1000.0, "厘米（乘 100）": 100.0}


class CoordinateApp(PickerUIMixin):
    def __init__(self, resources: Path):
        self.resources = Path(resources)
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            self.root = TkinterDnD.Tk()
            self.style = ttk.Style("darkly")
            self.dnd_type = DND_FILES
        except (ImportError, tk.TclError):
            self.root = ttk.Window(themename="darkly")
            self.dnd_type = None
        self.root.title("坐标生成器 v22.8 · CAD 取点联动")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{min(1500, sw-70)}x{min(940, sh-90)}")
        self.root.minsize(1080, 680)
        self.points = []
        self.undo = []
        self.base_doc = None
        self.base_preview = None
        self.base_path = None
        self.base_warnings = []
        self.base_bounds = None
        self.labels = []
        self.artists = []
        self.label_artists = []
        self.highlight = None
        self.preview_job = None
        self.preview_revision = 0
        self.file_revision = 0
        self.drawing_revision = 0
        self.loading_drawing = False
        self.pending_coordinates = None
        self.interaction_job = None
        self.interacting = False
        self.toolbar_drag = False
        self._selection_background = None
        self._cursor_updated = 0.0
        self.geometry_cache = None
        self.closed = False
        self.dirty = False
        self.project_path = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="coord")
        self.results = Queue()
        self.drawing_progress = Queue()
        self.status = tk.StringVar(value="先打开坐标文件或 CAD 底图，也可以点击“示例图”体验。")
        self.drawing_status = tk.StringVar(value="未打开底图")
        self.base_toggle_text = tk.StringVar(value="打开底图")
        self.count_text = tk.StringVar(value="0 个坐标点")
        self.import_text = tk.StringVar(value="支持两列坐标、三列点名坐标、四列含高程。")
        self.cursor_text = tk.StringVar(value="CAD：X = 东 E，Y = 北 N")
        self.order = tk.StringVar(value=next(iter(ORDERS)))
        self.unit = tk.StringVar(value=next(iter(UNITS)))
        self.color = tk.StringVar(value="红色")
        self.layer = tk.StringVar(value="COORD_POINTS")
        self.lineweight = tk.StringVar(value="0.50")
        self.radius = tk.StringVar(value="1.5")
        self.leader = tk.StringVar(value="20")
        self.text_height = tk.StringVar(value="2")
        self.offset_e = tk.StringVar(value="0")
        self.offset_n = tk.StringVar(value="0")
        self.draw_line = tk.BooleanVar(value=Settings().draw_line)
        self.closed_line = tk.BooleanVar(value=False)
        self.show_base = tk.BooleanVar(value=False)
        self.show_labels = tk.BooleanVar(value=True)
        self._init_picker()
        self._build()
        self._sync_base_controls()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-o>", lambda e: self.open_coordinates())
        self.root.bind("<Control-s>", lambda e: self.save_project())
        self.root.bind("<Control-z>", self._undo_shortcut)
        self.root.after(80, self._poll)
        self._blank_preview()

    def _build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="坐标生成器", font=("Microsoft YaHei", 19, "bold")).pack(side="left")
        ttk.Label(header, text="  v22.8  /  CAD 取点联动", bootstyle="info", font=("Microsoft YaHei", 11)).pack(side="left")
        for text, cmd, style in [("保存项目", self.save_project, "light-outline"),
                                  ("打开项目", self.open_project, "light-outline"),
                                  ("示例图", self.load_demo, "info-outline")]:
            ttk.Button(header, text=text, command=cmd, bootstyle=style).pack(side="right", padx=3)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 10))
        for text, cmd, style in [("打开坐标", self.open_coordinates, "primary"),
                                  ("粘贴坐标", self.paste, "secondary"),
                                  ("打开 CAD 底图", self.open_base, "info"),
                                  ("导出带标注 DXF", self.export_dxf, "success"),
                                  ("在 CAD 中打开…", self.open_cad, "success-outline")]:
            ttk.Button(actions, text=text, command=cmd, bootstyle=style, padding=(12, 8)).pack(side="left", padx=(0, 7))
        self.picker_button = ttk.Button(actions, textvariable=self.picker_button_text, command=self.open_picker, bootstyle="warning", padding=(12, 8))
        self.picker_button.pack(side="left", padx=(0, 7))
        self._build_picker_bar(outer)
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, width=430)
        # Keep room for the elevation column when it appears after import.
        left.pack_propagate(False)
        right = ttk.Frame(panes)
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        tabs = ttk.Notebook(left)
        tabs.pack(fill="both", expand=True, padx=(0, 10))
        data = ttk.Frame(tabs, padding=10)
        settings = ttk.Frame(tabs, padding=12)
        help_tab = ttk.Frame(tabs, padding=12)
        tabs.add(data, text="  坐标点  ")
        tabs.add(settings, text="  标注设置  ")
        tabs.add(help_tab, text="  使用说明  ")
        ttk.Label(data, text="导入列顺序", bootstyle="secondary").pack(anchor="w")
        ttk.Combobox(data, textvariable=self.order, values=list(ORDERS), state="readonly").pack(fill="x", pady=(3, 7))
        ttk.Label(data, textvariable=self.import_text, bootstyle="info", wraplength=365, font=("Microsoft YaHei", 9)).pack(anchor="w", fill="x", pady=(0, 4))
        ttk.Label(data, textvariable=self.count_text, font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(3, 6))
        table_frame = ttk.Frame(data)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=("name", "e", "n", "z"), displaycolumns=("name", "e", "n"), show="headings", selectmode="extended", height=14)
        for name, title, width in [("name", "点名", 103), ("e", "东 E（米）", 95), ("n", "北 N（米）", 95), ("z", "高程（米）", 75)]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70, anchor="e" if name != "name" else "w")
        scroll = ttk.Scrollbar(table_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(data, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=horizontal.set)
        horizontal.pack(fill="x", pady=(2, 0))
        self.tree.bind("<Double-1>", lambda e: self.edit_point())
        self.tree.bind("<<TreeviewSelect>>", self.select_point)
        self.tree.bind("<Delete>", lambda e: self.delete_points())
        self.tree.bind("<Button-3>", self._context_menu)
        rows = ttk.Frame(data)
        rows.pack(fill="x", pady=(8, 0))
        for i, (text, cmd) in enumerate([("新增", lambda: self.edit_point(new=True)), ("编辑", self.edit_point),
                                         ("删除", self.delete_points), ("撤销", self.undo_points),
                                         ("E / N 调换", self.swap), ("导出坐标", self.export_points)]):
            ttk.Button(rows, text=text, command=cmd, bootstyle="light-outline").grid(row=i//3, column=i%3, sticky="ew", padx=2, pady=3)
        for i in range(3):
            rows.columnconfigure(i, weight=1)
        ttk.Label(data, text="双击编辑 · 可拖入 TXT / CSV / Excel\n单击表格或图中圆心，联动选中坐标点", bootstyle="secondary", font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(8, 0))
        self._build_settings(settings)
        help_text = ("1  打开坐标\n自动识别分隔符、东/北表头和四列高程。\n无表头时结合已打开底图判断东/北顺序；\n不能唯一判断时显示样例供选择。\n高程保存在点表和项目中，预览与标注为二维。\n原始坐标始终按米保存。\n\n"
                     "2  打开 CAD 底图\n“关闭底图”后再点“打开底图”直接恢复。\n切换只影响显示，不需重新选择或加载文件。\nDWG优先通过本机AutoCAD或ODA转换副本。\nDXF直接读取；视口、嵌入表格不作为二维\n底图显示，图纸内容保留在导出副本中。\n图纸和坐标须使用同一坐标系及图纸单位。\n\n"
                     "3  预览与输出\n默认只显示点位和标注，不连接各点。\n只有一条连续线路才手动开启“连接全部点”。\n滚轮缩放，鼠标中键拖动；单击点可定位。\n标注与底图一起另存DXF，可用CAD打开。\nLSP可加载到已有图纸，命令BINDLINE。\n反推命令EXPORTCOORD，确认单位后输出米\n坐标，可直接重新导入。\n\n"
                     "4  CAD 取点\n选择CAD当前图纸，可下拉切换其他图。\n没有打开图纸时，先选择DWG/DXF文件。\n标注直接加到所选图纸，请在CAD保存。\n设置道路名称后，在CAD捕捉并摆放标注。\n左键/空格确认；Enter进入道路菜单。\n菜单Enter/N换路，C继续当前路。\n等待点位时N换路，U回退当前段。\nQ结束全部后，才能改坐标或更换项目。\n换路留空/Esc取消，返回先前状态。\n点位、高程和标注位置自动回传并保存。\n\n结束后，保持CAD图纸和软件打开，\n在CAD输入 ZB，再填写道路名即可继续。\nZB路名必填；留空/Esc取消，不会取点。\n也可点击软件的“继续 CAD 取点”。\n修改坐标、设置或项目后，需重新开始。\n\n保存项目保留坐标、样式和底图路径。\n底图换电脑请一起携带。这里只预览二维。\nCtrl+O 打开坐标    Ctrl+S 保存项目")
        ttk.Label(help_tab, text=help_text, justify="left", font=("Microsoft YaHei", 10), wraplength=350).pack(anchor="nw")
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(0, 5))
        ttk.Label(bar, textvariable=self.drawing_status, bootstyle="info", width=33, anchor="w").pack(side="left", fill="x", expand=True)
        for text, cmd in [("全图", lambda: self.fit("all")), ("底图范围", lambda: self.fit("drawing")), ("坐标范围", lambda: self.fit("points"))]:
            ttk.Button(bar, text=text, command=cmd, bootstyle="light-outline", padding=(7, 3)).pack(side="right", padx=2)
        self.base_toggle_button = ttk.Button(bar, textvariable=self.base_toggle_text, command=self.toggle_base_visibility,
                                            bootstyle="light-outline", padding=(7, 3))
        self.base_toggle_button.pack(side="right", padx=2)
        self.fig = Figure(figsize=(9, 7), dpi=100, facecolor="#18212c")
        self.ax = self.fig.add_axes((0.065, 0.075, 0.92, 0.9))
        self.canvas = ResponsiveCanvas(self.fig, right)
        self.canvas.before_draw = self._before_canvas_draw
        self.canvas.on_invalidate = self._invalidate_selection
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        nav = ttk.Frame(right)
        nav.pack(fill="x", pady=(4, 0))
        self.toolbar = NavigationToolbar2Tk(self.canvas, nav, pack_toolbar=False)
        self.toolbar.pack(side="left")
        self.base_visibility_check = ttk.Checkbutton(nav, text="底图", variable=self.show_base, command=self.toggle_base)
        self.base_visibility_check.pack(side="right", padx=5)
        ttk.Checkbutton(nav, text="点名", variable=self.show_labels, command=self.toggle_labels).pack(side="right", padx=5)
        ttk.Checkbutton(nav, text="连接全部点", variable=self.draw_line, command=self._settings_changed).pack(side="right", padx=5)
        ttk.Label(right, textvariable=self.cursor_text, bootstyle="secondary", font=("Consolas", 9)).pack(fill="x")
        self.canvas.mpl_connect("scroll_event", self.zoom)
        self.canvas.mpl_connect("button_press_event", self.mouse_down)
        self.canvas.mpl_connect("button_release_event", self.mouse_up)
        self.canvas.mpl_connect("motion_notify_event", self.mouse_move)
        self.canvas.mpl_connect("draw_event", self._cache_selection_background)
        self.pan_start = None
        self._connect_axes()
        ttk.Separator(outer).pack(fill="x", pady=(8, 6))
        ttk.Label(outer, textvariable=self.status, anchor="w", wraplength=1400, font=("Microsoft YaHei", 9)).pack(fill="x")
        if self.dnd_type:
            for widget in (self.tree, self.canvas.get_tk_widget()):
                widget.drop_target_register(self.dnd_type)
                widget.dnd_bind("<<Drop>>", self.drop)

    def _build_settings(self, parent):
        ttk.Label(parent, text="落图与标注", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fields = [("图纸单位", self.unit, list(UNITS)), ("标注颜色", self.color, list(COLORS)),
                  ("线宽（mm）", self.lineweight, ["0.25", "0.35", "0.50", "0.70", "1.00"]),
                  ("标注图层", self.layer, None), ("圆圈半径（米）", self.radius, None),
                  ("引线长度（米）", self.leader, None), ("文字高度（米）", self.text_height, None),
                  ("东 E 偏移（米）", self.offset_e, None), ("北 N 偏移（米）", self.offset_n, None)]
        for row, (label, var, choices) in enumerate(fields, 1):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=7)
            if choices:
                entry = ttk.Combobox(parent, textvariable=var, values=choices, state="readonly", width=20)
            else:
                entry = ttk.Entry(parent, textvariable=var, width=23)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
            var.trace_add("write", self._settings_changed)
        parent.columnconfigure(1, weight=1)
        ttk.Checkbutton(parent, text="连接全部点（按列表顺序）", variable=self.draw_line, command=self._settings_changed).grid(row=10, column=0, columnspan=2, sticky="w", pady=9)
        ttk.Checkbutton(parent, text="首尾闭合", variable=self.closed_line, command=self._settings_changed).grid(row=11, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Separator(parent).grid(row=12, column=0, columnspan=2, sticky="ew", pady=14)
        ttk.Button(parent, text="生成标注 LSP", command=self.export_lsp, bootstyle="info-outline").grid(row=13, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(parent, text="生成反推坐标 LSP", command=self.export_reverse, bootstyle="light-outline").grid(row=14, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Label(parent, text="默认只显示点位和标注。仅当列表是一条\n连续线路时开启连线，不会自动分道路或地块。\n落图坐标 =（原始坐标 + 偏移）× 单位比例\n预览采用等比例显示，文字高度与CAD对应。",
                  bootstyle="secondary", font=("Microsoft YaHei", 9), wraplength=350).grid(row=15, column=0, columnspan=2, sticky="w", pady=14)

    def settings(self):
        try:
            s = Settings(radius=float(self.radius.get()), leader=float(self.leader.get()), text_height=float(self.text_height.get()),
                         color=COLORS[self.color.get()][0], lineweight=round(float(self.lineweight.get())*100), layer=self.layer.get().strip(),
                         draw_line=self.draw_line.get(), closed=self.closed_line.get(), offset_e=float(self.offset_e.get()),
                         offset_n=float(self.offset_n.get()), scale=UNITS[self.unit.get()])
            validate_settings(s)
            return s
        except (ValueError, KeyError) as exc:
            raise ValueError(f"请检查标注设置：{exc}") from exc

    def _settings_changed(self, *_):
        if self._restoring_picker_settings:
            return
        if self._picker_active() and self.picker_settings is not None:
            self._restoring_picker_settings = True
            try:
                self._set_settings(self.picker_settings)
            finally:
                self._restoring_picker_settings = False
            self._picker_guard()
            return
        self.dirty = True
        self.schedule_preview()
        self._update_picker_resume()

    def _submit(self, fn, callback):
        future = self.executor.submit(fn)
        future.add_done_callback(lambda f: self.results.put((f, callback)))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                revision, progress = self.drawing_progress.get_nowait()
                if revision == self.drawing_revision:
                    self.status.set(progress)
        except Empty:
            pass
        try:
            while True:
                future, callback = self.results.get_nowait()
                try:
                    value = future.result()
                except Exception as exc:
                    callback(None, exc)
                else:
                    try:
                        callback(value, None)
                    except Exception as exc:
                        self.error(exc)
        except Empty:
            pass
        self._poll_picker()
        self.root.after(80, self._poll)

    def error(self, exc):
        self.status.set(str(exc))
        messagebox.showerror("操作未完成", str(exc), parent=self.root)

    def _changed_points(self, points, action="修改坐标", record=True, fit=True, from_picker=False):
        if not from_picker and self._picker_guard():
            return
        self.file_revision += 1
        # An edit/undo supersedes any import queued behind a slow drawing load.
        self.pending_coordinates = None
        if record:
            self.undo.append((action, self.points[:]))
            self.undo = self.undo[-30:]
        self.points = list(points)
        self.dirty = True
        self.refresh_table()
        self.schedule_preview(fit=fit)
        self._update_picker_resume()

    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        has_height = any(p.z is not None for p in self.points)
        self.tree.configure(displaycolumns=("name", "e", "n", "z") if has_height else ("name", "e", "n"))
        # Tk expands the original three columns to fill the table before a
        # background import adds Z. Reset those stretched widths when showing
        # four columns, otherwise Z starts outside the visible table.
        for column, width in (("name", 103), ("e", 95), ("n", 95), ("z", 75)):
            self.tree.column(column, width=width, stretch=not has_height)
        for i, p in enumerate(self.points):
            values = tuple("" if value is None else format(value, ".10f").rstrip("0").rstrip(".") for value in (p.e, p.n, p.z))
            self.tree.insert("", "end", iid=str(i), values=(p.name, *values))
        self.count_text.set(f"{len(self.points)} 个坐标点  ·  米")

    def open_coordinates(self):
        if self._picker_guard():
            return
        path = filedialog.askopenfilename(parent=self.root, title="打开坐标文件", filetypes=[("坐标文件", "*.txt *.csv *.tsv *.xlsx *.xls"), ("所有文件", "*.*")])
        if path:
            self.load_coordinates(path)

    def load_coordinates(self, path, order_override=None):
        if self._picker_guard():
            return
        if self.loading_drawing and order_override is None and ORDERS[self.order.get()] == "auto":
            self.file_revision += 1
            self.pending_coordinates = ("file", path)
            self.status.set("坐标文件已接收，正在等待底图范围以自动判断东 / 北顺序…")
            return
        self.pending_coordinates = None
        self._import_coordinates(lambda order, bounds, settings: read_points(path, order=order, reference_bounds=bounds, settings=settings),
                                 Path(path).name, "导入坐标", order_override)

    def _import_coordinates(self, reader, source_name, action, order_override=None):
        self.file_revision += 1
        rev = self.file_revision
        order = order_override or ORDERS[self.order.get()]
        try:
            settings = self.settings()
        except ValueError as exc:
            self.error(exc)
            return
        bounds = self.base_bounds
        self.status.set(f"正在识别坐标：{source_name}")
        def done(result, error):
            if rev != self.file_revision:
                return
            if isinstance(error, CoordinateOrderRequired):
                self._choose_coordinate_order(error, lambda chosen: self._import_coordinates(reader, source_name, action, chosen), rev)
                return
            if error:
                self.error(error)
                return
            self._changed_points(result.points, action)
            self.import_text.set(result.description or f"已按 {result.detected_order or order.upper()} 导入 {len(result.points)} 个点")
            self.status.set(f"已读取 {len(result.points)} 个坐标点：{source_name}")
            if result.warnings:
                messagebox.showwarning("坐标导入提示", "\n".join(result.warnings), parent=self.root)
        self._submit(lambda: reader(order, bounds, settings), done)

    def _choose_coordinate_order(self, error, on_selected, revision):
        self.status.set("文件格式已识别，请根据样例确认东 / 北顺序；当前坐标尚未改变。")
        dlg = ttk.Toplevel(self.root)
        dlg.title("确认本文件的坐标顺序")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        body = ttk.Frame(dlg, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="这份文件的东 / 北顺序还不能唯一确定", font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=str(error.reason) + "\n可取消后先打开对应底图，再重新导入自动匹配。", wraplength=570).pack(anchor="w", pady=(9, 12))
        sample = "\n".join("  |  ".join(map(str, row)) for row in error.sample_rows)
        ttk.Label(body, text="文件前几行：\n" + sample, font=("Consolas", 10), wraplength=590, bootstyle="info").pack(anchor="w", pady=(0, 15))
        def choose(order):
            dlg.destroy()
            if revision == self.file_revision:
                on_selected(order)
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        ttk.Button(actions, text="前一列为北 N，后一列为东 E", command=lambda: choose("NE"), bootstyle="primary").pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="前一列为东 E，后一列为北 N", command=lambda: choose("EN"), bootstyle="info").pack(side="left")
        ttk.Button(body, text="取消导入", command=dlg.destroy, bootstyle="light-outline").pack(anchor="e", pady=(12, 0))
        dlg.bind("<Escape>", lambda event: dlg.destroy())

    def paste(self):
        if self._picker_guard():
            return
        try:
            text = self.root.clipboard_get()
            if self.loading_drawing and ORDERS[self.order.get()] == "auto":
                self.file_revision += 1
                self.pending_coordinates = ("text", text)
                self.status.set("剪贴板坐标已接收，正在等待底图范围以自动判断东 / 北顺序…")
                return
            self.pending_coordinates = None
            self._import_clipboard(text)
        except Exception as exc:
            self.error(exc)

    def _import_clipboard(self, text):
        self._import_coordinates(lambda order, bounds, settings: parse_text(text, order=order, reference_bounds=bounds, settings=settings),
                                 "剪贴板", "粘贴坐标")

    def edit_point(self, new=False):
        if self._picker_guard():
            return
        selected = self.tree.selection()
        if not new and not selected:
            self.status.set("请先选中要编辑的坐标点。")
            return
        idx = len(self.points) if new else int(selected[0])
        p = Point(f"点{idx+1}", 0, 0) if new else self.points[idx]
        edit_revision = self.file_revision
        dlg = ttk.Toplevel(self.root)
        dlg.title("新增坐标点" if new else "编辑坐标点")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        variables = [tk.StringVar(value=p.name), tk.StringVar(value=repr(p.e)), tk.StringVar(value=repr(p.n)), tk.StringVar(value="" if p.z is None else repr(p.z))]
        for row, (label, var) in enumerate(zip(["点名", "东 E（米）", "北 N（米）", "高程（米，可留空）"], variables)):
            ttk.Label(dlg, text=label).grid(row=row, column=0, padx=16, pady=10, sticky="w")
            entry = ttk.Entry(dlg, textvariable=var, width=30)
            entry.grid(row=row, column=1, padx=(0, 16), pady=10)
            if row == 0:
                entry.focus_set()
        def save():
            try:
                if edit_revision != self.file_revision:
                    raise ValueError("坐标列表已在后台更新，请关闭此窗口后重新选择要编辑的点。")
                name = variables[0].get().strip()
                if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise ValueError("点名不能为空或包含换行。")
                e, n = float(variables[1].get()), float(variables[2].get())
                if not math.isfinite(e) or not math.isfinite(n):
                    raise ValueError("坐标必须是有限数字。")
                z_text = variables[3].get().strip()
                z = float(z_text) if z_text else None
                if z is not None and not math.isfinite(z):
                    raise ValueError("高程必须是有限数字或留空。")
                points = self.points[:]
                if new:
                    points.append(Point(name, e, n, z))
                else:
                    placement = p.placement
                    if placement is not None:
                        placement = replace(placement, e=placement.e+e-p.e, n=placement.n+n-p.n, horizontal_e=placement.horizontal_e+e-p.e)
                    points[idx] = replace(p, name=name, e=e, n=n, z=z, placement=placement)
                self._changed_points(points, "新增坐标" if new else "编辑坐标")
                dlg.destroy()
                self.tree.selection_set(str(idx))
                self.tree.see(str(idx))
            except ValueError as exc:
                messagebox.showerror("坐标格式错误", str(exc), parent=dlg)
        ttk.Button(dlg, text="保存", command=save, bootstyle="success").grid(row=4, column=1, sticky="e", padx=16, pady=12)
        dlg.bind("<Return>", lambda e: save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="编辑坐标", command=self.edit_point)
        menu.add_command(label="删除选中（可撤销）", command=self.delete_points)
        menu.tk_popup(event.x_root, event.y_root)

    def delete_points(self):
        selected = set(map(int, self.tree.selection()))
        if selected:
            self._changed_points([p for i, p in enumerate(self.points) if i not in selected], "删除坐标")

    def swap(self):
        if self.points:
            # A swapped horizontal leader is no longer horizontal. Re-layout it.
            self._changed_points([replace(p, e=p.n, n=p.e, placement=None) for p in self.points], "E/N 调换")

    def _undo_shortcut(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return
        self.undo_points()

    def undo_points(self):
        if self._picker_guard():
            return
        if self.undo:
            action, points = self.undo.pop()
            self._changed_points(points, record=False)
            self.status.set(f"已撤销：{action}")

    def drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return
        path = paths[0]
        if Path(path).suffix.lower() in (".dxf", ".dwg"):
            self.load_base(path)
        elif Path(path).suffix.lower() == ".json":
            self.open_project(path)
        else:
            self.load_coordinates(path)

    def open_base(self):
        if self._picker_guard():
            return
        path = filedialog.askopenfilename(parent=self.root, title="打开 CAD 底图", filetypes=[("CAD 图纸", "*.dxf *.dwg"), ("DXF", "*.dxf"), ("DWG 简图", "*.dwg")])
        if path:
            self.load_base(path)

    def load_base(self, path, adopt_units=True, mark_dirty=True):
        if self._picker_guard():
            return
        self._invalidate_picker_resume("底图已重新选择，请使用 CAD 取点开始新的工作图。")
        self.drawing_revision += 1
        revision = self.drawing_revision
        self.loading_drawing = True
        self._sync_base_controls()
        hint = "（首次转换可能需要一些时间）" if Path(path).suffix.lower() == ".dwg" else ""
        self.status.set(f"正在读取底图：{Path(path).name}…{hint}")
        def done(result, error):
            if revision != self.drawing_revision:
                return
            self.loading_drawing = False
            if error:
                queued = self.pending_coordinates is not None
                self.pending_coordinates = None
                if queued:
                    self.file_revision += 1
                self._sync_base_controls()
                self.error(ValueError(f"{error}\n等待底图的坐标导入已取消，请打开有效底图后重新导入坐标。") if queued else error)
                return
            doc, warnings, preview = result
            old_doc, old_path, old_warn, old_preview = self.base_doc, self.base_path, self.base_warnings, self.base_preview
            old_visible = self.show_base.get()
            self.base_doc, self.base_path, self.base_warnings, self.base_preview = doc, Path(path).resolve(), warnings, preview
            self.show_base.set(True)
            try:
                self._render_base()
            except Exception:
                self.pending_coordinates = None
                self.file_revision += 1
                self.base_doc, self.base_path, self.base_warnings, self.base_preview = old_doc, old_path, old_warn, old_preview
                self.show_base.set(old_visible)
                self._sync_base_controls()
                self._render_base()
                raise
            if mark_dirty:
                self.dirty = True
            self._sync_base_controls()
            # Units are explicit; match known drawing units without guessing coordinates.
            unit_id = doc.header.get("$INSUNITS", 0)
            matching = {6: "米（1 : 1）", 4: "毫米（乘 1000）", 5: "厘米（乘 100）"}.get(unit_id)
            if matching and adopt_units:
                self.unit.set(matching)
            elif not matching:
                warnings = list(warnings) + ["底图未声明米/厘米/毫米单位，请在标注设置里核对图纸单位。"]
            self.pending_drawing_fit = True
            self.schedule_preview(fit=True)
            self.status.set(f"已打开底图：{Path(path).name}" + (" · 简图/兼容提示见弹窗" if warnings else ""))
            if warnings:
                messagebox.showwarning("底图读取提示", "\n".join(warnings), parent=self.root)
            if self.pending_coordinates:
                kind, pending_value = self.pending_coordinates
                self.pending_coordinates = None
                if kind == "text":
                    self._import_clipboard(pending_value)
                else:
                    self.load_coordinates(pending_value)
        def read_and_prepare():
            from .preview import prepare_preview
            doc, warnings = cad.load_drawing(path)
            self.drawing_progress.put((revision, f"图纸已读取，正在准备 {len(doc.modelspace()):,} 个模型实体的预览…"))
            preview = prepare_preview(doc)
            return doc, list(dict.fromkeys([*warnings, *preview.warnings])), preview
        self._submit(read_and_prepare, done)

    def _connect_axes(self):
        self.ax.callbacks.connect("xlim_changed", self._view_changed)
        self.ax.callbacks.connect("ylim_changed", self._view_changed)

    def _view_changed(self, *_):
        self._invalidate_selection()

    def _before_canvas_draw(self):
        # Equal-aspect axes can change their pixel height during layout. Apply
        # aspect first so text is sized correctly in this same frame.
        self.ax.apply_aspect()
        self._update_text_sizes()

    def _invalidate_selection(self):
        self._selection_background = None

    def _selection_view_key(self):
        return (tuple(self.ax.get_xlim()), tuple(self.ax.get_ylim()), tuple(self.ax.bbox.bounds), self.fig.dpi)

    def _cache_selection_background(self, event):
        if event.canvas is not self.canvas or self.canvas.is_saving():
            return
        self._selection_background = self.canvas.copy_from_bbox(self.ax.bbox)
        self._selection_key = self._selection_view_key()
        if self.highlight is not None and self.highlight.get_visible():
            self.ax.draw_artist(self.highlight)

    def _begin_interaction(self):
        self.interacting = True
        for artist in getattr(self, "base_artists", []):
            if hasattr(artist, "set_interacting"):
                artist.set_interacting(True)
        if self.interaction_job is not None:
            self.root.after_cancel(self.interaction_job)
        self.interaction_job = self.root.after(160, self._finish_interaction)

    def _finish_interaction(self):
        if self.interaction_job is not None:
            self.root.after_cancel(self.interaction_job)
            self.interaction_job = None
        if not self.interacting:
            return
        self.interacting = False
        for artist in getattr(self, "base_artists", []):
            if hasattr(artist, "set_interacting"):
                artist.set_interacting(False)
        self.canvas.draw_idle()

    def _style_axes(self):
        self.ax.set_facecolor("#101a26")
        self.ax.set_aspect("equal", adjustable="box")
        self.ax.set_axis_on()
        self.ax.grid(True, color="#8295a8", alpha=.12)
        self.ax.tick_params(colors="#94a6b8", labelsize=8)
        self.ax.ticklabel_format(useOffset=False, style="plain")
        self.ax.set_xlabel("东 E / CAD X", color="#94a6b8", fontsize=9)
        self.ax.set_ylabel("北 N / CAD Y", color="#94a6b8", fontsize=9)
        for spine in self.ax.spines.values():
            spine.set_edgecolor("#344458")

    def _blank_preview(self):
        self._style_axes()
        self.empty_text = self.ax.text(.5, .5, "打开 CAD 底图 + 坐标文件\n\n在同一张图上检查点位与标注\n\n点击右上方“示例图”体验", transform=self.ax.transAxes,
                                      ha="center", va="center", color="#9dadbd", fontsize=14, linespacing=1.7)
        self.canvas.draw_idle()

    def _render_base(self):
        self._finish_interaction()
        self._invalidate_selection()
        self.ax.clear()
        self._connect_axes()
        self.artists, self.label_artists = [], []
        self.highlight = None
        self.empty_text = None
        self.base_artists = []
        self.base_bounds = None
        if self.base_doc is not None and self.base_preview is not None:
            from .preview import render_prepared
            self.base_artists = render_prepared(self.base_preview, self.ax)
            self.base_bounds = self.base_preview.bounds
            for artist in self.base_artists:
                artist.set_visible(self.show_base.get())
        self._style_axes()
        self.canvas.draw_idle()

    def clear_base(self):
        """Discard a drawing when replacing the project, not when hiding its view."""
        self._invalidate_picker_resume("底图或项目已改变，请使用 CAD 取点开始新的工作图。")
        self.drawing_revision += 1
        self.loading_drawing = False
        self.pending_coordinates = None
        self.base_doc = self.base_path = None
        self.base_preview = None
        self.base_warnings = []
        self.show_base.set(False)
        self._sync_base_controls()
        self.dirty = True
        self._render_base()
        self.schedule_preview(fit=True)

    def _sync_base_controls(self):
        loaded = self.base_doc is not None
        if not loaded:
            self.show_base.set(False)
        visible = loaded and self.show_base.get()
        self.base_toggle_text.set("正在打开…" if self.loading_drawing else "关闭底图" if visible else "打开底图")
        self.base_toggle_button.configure(state="disabled" if self.loading_drawing else "normal")
        self.base_visibility_check.configure(state="normal" if loaded and not self.loading_drawing else "disabled")
        if loaded:
            quality = {"simplified_dwg": "简图预览 · ", "native_autocad": "AutoCAD · "}.get(getattr(self.base_doc, "_coordtool_quality", ""), "")
            name = self.base_path.name if self.base_path else "底图"
            self.drawing_status.set(f"{name}  ·  {quality}{len(self.base_doc.modelspace())} 个实体" + (" · 已关闭显示" if not visible else ""))
        else:
            self.drawing_status.set("未打开底图")

    def toggle_base_visibility(self):
        if self.loading_drawing:
            return
        if self.base_doc is None:
            self.open_base()
            return
        self.show_base.set(not self.show_base.get())
        self.toggle_base()

    def toggle_base(self):
        """Show/hide existing drawing artists while retaining the document and view."""
        self._sync_base_controls()
        self._finish_interaction()
        self._invalidate_selection()
        for artist in getattr(self, "base_artists", []):
            artist.set_visible(self.show_base.get())
        if self.base_doc is not None:
            self.status.set("底图已恢复显示。" if self.show_base.get() else "底图已关闭显示，点位保留；点击“打开底图”恢复。")
        self.canvas.draw_idle()

    def toggle_labels(self):
        for artist in self.label_artists:
            artist.set_visible(self.show_labels.get())
        self.canvas.draw_idle()

    def schedule_preview(self, fit=False):
        self.preview_revision += 1
        self.pending_fit = getattr(self, "pending_fit", False) or fit
        if self.preview_job:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(180, self._preview)

    def _preview(self):
        self.preview_job = None
        try:
            settings = self.settings()
        except ValueError as exc:
            self.status.set(str(exc) + "；当前保留上一次有效预览。")
            return
        revision = self.preview_revision
        points = self.points[:]
        geometry = (tuple(points), settings.radius, settings.leader, settings.text_height, settings.offset_e, settings.offset_n, settings.scale, settings.closed)
        def done(labels, error):
            if revision != self.preview_revision:
                return
            if error:
                self.error(error)
                return
            self.geometry_cache = (geometry, labels)
            self.labels = labels
            do_fit, self.pending_fit = self.pending_fit, False
            self._draw_overlay(settings, do_fit)
        if self.geometry_cache and self.geometry_cache[0] == geometry:
            done(self.geometry_cache[1], None)
        else:
            self.status.set(f"正在计算 {len(points)} 个点的标注…")
            self._submit(lambda: calc_labels(points, settings), done)

    def _draw_overlay(self, settings, do_fit=False):
        self._invalidate_selection()
        if getattr(self, "empty_text", None):
            self.empty_text.remove()
            self.empty_text = None
        for artist in self.artists:
            artist.remove()
        self.artists, self.label_artists = [], []
        if self.highlight is not None:
            self.highlight.remove()
            self.highlight = None
        color = COLORS[self.color.get()][1]
        r = settings.radius*settings.scale
        segments = []
        circles = []
        for label in self.labels:
            x, y = label.x, label.y
            if label.arrow is None:
                circles.append(Circle((x, y), r))
            segments.extend(annotation_segments(label, settings))
        self._label_positions = np.asarray([(label.x, label.y) for label in self.labels], dtype=float).reshape((-1, 2))
        if settings.draw_line and len(self.labels) > 1:
            for group in connection_groups(self.points, self.labels):
                xy = [(l.x, l.y) for l in group]
                segments.extend([[xy[i-1], xy[i]] for i in range(1, len(xy))])
                if settings.closed and len(xy) >= 3:
                    segments.append([xy[-1], xy[0]])
        for collection in (LineCollection(segments, colors=color, linewidths=max(.6, settings.lineweight/100*2.835), zorder=5),
                           PatchCollection(circles, facecolors="none", edgecolors=color, linewidths=max(.6, settings.lineweight/100*2.835), zorder=6)):
            self.ax.add_collection(collection)
            self.artists.append(collection)
        # Large drawings retain all geometry; label text is explicitly limited for navigation responsiveness.
        for label in self.labels[:1000]:
            for value, x, y, height, align in annotation_texts(label, settings):
                text = self.ax.text(x, y, value, color=color, ha=align, va="baseline" if label.arrow is not None else "bottom", fontsize=9, zorder=7, clip_on=True)
                text._coord_height = height
                text.set_visible(self.show_labels.get())
                self.artists.append(text)
                self.label_artists.append(text)
        self.render_settings = settings
        self._style_axes()
        if do_fit:
            self.fit("drawing" if getattr(self, "pending_drawing_fit", False) and self.base_bounds else "all")
        self.pending_drawing_fit = False
        self._update_text_sizes()
        self.select_point()
        warning = " · 预览仅显示前1000个点名，导出包含全部" if len(self.labels) > 1000 else ""
        if self.base_bounds and self.labels:
            x0, y0, x1, y1 = self.base_bounds
            if not any(x0 <= label.x <= x1 and y0 <= label.y <= y1 for label in self.labels):
                warning += " · 坐标点在底图范围外，请核对是否仍为示例坐标或坐标系不同"
        self.status.set(f"{len(self.points)} 个点 · {self.unit.get()} · 滚轮缩放 / 中键平移 / 单击选点{warning}")
        self.canvas.draw_idle()

    def _point_bounds(self):
        if not self.labels:
            return None
        s = self.render_settings if hasattr(self, "render_settings") else self.settings()
        r, th = s.radius*s.scale, s.text_height*s.scale
        xs, ys = [], []
        for label in self.labels:
            xs.extend((label.x-r, label.x+r, label.lx, label.hx))
            ys.extend((label.y-r, label.y+r, label.ly, label.ly+th*1.4))
            for segment in annotation_segments(label, s):
                for x, y in segment:
                    xs.append(x)
                    ys.append(y)
            for _, x, y, height, _ in annotation_texts(label, s):
                xs.append(x)
                ys.extend((y-height*.3, y+height*1.4))
        return min(xs), min(ys), max(xs), max(ys)

    def fit(self, what="all"):
        self._finish_interaction()
        bounds = []
        pb = self._point_bounds()
        if pb and what != "drawing":
            bounds.append(pb)
        if what in ("all", "drawing") and self.base_bounds and self.show_base.get():
            bounds.append(self.base_bounds)
        if not bounds:
            return
        x0, y0 = min(b[0] for b in bounds), min(b[1] for b in bounds)
        x1, y1 = max(b[2] for b in bounds), max(b[3] for b in bounds)
        span = max(x1-x0, y1-y0, 1.)
        pad = span*.08
        self.ax.set_xlim(x0-pad, x1+pad)
        self.ax.set_ylim(y0-pad, y1+pad)
        self.toolbar.update()
        self.canvas.draw_idle()

    def _update_text_sizes(self, *_):
        if not hasattr(self, "render_settings"):
            return
        height = self.render_settings.text_height*self.render_settings.scale
        y0, y1 = self.ax.get_ylim()
        fontsize = max(.8, min(200., height/max(abs(y1-y0), 1e-12)*self.ax.bbox.height*72/self.fig.dpi))
        for text in self.label_artists:
            actual = max(.8, min(200., getattr(text, "_coord_height", height)/max(abs(y1-y0), 1e-12)*self.ax.bbox.height*72/self.fig.dpi))
            if abs(text.get_fontsize()-actual) > .01:
                text.set_fontsize(actual)

    def zoom(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        self._begin_interaction()
        factor = .8 if event.button == "up" else 1.25
        x, y = event.xdata, event.ydata
        self.ax.set_xlim([x+(v-x)*factor for v in self.ax.get_xlim()])
        self.ax.set_ylim([y+(v-y)*factor for v in self.ax.get_ylim()])
        self.canvas.draw_idle()

    def mouse_down(self, event):
        if event.inaxes != self.ax:
            return
        if event.button in (1, 3) and self.toolbar.mode:
            self.toolbar_drag = True
            self._begin_interaction()
        if event.button == 2:
            self._begin_interaction()
            self.pan_start = (event.x, event.y, self.ax.get_xlim(), self.ax.get_ylim())
        elif event.button == 1 and not self.toolbar.mode and self.labels:
            projected = self.ax.transData.transform(self._label_positions)
            distances = np.sum((projected - (event.x, event.y))**2, axis=1)
            idx = int(np.argmin(distances))
            distance = distances[idx]
            if distance <= 18**2 and self.tree.exists(str(idx)):
                self.tree.selection_set(str(idx))
                self.tree.see(str(idx))

    def mouse_up(self, event):
        self.pan_start = None
        self.toolbar_drag = False
        self._finish_interaction()

    def mouse_move(self, event):
        if self.toolbar_drag:
            self._begin_interaction()
        now = monotonic()
        if event.inaxes == self.ax and event.xdata is not None and now-self._cursor_updated >= .04:
            self._cursor_updated = now
            self.cursor_text.set(f"CAD X {event.xdata:.4f}   Y {event.ydata:.4f}    |    X = 东 E，Y = 北 N")
        if self.pan_start and event.x is not None:
            self._begin_interaction()
            x, y, xlim, ylim = self.pan_start
            dx = (event.x-x)/self.ax.bbox.width*(xlim[1]-xlim[0])
            dy = (event.y-y)/self.ax.bbox.height*(ylim[1]-ylim[0])
            self.ax.set_xlim(xlim[0]-dx, xlim[1]-dx)
            self.ax.set_ylim(ylim[0]-dy, ylim[1]-dy)
            self.canvas.draw_idle()

    def select_point(self, _=None):
        selected = self.tree.selection()
        if selected and int(selected[0]) < len(self.labels):
            label = self.labels[int(selected[0])]
            if self.highlight is None:
                self.highlight, = self.ax.plot([], [], "o", markerfacecolor="none", markeredgecolor="#ffffff", markersize=16, markeredgewidth=1.8, zorder=10, animated=True)
            self.highlight.set_data([label.x], [label.y])
            self.highlight.set_visible(True)
        elif self.highlight is not None:
            self.highlight.set_visible(False)
        if self._selection_background is not None and self._selection_key == self._selection_view_key():
            self.canvas.restore_region(self._selection_background)
            if self.highlight is not None and self.highlight.get_visible():
                self.ax.draw_artist(self.highlight)
            self.canvas.blit(self.ax.bbox)
        else:
            self.canvas.draw_idle()

    def _save_path(self, title, suffix, name):
        return filedialog.asksaveasfilename(parent=self.root, title=title, initialfile=name, defaultextension=suffix, filetypes=[(suffix.upper()[1:]+" 文件", "*"+suffix)])

    def _ready_export(self):
        if not self.points:
            raise ValueError("请先导入坐标点。")
        settings = self.settings()
        if self.base_doc is not None:
            expected = {6: 1., 4: 1000., 5: 100.}.get(self.base_doc.header.get("$INSUNITS", 0))
            if expected and settings.scale != expected:
                raise ValueError("当前图纸单位与底图声明不一致，请在标注设置中核对米/毫米/厘米。")
        return settings

    def export_points(self):
        try:
            if not self.points:
                raise ValueError("没有可导出的坐标点。")
            path = self._save_path("导出原始坐标（米）", ".csv", "坐标导出.csv")
            if path:
                write_points(path, self.points)
                self.status.set(f"已导出原始坐标：{path}")
        except Exception as exc:
            self.error(exc)

    def export_dxf(self, open_after=False):
        try:
            settings = self._ready_export()
            path = self._save_path("另存带标注图纸", ".dxf", "坐标标注图.dxf")
            if not path:
                return
            if self.base_path and Path(path).resolve() == self.base_path:
                raise ValueError("请选择新的文件名，保留原始底图。")
            points, doc = self.points[:], self.base_doc
            self.status.set("正在生成带标注图纸…")
            def done(result, error):
                if error:
                    self.error(error)
                    return
                self.status.set(f"已保存 {len(points)} 个标注：{path}")
                if open_after:
                    try:
                        from .launcher import open_in_cad
                        method = open_in_cad(path)
                        self.status.set(f"图纸已保存并交给 {method} 打开：{path}")
                    except OSError as exc:
                        self.error(ValueError(f"图纸已保存，但未能打开默认CAD程序。请在CAD中手动打开：\n{path}\n{exc}"))
            self._submit(lambda: cad.export_dxf(path, points, settings, base_doc=doc), done)
        except Exception as exc:
            self.error(exc)

    def open_cad(self):
        if self._picker_guard():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="选择要在 CAD 中打开的图纸",
            initialdir=str(Path(self.base_path).parent if self.base_path else Path.home()/"Desktop"),
            filetypes=[("CAD 图纸", "*.dwg *.dxf"), ("DWG 图纸", "*.dwg"), ("DXF 图纸", "*.dxf")])
        if not path:
            return
        from .launcher import open_in_cad
        self.status.set(f"正在打开 CAD 图纸：{Path(path).name}…")
        def done(method, error):
            if error:
                self.error(error)
            else:
                self.status.set(f"已将图纸交给 {method} 打开：{path}")
        self._submit(lambda: open_in_cad(path), done)

    def export_lsp(self):
        try:
            settings = self._ready_export()
            path = self._save_path("保存标注脚本", ".lsp", "坐标标注.lsp")
            if path:
                points = self.points[:]
                def done(result, error):
                    if error:
                        self.error(error)
                    else:
                        Path(path).write_text(result, encoding="utf-8")
                        self.status.set(f"已生成：{path} · CAD中APPLOAD加载后输入BINDLINE")
                self._submit(lambda: cad.generate_lsp(points, settings), done)
        except Exception as exc:
            self.error(exc)

    def export_reverse(self):
        try:
            settings = self.settings()
            path = self._save_path("保存反推坐标脚本", ".lsp", "反推坐标.lsp")
            if path:
                Path(path).write_text(cad.generate_reverse_lsp(settings.layer), encoding="utf-8")
                self.status.set(f"已生成：{path} · CAD中APPLOAD加载后输入EXPORTCOORD")
        except Exception as exc:
            self.error(exc)

    def _confirm_pending_changes(self, action):
        """Resolve unsaved edits before replacing a whole project, even if empty."""
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            action, "当前项目有未保存更改，是否先保存？\n选择“否”会放弃当前修改，选择“取消”继续编辑。", parent=self.root)
        if answer is None:
            return False
        if answer:
            self.save_project()
            return not self.dirty
        return True

    def save_project(self):
        try:
            settings = self.settings()
            path = self._save_path("保存坐标项目", ".json", "坐标项目.json")
            if not path:
                return
            payload = {"format": "coordtool-project", "version": 1, "points": [asdict(p) for p in self.points],
                       "settings": asdict(settings), "drawing": str(self.base_path) if self.base_path else None}
            from .project import atomic_json
            atomic_json(path, payload)
            self.project_path = Path(path)
            self.dirty = False
            self.status.set(f"项目已保存：{path}")
        except Exception as exc:
            self.error(exc)

    def open_project(self, path=None, *, recover_picker=False):
        if self._picker_guard():
            return
        if not path:
            path = filedialog.askopenfilename(parent=self.root, title="打开坐标项目", filetypes=[("坐标项目", "*.json")])
        if not path:
            return
        try:
            notices = []
            if recover_picker:
                from .picker import recover_picker_snapshot
                points, settings, drawing = recover_picker_snapshot(path, warnings=notices)
            else:
                from .project import load_project
                points, settings, drawing = load_project(path)
            if not self._confirm_pending_changes("恢复取点记录" if recover_picker else "打开项目"):
                return
            self._load_project_data(points, settings, drawing,
                                    project_path=None if recover_picker else Path(path), dirty=recover_picker)
            if recover_picker:
                self.status.set(f"已恢复 {len(points)} 个坐标点，请保存项目到自己的工作目录。")
                if notices:
                    messagebox.showwarning("取点恢复提示", "\n".join(notices), parent=self.root)
        except Exception as exc:
            self.error(exc)

    def _load_project_data(self, points, settings, drawing, *, project_path=None, dirty=False):
        self._invalidate_picker_resume("已打开另一个项目，请使用 CAD 取点开始新的工作图。")
        self.file_revision += 1
        self.clear_base()
        self._set_settings(settings)
        self._changed_points(points, "打开项目", record=False)
        # Point-only undo must not cross a project boundary: the old drawing,
        # units and offsets belong to that old project too.
        self.undo.clear()
        self.import_text.set(f"已恢复项目：{len(points)} 个点" + ("，含高程" if any(p.z is not None for p in points) else ""))
        self.project_path = project_path
        if drawing:
            if Path(drawing).is_file():
                self.load_base(drawing, adopt_units=False, mark_dirty=False)
            else:
                messagebox.showwarning("底图未找到", f"坐标和设置已载入，请重新选择底图：\n{drawing}", parent=self.root)
        self.dirty = dirty

    def _set_settings(self, settings):
        for attr in ("radius", "leader", "text_height", "layer", "offset_e", "offset_n", "draw_line"):
            getattr(self, attr).set(getattr(settings, attr))
        self.closed_line.set(settings.closed)
        self.color.set(next(k for k, v in COLORS.items() if v[0] == settings.color))
        self.unit.set(next(k for k, v in UNITS.items() if v == settings.scale))
        self.lineweight.set(f"{settings.lineweight/100:.2f}")

    def load_demo(self):
        if self._picker_guard():
            return
        try:
            points = read_points(self.resources/"examples"/"sample_points.csv", order="EN").points
            if not self._confirm_pending_changes("打开示例图"):
                return
            self.order.set(next(iter(ORDERS)))
            self._load_project_data(points, Settings(radius=1.2, leader=9, text_height=1.6),
                                    self.resources/"examples"/"sample_base.dxf", dirty=True)
        except Exception as exc:
            self.error(exc)

    def close(self):
        self._cancel_picker_discovery()
        if self._picker_active():
            if not self.picker_stop_requested:
                self.stop_picker()
            if self.picker_stop_requested:
                self.status.set("已请求结束全部取点，请在 CAD 按 Enter / Esc 响应，待最后一批坐标回传后再关闭。")
            else:
                self.status.set("请在 CAD 输入 Q 结束全部取点，待最后一批坐标回传后再关闭。")
            return
        if not self._confirm_pending_changes("关闭程序"):
            return
        self._invalidate_picker_resume("软件已关闭，CAD ZB 续接不可用。")
        self.closed = True
        if self.interaction_job is not None:
            self.root.after_cancel(self.interaction_job)
            self.interaction_job = None
        self.canvas.dispose()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
