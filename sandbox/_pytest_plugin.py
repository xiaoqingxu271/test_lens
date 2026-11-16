"""Standalone pytest bootstrap used inside the sandbox subprocess.

This file is intentionally dependency-free beyond pytest and the standard
library. It records per-test outcomes and target-symbol coverage as JSON so the
runner process can build the frozen ExecutionReport without parsing pytest
output.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _main() -> int:
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output_path = Path(config["output_path"])
    project_root = Path(config["project_root"]).resolve()
    pytest_config = Path(config["pytest_config"])

    sys.path.insert(0, str(project_root))
    os.chdir(project_root)

    import pytest

    target_symbols: list[dict] = []
    for raw in config.get("target_symbols", []):
        item = dict(raw)
        absolute = os.path.abspath(item["file"])
        item["file"] = absolute
        item["file_key"] = os.path.normcase(absolute)
        target_symbols.append(item)
    target_files = {item["file_key"] for item in target_symbols}

    state: dict = {
        "tests": {},
        "current_test": None,
        "collection_errors": [],
    }
    coverage: dict = {
        "symbols": {},
        "entries": set(),
    }

    def write_state() -> None:
        payload = {
            "tests": state["tests"],
            "current_test": state["current_test"],
            "collection_errors": state["collection_errors"],
        }
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        temporary.replace(output_path)

    def trace_func(frame, event, arg):
        filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if filename not in target_files:
            return None
        if event in ("call", "line"):
            for symbol in target_symbols:
                if filename != symbol["file_key"]:
                    continue
                if event == "call" and frame.f_code.co_name == symbol["name"]:
                    coverage["entries"].add(symbol["symbol_id"])
                if (
                    event == "line"
                    and symbol["line_start"] <= frame.f_lineno <= symbol["line_end"]
                ):
                    coverage["symbols"].setdefault(symbol["symbol_id"], set()).add(
                        frame.f_lineno
                    )
        return trace_func

    def start_trace() -> None:
        coverage["symbols"] = {}
        coverage["entries"] = set()
        sys.settrace(trace_func)

    def stop_trace() -> None:
        sys.settrace(None)

    def covered_symbols() -> list[dict]:
        result = []
        for symbol in target_symbols:
            symbol_id = symbol["symbol_id"]
            lines = coverage["symbols"].get(symbol_id, set())
            if not lines and symbol_id not in coverage["entries"]:
                continue
            result.append(
                {
                    "symbol_id": symbol_id,
                    "file": symbol["file"],
                    "line_start": symbol["line_start"],
                    "line_end": symbol["line_end"],
                    "entry_hit": symbol_id in coverage["entries"],
                    "executed_lines": sorted(lines),
                    "coverage_source": "sys.settrace",
                }
            )
        return result

    def exception_info(report) -> tuple[str | None, str | None]:
        longrepr = getattr(report, "longrepr", None)
        if longrepr is None:
            return None, None
        reprcrash = getattr(longrepr, "reprcrash", None)
        if reprcrash is not None:
            message = getattr(reprcrash, "message", "") or str(reprcrash)
        else:
            message = str(longrepr)
        exception_type = message.split(":", 1)[0].strip() if ":" in message else message.strip()
        return (exception_type or None), message

    def digest(message: str | None) -> str | None:
        if message is None:
            return None
        return hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()[:16]

    def relative_test_file(report) -> str:
        path = getattr(report, "path", None) or getattr(report, "fspath", None)
        if path is None:
            return report.nodeid.split("::", 1)[0]
        try:
            return os.path.relpath(str(path), str(project_root)).replace("\\", "/")
        except ValueError:
            return str(path)

    class Plugin:
        def __init__(self):
            self._exception_info: dict[tuple[str, str], tuple[str | None, str | None]] = {}

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            if call.excinfo is not None:
                self._exception_info[(report.nodeid, report.when)] = (
                    getattr(call.excinfo.type, "__name__", None),
                    str(call.excinfo.value),
                )

        def _info(self, report) -> tuple[str | None, str | None]:
            info = self._exception_info.get((report.nodeid, report.when))
            if info is None:
                return exception_info(report)
            return info

        def pytest_collectreport(self, report):
            if report.failed:
                exception_type, message = exception_info(report)
                state["collection_errors"].append(
                    {
                        "nodeid": report.nodeid,
                        "exception_type": exception_type,
                        "message": message or "collection error",
                    }
                )
                write_state()

        def pytest_runtest_setup(self, item):
            state["current_test"] = item.nodeid
            start_trace()
            write_state()

        def pytest_runtest_logreport(self, report):
            nodeid = report.nodeid
            entry = state["tests"].setdefault(
                nodeid,
                {
                    "test_id": nodeid,
                    "test_file": relative_test_file(report),
                    "test_name": nodeid.split("::", 1)[1] if "::" in nodeid else report.name,
                    "status": "pending",
                    "failure_kind": "none",
                    "exception_type": None,
                    "message_digest": None,
                    "duration_ms": 0,
                    "covered_symbols": [],
                    "timeout_phase": "execution",
                },
            )
            if report.when == "setup":
                if report.skipped:
                    entry["status"] = "skipped"
                elif report.failed:
                    exception_type, message = self._info(report)
                    entry["status"] = "error"
                    if exception_type in ("ImportError", "ModuleNotFoundError"):
                        entry["failure_kind"] = "import_error"
                    else:
                        entry["failure_kind"] = "exception"
                    entry["exception_type"] = exception_type
                    entry["message_digest"] = digest(message)
            elif report.when == "call":
                entry["duration_ms"] = int(report.duration * 1000)
                if report.passed:
                    entry["status"] = "passed"
                    entry["failure_kind"] = "none"
                elif report.skipped:
                    entry["status"] = "skipped"
                elif report.failed:
                    exception_type, message = self._info(report)
                    entry["status"] = "failed"
                    if exception_type == "AssertionError" or (
                        message and message.startswith("AssertionError")
                    ):
                        entry["failure_kind"] = "assertion"
                    elif exception_type in ("ImportError", "ModuleNotFoundError"):
                        entry["failure_kind"] = "import_error"
                    else:
                        entry["failure_kind"] = "exception"
                    entry["exception_type"] = exception_type
                    entry["message_digest"] = digest(message)
            elif report.when == "teardown":
                if report.failed and entry["status"] == "passed":
                    exception_type, message = self._info(report)
                    entry["status"] = "error"
                    entry["failure_kind"] = "exception"
                    entry["exception_type"] = exception_type
                    entry["message_digest"] = digest(message)
            write_state()

        def pytest_runtest_teardown(self, item):
            stop_trace()
            entry = state["tests"].get(item.nodeid)
            if entry is not None:
                entry["covered_symbols"] = covered_symbols()
            state["current_test"] = None
            write_state()

        def pytest_sessionfinish(self, session, exitstatus):
            stop_trace()
            write_state()

    args = [
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "-c",
        str(pytest_config),
        "--rootdir",
        str(project_root),
    ]
    args.extend(str(path) for path in config.get("test_paths", []))
    exit_code = pytest.main(args, plugins=[Plugin()])
    sys.exit(int(exit_code))


if __name__ == "__main__":
    _main()
