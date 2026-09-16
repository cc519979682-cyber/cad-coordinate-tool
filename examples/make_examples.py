"""Generate and verify fictional coordinate/CAD examples. No project data is used."""
from __future__ import annotations

import csv
from pathlib import Path

import ezdxf


HERE = Path(__file__).resolve().parent
POINTS = [
    ("P01", 5015.0, 10020.0),
    ("P02", 5055.0, 10020.0),
    ("P03", 5095.0, 10020.0),
    ("P04", 5105.0, 10030.0),
    ("P05", 5105.0, 10070.0),
    ("P06", 5095.0, 10080.0),
    ("P07", 5055.0, 10080.0),
    ("P08", 5015.0, 10080.0),
    ("P09", 5005.0, 10070.0),
    ("P10", 5005.0, 10030.0),
]


def build() -> None:
    doc = ezdxf.new("R2013", setup=True)
    doc.units = 6  # INSUNITS: meters
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    doc.styles.new("CN", dxfattribs={"font": "simhei.ttf"})
    layer_specs = [
        ("BASE_FRAME", 8, 13),
        ("BASE_ROAD", 253, 25),
        ("BASE_AXIS", 8, 13),
        ("BASE_BOUNDARY", 252, 25),
        ("BASE_BUILDING", 4, 25),
        ("BASE_LANDSCAPE", 3, 13),
        ("BASE_SERVICE", 4, 18),
        ("BASE_SURVEY", 2, 18),
        ("BASE_TEXT", 7, 13),
    ]
    for name, color, lineweight in layer_specs:
        doc.layers.new(name, dxfattribs={"color": color, "lineweight": lineweight})
    if "DASHED" in doc.linetypes:
        doc.layers.get("BASE_AXIS").dxf.linetype = "DASHED"
    msp = doc.modelspace()

    def text(value: str, point: tuple[float, float], height: float = 1.7,
             layer: str = "BASE_TEXT") -> None:
        entity = msp.add_text(value, dxfattribs={"height": height, "style": "CN", "layer": layer})
        entity.dxf.insert = point

    def line(a: tuple[float, float], b: tuple[float, float], layer: str) -> None:
        msp.add_line(a, b, dxfattribs={"layer": layer})

    # The frame and every physical base entity stay inside the documented extent.
    msp.add_lwpolyline([(5000, 10000), (5120, 10000), (5120, 10100), (5000, 10100)],
                       close=True, dxfattribs={"layer": "BASE_FRAME"})
    text("简易工程底图 · 虚构示例", (5004, 10095), 2.6)
    text("CAD X = 东 E   /   CAD Y = 北 N   /   单位：米", (5004, 10091), 1.5)
    text("地块边界 10 点 · EN / NE 文件为同一组坐标", (5004, 10086), 1.5)

    # East-west demonstration road; center and edges are distinct line types.
    for y in (10005.0, 10015.0):
        line((5000, y), (5120, y), "BASE_ROAD")
    line((5000, 10010), (5120, 10010), "BASE_AXIS")
    text("示例道路  /  宽 10 m", (5050, 10007), 1.5)

    boundary = [(e, n) for _, e, n in POINTS]
    msp.add_lwpolyline(boundary, close=True, dxfattribs={"layer": "BASE_BOUNDARY"})
    # The survey reference block is centered at local 0,0 and inserted at each point.
    survey = doc.blocks.new("BASE_POINT_MARK")
    survey.add_circle((0, 0), 0.45, dxfattribs={"layer": "0"})
    survey.add_line((-0.8, 0), (0.8, 0), dxfattribs={"layer": "0"})
    survey.add_line((0, -0.8), (0, 0.8), dxfattribs={"layer": "0"})
    for _, e, n in POINTS:
        msp.add_blockref("BASE_POINT_MARK", (e, n), dxfattribs={"layer": "BASE_SURVEY"})

    # Simple building with two rooms and a door-swing arc.
    msp.add_lwpolyline([(5020, 10032), (5050, 10032), (5050, 10054), (5020, 10054)],
                       close=True, dxfattribs={"layer": "BASE_BUILDING"})
    line((5035, 10032), (5035, 10054), "BASE_BUILDING")
    line((5020, 10043), (5035, 10043), "BASE_BUILDING")
    line((5040, 10032), (5040, 10036), "BASE_BUILDING")
    msp.add_arc((5040, 10032), 4, 0, 90, dxfattribs={"layer": "BASE_BUILDING"})
    text("示例建筑", (5025, 10047), 2)
    text("30 × 22 m", (5039, 10039), 1.3)

    # Service circle and semicircular path make curved CAD geometry visible.
    msp.add_circle((5080, 10043), 7, dxfattribs={"layer": "BASE_SERVICE"})
    line((5071, 10043), (5089, 10043), "BASE_AXIS")
    line((5080, 10034), (5080, 10052), "BASE_AXIS")
    text("设施区", (5076, 10042), 1.6)
    text("R = 7 m", (5076, 10038), 1.3)
    msp.add_arc((5080, 10062), 11, 0, 180, dxfattribs={"layer": "BASE_LANDSCAPE"})
    msp.add_arc((5080, 10062), 8, 0, 180, dxfattribs={"layer": "BASE_LANDSCAPE"})
    line((5069, 10062), (5072, 10062), "BASE_LANDSCAPE")
    line((5088, 10062), (5091, 10062), "BASE_LANDSCAPE")
    text("弧形步道", (5075, 10061), 1.5)

    tree = doc.blocks.new("BASE_TREE")
    for cx, cy in ((-0.8, 0), (0.8, 0), (0, 1)):
        tree.add_circle((cx, cy), 1.6, dxfattribs={"layer": "0"})
    tree.add_line((-0.5, 0), (0.5, 0), dxfattribs={"layer": "0"})
    tree.add_line((0, -0.5), (0, 0.5), dxfattribs={"layer": "0"})
    for x, y, rotation in ((5014, 10060, 0), (5026, 10069, 25), (5040, 10069, 60), (5055, 10069, 90)):
        msp.add_blockref("BASE_TREE", (x, y), dxfattribs={"layer": "BASE_LANDSCAPE", "rotation": rotation})

    # North arrow and scale provide intuitive orientation and a length check.
    line((5113, 10084), (5113, 10094), "BASE_TEXT")
    msp.add_lwpolyline([(5111.5, 10091), (5113, 10094), (5114.5, 10091)],
                       dxfattribs={"layer": "BASE_TEXT"})
    text("N", (5112, 10096), 2)
    line((5007, 10003), (5027, 10003), "BASE_TEXT")
    for x in (5007, 5017, 5027):
        line((x, 10002.6), (x, 10003.4), "BASE_TEXT")
    text("0", (5006.7, 10000.8), 1.1)
    text("10", (5016.4, 10000.8), 1.1)
    text("20 m", (5026.0, 10000.8), 1.1)

    doc.set_modelspace_vport(height=125, center=(5060, 10050))
    doc.saveas(HERE / "sample_base.dxf")
    with (HERE / "sample_points.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["点名", "东E", "北N"])
        writer.writerows((name, f"{e:.3f}", f"{n:.3f}") for name, e, n in POINTS)
    with (HERE / "sample_points_NE.txt").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["点名", "北N", "东E"])
        writer.writerows((name, f"{n:.3f}", f"{e:.3f}") for name, e, n in POINTS)
    verify()


