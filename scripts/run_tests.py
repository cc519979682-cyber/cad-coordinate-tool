"""Run each regression module in a fresh interpreter to isolate Tk state.

Usage: python scripts/run_tests.py [--timeout 600] [tests.test_core ...]
Reports are local generated files; do not commit them to the public repository.
Native AutoCAD tests are opt-in through the COORDTOOL_NATIVE_* variables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_module(module, timeout, report_directory):
    started = time.perf_counter()
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    timed_out = False
    try:
        process = subprocess.run(
            [sys.executable, "-m", "unittest", module, "-v"],
            cwd=ROOT, env=environment, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        output = process.stdout + "\n" + process.stderr
        code = process.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        code = 124
        output = (_text(error.stdout) + "\n" + _text(error.stderr)
                  + f"\nModule timed out after {timeout:g} seconds.\n")
    except OSError as error:
        code = 1
        output = f"Unable to start test module: {error}\n"
    count = re.search(r"Ran (\d+) tests?", output)
    status = re.search(r"^(?:OK|FAILED)(?: \(([^\n]*)\))?\s*$", output, re.MULTILINE)
    counts = {name: 0 for name in (
        "skipped", "failures", "errors", "expected failures", "unexpected successes")}
    if status and status[1]:
        for name, value in re.findall(r"([a-z ]+)=(\d+)", status[1]):
            if name.strip() in counts:
                counts[name.strip()] = int(value)
    if code == 0 and (count is None or status is None or int(count[1]) == 0):
        code = 1
        output += "\nTest module did not report a completed nonempty test run.\n"
    log = report_directory / (module.replace(".", "_") + ".log")
    log.write_text(output, encoding="utf-8")
    return {
        "module": module,
        "code": code,
        "tests": int(count[1]) if count else 0,
        "skipped": counts["skipped"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "expected_failures": counts["expected failures"],
        "unexpected_successes": counts["unexpected successes"],
        "timed_out": timed_out,
        "seconds": round(time.perf_counter() - started, 3),
        "log": log.relative_to(ROOT).as_posix(),
    }, output


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="*", help="Optional tests.test_* modules")
    parser.add_argument("--timeout", type=float, default=600,
                        help="Maximum seconds per module (default: 600)")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than zero and at most 3600 seconds")
    available = ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))]
    modules = args.modules or available
    invalid = [module for module in modules if module not in available]
    if invalid:
        parser.error("Unknown test module(s): " + ", ".join(invalid))
    if not modules:
        parser.error("No test modules found")
    report_directory = ROOT / "reports"
    report_directory.mkdir(exist_ok=True)
    results = []
    for module in modules:
        result, output = run_module(module, args.timeout, report_directory)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["code"]:
            print(output[-6000:], flush=True)
    report = {
        "success": all(result["code"] == 0 for result in results),
        "tests": sum(result["tests"] for result in results),
        "skipped": sum(result["skipped"] for result in results),
        "failures": sum(result["failures"] for result in results),
        "errors": sum(result["errors"] for result in results),
        "seconds": round(sum(result["seconds"] for result in results), 3),
        "runs": results,
    }
    (report_directory / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"},
                     ensure_ascii=False), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
