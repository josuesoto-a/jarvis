"""Explicit one-child D1G-D2B diagnostic. Never imported by ordinary tests."""
from __future__ import annotations
import sys


def main():
    # Install helper guards before optional imports, just as in D2a.
    guards = {"audit_events": [], "launcher_attempts": [], "platform_attempts": []}
    def forbidden(name):
        def blocked(*args, **kwargs):
            guards["launcher_attempts"].append(name)
            raise RuntimeError("D2b forbidden helper: " + name)
        return blocked
    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn",
                     "os.exec", "os.fork", "os.forkpty"}:
            guards["audit_events"].append(event)
            raise RuntimeError("D2b helper audit stop")
        if event == "import" and args[0].split(".")[0] in {
                "subprocess", "platform", "multiprocessing", "wmi", "win32com", "pythoncom", "win32process"}:
            raise RuntimeError("D2b forbidden module: " + args[0])
    sys.addaudithook(audit)
    if "platform" in sys.modules or "subprocess" in sys.modules:
        raise RuntimeError("isolated interpreter required")
    import os
    import _winapi
    from types import ModuleType
    for name in ("system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe", "execl", "execle", "execlp",
                 "execlpe", "execv", "execve", "execvp", "execvpe", "posix_spawn",
                 "posix_spawnp", "fork", "forkpty"):
        if hasattr(os, name):
            setattr(os, name, forbidden("os." + name))
    _winapi.CreateProcess = forbidden("_winapi.CreateProcess")
    subprocess_guard = ModuleType("subprocess")
    for name in ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        setattr(subprocess_guard, name, forbidden("subprocess." + name))
    sys.modules["subprocess"] = subprocess_guard
    platform_guard = ModuleType("platform")
    def platform_attribute(name):
        if name.startswith("__"):
            raise AttributeError(name)
        guards["platform_attempts"].append(name)
        raise RuntimeError("platform helper fallback forbidden")
    platform_guard.__getattr__ = platform_attribute
    sys.modules["platform"] = platform_guard

    import argparse
    import ctypes as C
    import json
    import struct
    import traceback
    from collections import Counter
    from dataclasses import asdict
    from hashlib import sha256
    from pathlib import Path
    from threading import Event, Thread, current_thread, main_thread
    from time import monotonic, time

    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge", required=True, choices=["D1G-D2B-ONE-CHILD"])
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise RuntimeError("invoke official Python with -I -S -B")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))
    from capabilities import terminal as T, terminal_runtime as R, terminal_win32 as W
    from d2b_contract import PYTHON, DIGEST, require, frozen_target, exact_capture

    evidence_path = root / "diagnostics/d1g_d2a_result.json"
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    target = frozen_target(evidence)
    require(sys.executable == PYTHON, "exact diagnostic interpreter required")
    expected_head = "722e4d0513d56f40853187e576c682ac9ca33691"
    require((root / ".git/HEAD").read_text().strip() == "ref: refs/heads/main"
            and (root / ".git/refs/heads/main").read_text().strip() == expected_head
            and (root / ".git/refs/remotes/origin/main").read_text().strip() == expected_head, "repository base differs: STOP")
    directory = root / "diagnostics/d2b"
    directory.mkdir(exist_ok=True)
    output = directory / "result.json"
    reservation = directory / "single_attempt_reserved.json"
    require(not output.exists() and not reservation.exists(), "D2b already reserved: NEVER retry")
    report = {"phase": "D1G-D2B", "status": "STOP", "scope": "isolated diagnostic interpreter only; external command runner and ordinary tests excluded",
              "base": {"branch": "main", "head": "722e4d0513d56f40853187e576c682ac9ca33691", "origin_main": "722e4d0513d56f40853187e576c682ac9ca33691"},
              "diagnostic_pid": os.getpid(), "start_utc_epoch": time(),
              "d2a_evidence_sha256": sha256(evidence_bytes).hexdigest(),
              "target": T.terminal_approval_arguments(target),
              "command_line": T.serialize_windows_command_line(target.executable_resolved, target.argv),
              "expected_stdout": evidence["proposed_d2b_target"]["expected_stdout"],
              "expected_stderr": evidence["proposed_d2b_target"]["expected_stderr"],
              "newline_normalization": "none; exact utf8-replace/v1 captured content",
              "creation_counts": {"wrapper_attempts": 0, "native_dispatches": 0, "known_successes": 0, "known_failures": 0, "uncertain_outcomes": 0},
              "resume_counts": {"native_dispatches": 0, "result": None},
              "production_terminal": "OFF", "d3_performed": False, "d4_performed": False}
    retained = []
    hold = Event()

    def owner():
        require(current_thread() is not main_thread() and not current_thread().daemon, "owner affinity failed")
        counts = report["creation_counts"]
        calls, acquired, released = Counter(), Counter(), Counter()
        live = {kind: set() for kind in ("handles", "heap", "local", "attributes")}
        release_order, events = [], []
        original_windll, original_cdll = C.WinDLL, C.CDLL
        original_loader = W._load_live_bindings
        original_host = T._validate_windows_host, W._validate_windows_host
        domain = R.LaunchAdmissionDomain()
        retained.extend((domain, live))
        backend = None
        invocations = []
        original_admit = domain.admit
        def admit(**kwargs):
            invocation = original_admit(**kwargs)
            invocations.append(invocation)
            retained.append(invocation)
            return invocation
        domain.admit = admit

        def native_create(function):
            def dispatch(*arguments):
                require(counts["native_dispatches"] == 0 and reservation.exists(), "one native dispatch budget exhausted")
                counts["native_dispatches"] += 1
                # NO post-return Python instrumentation before backend's P1/C1 latch.
                return function(*arguments)
            return dispatch
        def native_resume(function):
            def dispatch(*arguments):
                require(report["resume_counts"]["native_dispatches"] == 0, "second resume forbidden")
                report["resume_counts"]["native_dispatches"] += 1
                return function(*arguments)
            return dispatch
        class GuardedDLL:
            def __init__(self, dll):
                self._dll = dll
            def __getattr__(self, name):
                function = getattr(self._dll, name)
                if name in {"CreateProcessW", "ResumeThread"}:
                    function.restype, function.argtypes = W.API_SIGNATURES[name]
                if name == "CreateProcessW":
                    function = native_create(function)
                elif name == "ResumeThread":
                    function = native_resume(function)
                elif name in {"CreateProcessA", "CreateProcessAsUserW", "CreateProcessAsUserA",
                              "CreateProcessWithLogonW", "CreateProcessWithTokenW", "ShellExecuteW",
                              "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA", "WinExec"}:
                    function = forbidden("native." + name)
                setattr(self, name, function)
                return function
        def windll(*args, **kwargs):
            return GuardedDLL(original_windll(*args, **kwargs))
        def cdll(*args, **kwargs):
            return GuardedDLL(original_cdll(*args, **kwargs))
        C.WinDLL, C.CDLL = windll, cdll
        C.windll, C.cdll = C.LibraryLoader(windll), C.LibraryLoader(cdll)

        def value(argument):
            return C.cast(argument, C.POINTER(W.HANDLE))[0]
        def observe(name, function):
            def observed(*arguments):
                calls[name] += 1
                result = function(*arguments)
                error = C.get_last_error()
                kind, raw = None, None
                if name in {"CreateFileW", "CreateJobObjectW", "CreateNamedPipeW", "CreateEventW"} and result not in {None, 0, W.INVALID_HANDLE_VALUE}:
                    kind, raw = "handles", result
                elif name == "DuplicateHandle" and result:
                    kind, raw = "handles", value(arguments[3])
                elif name in {"OpenProcessToken", "OpenThreadToken"} and result:
                    kind, raw = "handles", value(arguments[-1])
                elif name == "HeapAlloc" and result:
                    kind, raw = "heap", result
                elif name in {"ConvertSidToStringSidW", "ConvertStringSecurityDescriptorToSecurityDescriptorW"} and result:
                    kind, raw = "local", value(arguments[1 if name == "ConvertSidToStringSidW" else 2])
                elif name == "InitializeProcThreadAttributeList" and arguments[0] and result:
                    kind, raw = "attributes", arguments[0]
                if kind:
                    require(raw not in live[kind], "duplicate native ownership")
                    live[kind].add(raw)
                    acquired[kind] += 1
                kind, raw = None, None
                if name == "CloseHandle" and result:
                    kind, raw = "handles", arguments[0]
                elif name == "HeapFree" and result:
                    kind, raw = "heap", arguments[2]
                elif name == "LocalFree" and not result:
                    kind, raw = "local", arguments[0]
                elif name == "DeleteProcThreadAttributeList":
                    kind, raw = "attributes", arguments[0]
                if kind:
                    live[kind].remove(raw)
                    released[kind] += 1
                C.set_last_error(error)
                return result
            return observed

        def loader():
            bindings = original_loader()
            for name in W.API_SIGNATURES:
                if name not in {"CreateProcessW", "ResumeThread"}:
                    setattr(bindings, name, observe(name, getattr(bindings, name)))
            function = bindings._kernel.GetHandleInformation
            function.restype, function.argtypes = W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]
            bindings.GetHandleInformation = observe("GetHandleInformation", function)
            return bindings

        class Backend(W.Win32TerminalBackend):
            def open_executable(self, scope, path):
                self.scope = scope
                self.executable = super().open_executable(scope, path)
                self.digest = sha256()
                self.header = bytearray()
                return self.executable
            def open_cwd(self, scope, path):
                self.cwd = super().open_cwd(scope, path)
                return self.cwd
            def file_evidence(self, resource):
                result = super().file_evidence(resource)
                report.setdefault("file_verification", []).append(asdict(result))
                return result
            def read_executable(self, resource, size):
                result = super().read_executable(resource, size)
                self.digest.update(result)
                self.header.extend(result[:max(0, T.TERMINAL_PE_HEADER_BYTES - len(self.header))])
                return result
            def configure_job(self, job):
                super().configure_job(job)
                self.job = job
                info, length = W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION(), W.DWORD()
                self._bool("QueryInformationJobObject", job._raw(), W.JobObjectExtendedLimitInformation, C.byref(info), C.sizeof(info), C.byref(length))
                require(length.value == C.sizeof(info) and info.BasicLimitInformation.LimitFlags == W.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, "Job policy differs")
                require(super().job_active(job) == 0, "fresh Job not empty")
                report["job"] = {"unnamed": True, "limit_flags": info.BasicLimitInformation.LimitFlags, "initial_active": 0, "at_creation_attribute": "PROC_THREAD_ATTRIBUTE_JOB_LIST"}
            def prepare_launch(self, scope, target, command, environment, job, children):
                result = super().prepare_launch(scope, target, command, environment, job, children)
                with scope.invocation.resources.borrow(result) as lease:
                    launch = lease.payload

                    expected_environment = environment.encode("utf-16-le")
                    require(C.string_at(C.addressof(launch._environment), len(expected_environment)) == expected_environment
                            and W._decode_utf16(launch._application, len(launch._application) - 1) == target.executable_resolved
                            and W._decode_utf16(launch._command, len(launch._command) - 1) == command
                            and W._decode_utf16(launch._cwd, len(launch._cwd) - 1) == target.cwd
                            and launch._call_arguments[4].value == 1
                            and launch._call_arguments[5].value == W.CREATION_FLAGS
                            and not launch._call_arguments[2] and not launch._call_arguments[3], "native marshaling differs")
                    report["marshaling"] = {"explicit_application": True, "mutable_command": True,
                                           "exact_frozen_environment_block": True, "environment_implicit_inheritance": False,
                                           "exact_cwd": True, "process_thread_security_null": True,
                                           "creation_flags_unchanged": True}

                    allow = set(launch._attributes._handles)
                    inherit = []
                    for handle in live["handles"]:
                        flags = W.DWORD()
                        self._bool("GetHandleInformation", handle, C.byref(flags))
                        if flags.value & 1:
                            inherit.append(handle)
                    require(len(allow) == 3 and set(inherit) == allow, "inherited handle set differs")
                    require(list(launch._attributes._jobs) == [job._raw()], "Job attribute differs")
                    require(launch._startup.StartupInfo.cb == C.sizeof(W.STARTUPINFOEXW)
                            and launch._startup.StartupInfo.dwFlags == W.STARTF_USESTDHANDLES
                            and [launch._startup.StartupInfo.hStdInput, launch._startup.StartupInfo.hStdOutput, launch._startup.StartupInfo.hStdError] == list(launch._attributes._handles), "startup stdio differs")
                    report["attributes"] = {"count": 2, "handle_list_count": 3, "job_list_count": 1, "arrays_retained": True, "flags": W.CREATION_FLAGS, "startup_cb": launch._startup.StartupInfo.cb}
                    report["inheritance"] = {"exact_set": ["stdin-nul", "stdout-writer", "stderr-writer"], "inheritable_handles": len(inherit), "other_owned_handles_noninheritable": True}
                return result
            def create_process(self, receipt, launch):
                counts["wrapper_attempts"] += 1
                require(counts["wrapper_attempts"] == 1, "second wrapper attempt forbidden")
                ledger = self.scope.invocation.resources
                with ledger.borrow(self.executable) as lease:
                    executable_info = self.file_evidence(lease.payload)
                with ledger.borrow(self.cwd) as lease:
                    cwd_info = self.file_evidence(lease.payload)
                require(executable_info == R.FileEvidence(**{**report["file_verification"][0], "stamp": tuple(report["file_verification"][0]["stamp"])})
                        and executable_info.size == 103192 and executable_info.final_path == PYTHON
                        and self.digest.hexdigest() == DIGEST and cwd_info.final_path == target.cwd
                        and cwd_info.directory and cwd_info.disk, "immediate held-handle identity differs")
                machine = T.inspect_windows_console_pe(bytes(self.header), file_size=executable_info.size)
                pe = struct.unpack_from("<I", self.header, 0x3c)[0]
                subsystem = struct.unpack_from("<H", self.header, pe + 24 + 68)[0]
                require(machine == 0x8664 and subsystem == 3, "PE identity differs")
                report["executable"] = {"final_path": executable_info.final_path, "sha256": self.digest.hexdigest(), "machine": machine, "subsystem": subsystem, "size": executable_info.size, "same_restrictive_handle_held": True}
                report["cwd"] = asdict(cwd_info)
                original_success, original_failed = receipt.record_success, receipt.record_failed
                def committed_success():
                    original_success()  # AUTHORITATIVE P1 FIRST; no output inspection/logging.
                    counts["known_successes"] += 1
                    events.append("P1 committed")
                    report["p1_commit"] = {"no_payload_first_commit": True, "before_output_inspection": True, "before_adoption": True, "before_membership": True, "before_resume": True}
                    for field in ("hProcess", "hThread"):
                        raw = getattr(launch._information, field)
                        require(raw not in {None, 0, W.INVALID_HANDLE_VALUE}, "known-success output invalid")
                        live["handles"].add(raw)
                        acquired["handles"] += 1
                    report["child_pid"] = launch._information.dwProcessId
                def committed_failed():
                    original_failed()
                    counts["known_failures"] += 1
                    report["native_creation_error"] = C.get_last_error()
                receipt.record_success, receipt.record_failed = committed_success, committed_failed
                with reservation.open("x", encoding="utf-8") as marker:
                    json.dump({"phase": "D1G-D2B", "pid": os.getpid(), "policy": "one dispatch; never retry even if result unavailable"}, marker)
                super().create_process(receipt, launch)
                report["creation_receipt_after_backend"] = asdict(receipt.snapshot())
                events.append("backend adoption complete")
            def membership(self, process, job):
                result = super().membership(process, job)
                report.setdefault("membership_observations", []).append(result)
                require(result, "Job-at-creation membership failed")
                events.append("membership verified")
                return result
            def resume(self, thread):
                snapshot = thread._launch._receipt.snapshot()
                require(snapshot.process_created and snapshot.adoption_complete
                        and report.get("membership_observations", [False])[0]
                        and not any(s.borrowers for s in self.scope.invocation.resources.snapshot()
                                    if s.name not in {"primary-thread"}), "resume prerequisites differ")
                require(all(r.snapshot().state is R.OwnershipState.RELEASED for r in self.scope.creation_only), "creation-only ownership remains")
                result = super().resume(thread)
                report["resume_counts"]["result"] = result
                events.append("ResumeThread returned")
                return result
            def root_exited(self, process):
                result = super().root_exited(process)
                if result:
                    report["root_exit_observed"] = True
                return result
            def job_active(self, job):
                result = super().job_active(job)
                report.setdefault("job_active_observations", []).append(result)
                return result
            def release(self, token):
                name = next((r.snapshot().name for r in self.scope.ordered if r._identity_payload_unlocked() is token), type(token).__name__)
                result = super().release(token)
                if result:
                    release_order.append(name)
                return result

        try:
            kernel = C.WinDLL("kernel32", use_last_error=True)
            current_process = kernel.GetCurrentProcess
            current_process.restype, current_process.argtypes = W.HANDLE, []
            architecture = kernel.IsWow64Process2
            architecture.restype, architecture.argtypes = W.BOOL, [W.HANDLE, C.POINTER(W.WORD), C.POINTER(W.WORD)]
            process_machine, native_machine = W.WORD(), W.WORD()
            require(architecture(current_process(), C.byref(process_machine), C.byref(native_machine)), "host architecture query failed")
            version = sys.getwindowsversion()
            def host_check():
                require(sys.platform == "win32" and version.major == 10 and version.build >= 10240
                        and version.product_type == 1 and sys.maxsize > 2**32
                        and C.sizeof(C.c_void_p) == 8 and C.sizeof(C.c_wchar) == 2
                        and process_machine.value == 0 and native_machine.value == 0x8664, "unsupported native x64 host")
            T._validate_windows_host = W._validate_windows_host = host_check
            host_check()
            report["host"] = {"windows": list(version), "product_type": version.product_type, "process_machine": process_machine.value, "native_machine": native_machine.value, "pointer_bytes": C.sizeof(C.c_void_p), "wchar_bytes": C.sizeof(C.c_wchar), "python": sys.version, "host_validator": "D2a diagnostic-local direct-native adapter; identical host policy"}
            W._load_live_bindings = loader
            backend = Backend(owner_thread=current_thread())
            retained.append(backend)
            runtime = R.WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=current_thread())
            retained.append(runtime)
            outcome = runtime.execute(target)
            report["outcome"] = asdict(outcome)
            require(counts == {"wrapper_attempts": 1, "native_dispatches": 1, "known_successes": 1, "known_failures": 0, "uncertain_outcomes": 0}, "creation counts differ")
            require(outcome.creation_classification is R.CreationClassification.PROCESS_CREATED
                    and outcome.process_resumed and outcome.process_exited and outcome.cleanup.job_empty
                    and outcome.cleanup_completed and outcome.disposition is R.RuntimeDisposition.COMPLETED, "runtime did not complete")
            require(report["resume_counts"] == {"native_dispatches": 1, "result": 1}, "resume evidence differs")
            exact_capture(outcome.execution.stdout, report["expected_stdout"])
            exact_capture(outcome.execution.stderr, report["expected_stderr"])
            require(acquired == released and not any(live.values())
                    and domain.snapshot().state is R.LaunchAdmissionState.OPEN
                    and domain.quarantine_owner is None and release_order[-1] == "job", "cleanup accounting differs")
            require(not any(guards.values()), "helper guard was reached")
            report["status"] = "PASS"
        except BaseException:
            report["failure"] = traceback.format_exc()
        finally:
            W._load_live_bindings = original_loader
            T._validate_windows_host, W._validate_windows_host = original_host
            # Keep native launcher guards installed until diagnostic exit.
            invocation = invocations[0] if invocations else None
            if invocation and invocation.creation.snapshot().result_pending:
                counts["uncertain_outcomes"] = 1
            report.update({"creation_classification": invocation.creation.snapshot().classification if invocation else "P0",
                           "native_call_counts": dict(calls), "native_acquisitions": dict(acquired),
                           "native_releases": dict(released), "native_remaining": {k: len(v) for k, v in live.items()},
                           "release_order": release_order, "ordered_events": events,
                           "domain": asdict(domain.snapshot()), "quarantined": domain.quarantine_owner is not None,
                           "poisoned": domain.snapshot().state is R.LaunchAdmissionState.POISONED,
                           "resources": [asdict(r) for r in invocation.resources.snapshot()] if invocation else [],
                           "cleanup_evidence": asdict(invocation.cleanup_evidence()) if invocation else None,
                           "guards": guards, "helper_child_count": 0 if not any(guards.values()) else None,
                           "terminal_child_count": counts["known_successes"] if not counts["uncertain_outcomes"] else None,
                           "execution_seconds": 30, "cleanup_seconds": 5,
                           "guards_remained_installed": C.WinDLL is windll and C.CDLL is cdll and sys.modules["platform"] is platform_guard and sys.modules["subprocess"] is subprocess_guard,
                           "end_utc_epoch": time()})
            output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
            print(json.dumps({k: report.get(k) for k in ("status", "failure", "creation_classification", "creation_counts", "resume_counts", "native_remaining", "quarantined", "poisoned")}), flush=True)
            if any(live.values()) or domain.quarantine_owner is not None:
                print("STOP: retain owner/native state; no retry or uncertain-handle work.", flush=True)
                hold.wait()

    def guarded_owner():
        try:
            owner()
        except BaseException:
            report["failure"] = traceback.format_exc()
            output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
            print(report["failure"], flush=True)
            if retained:
                hold.wait()
    worker = Thread(target=guarded_owner, name="D2b-native-diagnostic-owner", daemon=False)
    worker.start()
    worker.join()
    report["owner_thread"] = {"name": worker.name, "explicit_non_main": True, "daemon": worker.daemon, "joined": not worker.is_alive(), "failures_propagated": True}
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    require(report["status"] == "PASS", report.get("failure", "D2b STOP"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




