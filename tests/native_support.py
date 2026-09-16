"""Optional AutoCAD tests use generated fixtures and an explicitly isolated Core."""
import os
from pathlib import Path
import shutil

import ezdxf

from coordtool.native_dwg import find_core_console as find_registered_console


def find_core_console():
    """Allow an installation override, otherwise use the registered AutoCAD."""
    override = os.environ.get("COORDTOOL_CORE_CONSOLE")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise ValueError("COORDTOOL_CORE_CONSOLE must name an existing accoreconsole executable")
        return candidate.resolve()
    registered = find_registered_console()
    if registered:
        return registered
    executable = shutil.which("accoreconsole.exe")
    return Path(executable).resolve() if executable else None


def synthetic_seed(directory):
    """Create a tiny model drawing without any project or user-supplied data."""
    path = Path(directory) / "synthetic_seed.dxf"
    drawing = ezdxf.new("R2018", units=6)
    drawing.modelspace().add_line((1, 2, 0), (3, 4, 0))
    drawing.saveas(path)
    return path
