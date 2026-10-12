"""Explicit R2 pytest host: process launch guards precede optional imports."""
from __future__ import annotations
import sys


def main():
    blocked = {"audit_events": [], "launch_api_attempts": [], "native_symbol_attempts": []}
    launch_symbols = {
        "CreateProcessW", "CreateProcessA", "CreateProcessAsUserW", "CreateProcessAsUserA",
        "CreateProcessWithLogonW", "CreateProcessWithTokenW", "ShellExecuteW", "ShellExecuteA",
        "ShellExecuteExW", "ShellExecuteExA", "WinExec", "NtCreateUserProcess",
        "NtCreateProcess", "NtCreateProcessEx", "RtlCreateUserProcess",
    }
    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn",
                     "os.exec", "os.fork", "os.forkpty"}:
            blocked["audit_events"].append(event)
            raise RuntimeError("R2 forbids child/helper creation: " + event)
        if event in {"ctypes.dlsym", "ctypes.dlsym/handle"}:
            symbol = args[1]
            if isinstance(symbol, bytes):
                symbol = symbol.decode("ascii", errors="replace")
            if symbol in launch_symbols:
                blocked["native_symbol_attempts"].append(symbol)
                raise RuntimeError("R2 forbids resolving a live child-launch symbol: " + symbol)
    sys.addaudithook(audit)

    import os
    import _winapi
    def prohibit(name):
        def forbidden(*args, **kwargs):
            blocked["launch_api_attempts"].append(name)
            raise RuntimeError("R2 forbids helper launch API: " + name)
        return forbidden
    for name in ("system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe", "execl", "execle", "execlp",
                 "execlpe", "execv", "execve", "execvp", "execvpe", "posix_spawn",
                 "posix_spawnp", "fork", "forkpty"):
        if hasattr(os, name):
            setattr(os, name, prohibit("os." + name))
    guarded_create = prohibit("_winapi.CreateProcess")
    _winapi.CreateProcess = guarded_create

    import argparse
    import json
    from pathlib import Path
    import time
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise RuntimeError("run guarded pytest host with -I -S -B")
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "tests"), str(root / ".venv/Lib/site-packages")]
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest
    from capabilities import terminal_win32 as W

    def prohibit_live_bindings(*args, **kwargs):
        raise AssertionError("R2 tests must inject in-memory bindings; live loader is forbidden")
    W._load_live_bindings = prohibit_live_bindings
    W._LiveBindings.__init__ = prohibit_live_bindings

    class Results:
        def __init__(self):
            self.counts = {"passed": 0, "failed": 0, "skipped": 0}
            self.collected = 0
        def pytest_collection_finish(self, session):
            self.collected = len(session.items)
        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                key = "passed" if report.passed else "skipped" if report.skipped else "failed"
                self.counts[key] += 1
            elif report.failed:
                self.counts["failed"] += 1
        def pytest_collectreport(self, report):
            if report.failed:
                self.counts["failed"] += 1
    results = Results()
    command = args.pytest_args
    if command and command[0] == "--":
        command = command[1:]
    started = time.monotonic()
    code = pytest.main(command or ["-q"], plugins=[results])
    report = {
        "phase": "D1G-D2B-R2", "pytest_exit_code": int(code),
        "collected": results.collected, "results": results.counts,
        "seconds": time.monotonic() - started,
        "blocked_process_events": blocked,
        "winapi_launch_guard_retained": _winapi.CreateProcess is guarded_create,
        "no_live_launch_evidence": not any(blocked.values()) and _winapi.CreateProcess is guarded_create,
        "scope": "isolated guarded Python pytest host; command-runner test-host startup excluded",
        "historical_live_harness_invoked": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return int(code) or (1 if not report["no_live_launch_evidence"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