def verify() -> None:
    doc = ezdxf.readfile(HERE / "sample_base.dxf")
    auditor = doc.audit()
    assert not auditor.errors, auditor.errors
    assert doc.units == 6
    msp = doc.modelspace()
    markers = msp.query('INSERT[name=="BASE_POINT_MARK"]')
    positions = {(round(ref.dxf.insert.x, 6), round(ref.dxf.insert.y, 6)) for ref in markers}
    expected = {(e, n) for _, e, n in POINTS}
    assert len(markers) == len(POINTS) == 10
    assert positions == expected
    outline = list(msp.query('LWPOLYLINE[layer=="BASE_BOUNDARY"]'))
    assert len(outline) == 1 and outline[0].closed
    assert {(float(x), float(y)) for x, y in outline[0].get_points("xy")} == expected
    for filename, delimiter, reverse in (("sample_points.csv", ",", False), ("sample_points_NE.txt", "\t", True)):
        with (HERE / filename).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, delimiter=delimiter))[1:]
        actual = [(name, float(b) if reverse else float(a), float(a) if reverse else float(b))
                  for name, a, b in rows]
        assert actual == POINTS, filename
    assert len(msp.query("ARC")) >= 3
    assert len(msp.query('INSERT[name=="BASE_TREE"]')) == 4
    print(f"Verified: {len(POINTS)} fictional points; EN = NE = DXF survey inserts = boundary vertices; units = meters.")


if __name__ == "__main__":
    build()
