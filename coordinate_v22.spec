# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 one-folder build. Run from the project folder with PyInstaller."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


ROOT = Path(SPECPATH).resolve()
APP_NAME = "坐标生成器v22.8"
generated = ROOT / "build" / "generated"
generated.mkdir(parents=True, exist_ok=True)
runtime_hook = generated / "coordinate_runtime.py"
runtime_hook.write_text(
    '''"""Frozen runtime: a predictable backend and writable diagnostics."""
import datetime
import os
from pathlib import Path
import sys

os.environ["MPLBACKEND"] = "TkAgg"
try:
    _coord_user = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CoordinateGeneratorV22"
    _coord_user.mkdir(parents=True, exist_ok=True)
    _coord_logs = _coord_user / "logs"
    _coord_logs.mkdir(exist_ok=True)
    _coord_logfile = _coord_logs / "startup.log"
    # Keep a single previous log when repeated launches have grown this file.
    if _coord_logfile.exists() and _coord_logfile.stat().st_size > 2_000_000:
        _coord_logfile.replace(_coord_logs / "startup.previous.log")
    _coord_stream = _coord_logfile.open("a", encoding="utf-8", buffering=1)
    sys.stdout = _coord_stream
    sys.stderr = _coord_stream
    os.environ["MPLCONFIGDIR"] = str(_coord_user / "matplotlib")
    print("\\n[" + datetime.datetime.now().isoformat(timespec="seconds") + "] Coordinate v22.8 startup")
except OSError:
    pass
''',
    encoding="utf-8",
)

datas = [(str(ROOT / "examples" / filename), "examples") for filename in (
    "sample_base.dxf", "sample_points.csv", "sample_points_NE.txt",
)]
datas += [(str(ROOT / filename), ".") for filename in (
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
)]
# ttkbootstrap themes/icons are Python modules; collect its accompanying assets too.
datas += collect_data_files("ttkbootstrap")
datas += collect_data_files("tkinterdnd2", includes=["tkdnd/win-x64/**"])
datas += collect_data_files("ezdxf")
for distribution in ("ezdxf", "ezdwg", "ttkbootstrap", "tkinterdnd2"):
    datas += copy_metadata(distribution)

binaries = collect_dynamic_libs("ezdwg", search_patterns=["*.pyd", "*.dll"])
hiddenimports = [
    "coordtool.launcher",
    "coordtool.native_dwg", "coordtool.preview", "coordtool.interaction", "coordtool.preview_layer",
    "tkinterdnd2.TkinterDnD", "ttkbootstrap.themes.standard", "ttkbootstrap.themes.user",
    "ttkbootstrap.localization.msgs", "ttkbootstrap.icons",
    "ezdwg._core", "ezdwg.convert", "ezdxf.acc", "ezdxf.addons.odafc",
    "ezdxf.addons.drawing.matplotlib", "matplotlib.backends.backend_tkagg",
    "openpyxl", "xlrd",
]
# The application renders through TkAgg and does not use notebook, Qt, or ML stacks.
excluded = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gi",
    "IPython", "jupyter", "notebook", "pytest", "scipy", "pandas", "sympy",
    "torch", "tensorflow", "tkinter.test", "matplotlib.tests", "numpy.tests",
    "ezdxf.addons.drawing.qtviewer", "ezdxf.addons.drawing.pyqt",
]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["TkAgg"]}},
    runtime_hooks=[str(runtime_hook)],
    excludes=excluded,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
