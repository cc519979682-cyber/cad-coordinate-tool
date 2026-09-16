"""Render the public fictional demo; this is not a desktop screenshot.

Run from the repository root: python scripts/make_preview.py
Uses the same DXF renderer and annotation geometry as the desktop application.
No CAD process is opened and no project data is read.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyBboxPatch

from coordtool.cad import load_drawing
from coordtool.core import Settings, annotation_segments, annotation_texts, calc_labels, read_points
from coordtool.preview import prepare_preview, render_prepared


def main() -> None:
    candidates = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC")
    available = {font.name for font in font_manager.fontManager.ttflist}
    selected = next((name for name in candidates if name in available), None)
    if selected is None:
        raise RuntimeError("Install a Chinese font (Microsoft YaHei, SimHei, or Noto Sans CJK SC).")
    matplotlib.rcParams.update({"font.family": selected, "axes.unicode_minus": False})

    points = read_points(ROOT / "examples" / "sample_points.csv", order="auto").points
    document, warnings = load_drawing(ROOT / "examples" / "sample_base.dxf")
    bundle = prepare_preview(document, background="#101a26")
    settings = Settings(radius=0.75, leader=3.5, text_height=1.5)
    labels = calc_labels(points, settings)

    fig = Figure(figsize=(15.6, 9.6), dpi=150, facecolor="#0b131e")
    FigureCanvasAgg(fig)
    fig.text(0.045, 0.93, "坐标生成器", color="#f2f7fc", fontsize=26, weight="bold")
    fig.text(0.227, 0.934, "CAD 底图 × 坐标标注", color="#5fc4ff", fontsize=15)
    fig.text(0.045, 0.88, "虚构图纸预览  ·  由实际底图渲染与标注代码生成", color="#a4b8cc", fontsize=12)

    panel = FancyBboxPatch((0.035, 0.13), 0.253, 0.694,
                          boxstyle="round,pad=0.006,rounding_size=0.009",
                          facecolor="#132132", edgecolor="#24374b", linewidth=1,
                          transform=fig.transFigure, zorder=0)
    fig.add_artist(panel)
    fig.text(0.051, 0.786, "坐标点", color="#eef6ff", fontsize=17, weight="bold")
    fig.text(0.051, 0.753, "10 点  ·  单位：米  ·  E / N", color="#7cbfef", fontsize=11)
    table_ax = fig.add_axes((0.047, 0.31, 0.23, 0.415))
    table_ax.set_axis_off()
    rows = [[p.name, f"{p.e:.3f}", f"{p.n:.3f}"] for p in points]
    table = table_ax.table(cellText=rows, colLabels=["点名", "东 E", "北 N"],
                           colWidths=[0.2, 0.4, 0.4], cellLoc="right", bbox=(0, 0, 1, 1))
    table.auto_set_font_size(False)
    table.set_fontsize(10.5)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#24374b")
        cell.set_linewidth(0.45)
        cell.set_facecolor("#20354b" if row == 0 else ("#17283a" if row % 2 else "#132132"))
        cell.get_text().set_color("#9bd7ff" if row == 0 else "#e0eaf5")
        if col == 0:
            cell.get_text().set_ha("center")
    fig.text(0.052, 0.254, "坐标导入 → 底图核对", color="#e0eaf5", fontsize=12)
    fig.text(0.052, 0.216, "点位标注 → 导出 DXF", color="#e0eaf5", fontsize=12)
    fig.text(0.052, 0.163, "图纸与坐标均来自公开虚构示例", color="#88a0b8", fontsize=10)

    ax = fig.add_axes((0.337, 0.185, 0.627, 0.64))
    render_prepared(bundle, ax, interactive_cache=False)
    color = "#ff6d79"
    segments = [segment for label in labels for segment in annotation_segments(label, settings)]
    ax.add_collection(LineCollection(segments, colors=color, linewidths=1.25, zorder=3))
    for label in labels:
        ax.add_patch(Circle((label.x, label.y), settings.radius, fill=False,
                            edgecolor=color, linewidth=1.3, zorder=4))
        for text, x, y, _height, align in annotation_texts(label, settings):
            ax.text(x, y, text, color=color, fontsize=9.5, ha=align, va="bottom", zorder=5)
    ax.set_xlim(4988, 5128)
    ax.set_ylim(9995, 10107)
    ax.set_aspect("equal", adjustable="box")
    ax.tick_params(colors="#8ca4bb", labelsize=9, length=3)
    ax.ticklabel_format(axis="both", useOffset=False, style="plain")
    ax.set_xlabel("东 E / CAD X", color="#a8bbcf", fontsize=11, labelpad=10)
    ax.set_ylabel("北 N / CAD Y", color="#a8bbcf", fontsize=11, labelpad=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#2c4157")
    ax.grid(color="#253748", linewidth=0.4, alpha=0.65, zorder=0)
    fig.text(0.355, 0.835, "sample_base.dxf", color="#b4c9dc", fontsize=11)
    fig.text(0.895, 0.835, "●  原始底图", color="#c6d4e1", fontsize=10, ha="right")
    fig.text(0.964, 0.835, "●  坐标标注", color=color, fontsize=10, ha="right")
    fig.text(0.045, 0.061, "示意预览，非实机窗口截图。所有坐标和图形均为虚构，不含真实工程资料。",
             color="#92a9bf", fontsize=11)
    target = ROOT / "docs" / "demo-preview.png"
    fig.savefig(target, dpi=150, facecolor=fig.get_facecolor(),
                metadata={"Title": "坐标生成器：虚构图纸预览", "Software": "Matplotlib / coordtool"})
    print(f"Rendered fictional preview: {target.name}; {len(points)} points; {len(document.modelspace())} base entities")
    for warning in [*warnings, *bundle.warnings]:
        print(warning)


if __name__ == "__main__":
    main()
