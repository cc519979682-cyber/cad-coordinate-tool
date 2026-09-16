"""Collect notices from this interpreter's requirements closure for redistribution.

Run in the same clean virtual environment used by PyInstaller. This records
installed dependency versions, not every package on a developer's computer.
It does not install packages or decide whether a release meets every license.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
NOTICE_WORDS = ("license", "licence", "copying", "copyright", "notice")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def required_distributions(requirements: Path):
    """Resolve markers and selected extras using the actual build interpreter."""
    queue = []
    for line in requirements.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            queue.append(Requirement(line))
    found = {}
    processed = set()
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        dist = metadata.distribution(name)
        if requirement.specifier and not requirement.specifier.contains(dist.version):
            raise RuntimeError(f"Installed version does not match: {requirement}")
        found[name] = dist
        contexts = {"", *requirement.extras}
        for extra in contexts:
            key = (name, extra)
            if key in processed:
                continue
            processed.add(key)
            for value in dist.requires or ():
                child = Requirement(value)
                if child.marker is None or child.marker.evaluate({"extra": extra}):
                    # This edge has already been evaluated in the parent's context.
                    child.marker = None
                    queue.append(child)
    return found


def copy_notices(dist, output: Path):
    copied = []
    package = output / "python-packages" / safe_name(
        f"{dist.metadata['Name']}-{dist.version}"
    )
    for entry in sorted(dist.files or (), key=str):
        if not any(word in Path(str(entry)).name.lower() for word in NOTICE_WORDS):
            continue
        source = Path(dist.locate_file(entry))
        if not source.is_file() or source.suffix.lower() in {".py", ".pyc", ".pyd", ".dll", ".so"}:
            continue
        # Wheel RECORD paths can begin with ../. Never reproduce that traversal.
        components = [safe_name(part) for part in entry.parts if part not in {".", ".."}]
        target = package.joinpath(*components)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(output).as_posix())
    if not copied:
        raise RuntimeError(f"No full license/notice file in installed package: {dist.metadata['Name']}")
    value = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See notice files"
    # Some wheels put the full license (tens of kilobytes) in this metadata field.
    summary = value if "\n" not in value and len(value) <= 240 else "See notice files"
    return {
        "name": dist.metadata["Name"], "version": dist.version,
        "license_metadata": summary, "notice_files": copied,
    }


def collect_interpreter(output: Path):
    interpreter = output / "interpreter"
    interpreter.mkdir(parents=True, exist_ok=True)
    prefix = Path(sys.base_prefix)
    python_license = next((prefix / name for name in ("LICENSE.txt", "LICENSE", "license.txt") if (prefix / name).is_file()), None)
    if python_license is None:
        raise RuntimeError("Python distribution LICENSE was not found; provide a standard CPython installation.")
    shutil.copyfile(python_license, interpreter / "Python-LICENSE.txt")

    import tkinter
    tcl = tkinter.Tcl()
    tcl_version = str(tcl.call("info", "patchlevel"))
    tcl_library = Path(str(tcl.call("info", "library")))
    tcl_license = tcl_library / "license.terms"
    if not tcl_license.is_file():
        tcl_license = ROOT / "licenses" / "native" / f"tcl-{tcl_version}" / "license.terms"
    if not tcl_license.is_file():
        raise RuntimeError(f"Tcl {tcl_version} license is missing; add its upstream notice before publishing.")
    shutil.copyfile(tcl_license, interpreter / f"Tcl-{safe_name(tcl_version)}-license.terms")
    tk_library = tcl_library.parent / f"tk{tkinter.TkVersion}"
    tk_license = tk_library / "license.terms"
    if not tk_license.is_file():
        raise RuntimeError("Tk license.terms is missing; add the matching upstream notice before publishing.")
    shutil.copyfile(tk_license, interpreter / "Tk-license.terms")
    return {"python_version": sys.version.split()[0], "tcl_version": tcl_version,
            "tk_major_minor": str(tkinter.TkVersion)}


def collect_native(found, output: Path):
    """Copy pinned notices not present in the Python wheel's metadata."""
    native = ROOT / "licenses" / "native"
    names = []
    if "ezdwg" in found:
        version = found["ezdwg"].version
        snapshot = native / f"ezdwg-{version}"
        if not snapshot.is_dir():
            raise RuntimeError(f"ezdwg {version}: refresh Rust crate license snapshot before publishing.")
        shutil.copytree(snapshot, output / "native" / snapshot.name)
        names.append(snapshot.name)
    if "tkinterdnd2" in found:
        if found["tkinterdnd2"].version != "0.5.0":
            raise RuntimeError("tkinterdnd2 changed: verify bundled tkdnd version and refresh notices.")
        # This release's Windows x64 native library is libtkdnd2.9.5.dll.
        wheel_files = {str(p).replace("\\", "/") for p in found["tkinterdnd2"].files or ()}
        if not any(p.endswith("win-x64/libtkdnd2.9.5.dll") for p in wheel_files):
            raise RuntimeError("Expected Windows x64 tkdnd library was not found.")
        shutil.copytree(native / "tkdnd-2.9.5", output / "native" / "tkdnd-2.9.5")
        names.append("tkdnd-2.9.5")
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New or empty output directory")
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Output directory must be empty to prevent stale notices from earlier builds.")
    found = required_distributions(args.requirements)
    output.mkdir(parents=True, exist_ok=True)
    packages = [copy_notices(found[name], output) for name in sorted(found)]
    runtime = collect_interpreter(output)
    native = collect_native(found, output)
    manifest = {
        "scope": "Installed requirements closure including build tools; conservative notice set, not a binary link map or legal determination.",
        "runtime": runtime, "packages": packages, "native_supplements": native,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hashes = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes.append(f"{digest}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(f"Collected notices for {len(packages)} installed distributions, Python/Tcl/Tk, and {len(native)} native supplements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
