"""Pure ctypes layout and in-memory native marshaling; live loader forbidden."""
from collections import deque
import ctypes as C
import ast
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from threading import Thread

import pytest

import capabilities.terminal_win32 as W
from capabilities.terminal import serialize_windows_command_line, build_windows_environment_block
from capabilities.terminal_runtime import (
    CreationClassification as CC, CreationReceipt, CleanupReason, LaunchAdmissionDomain,
    LaunchAdmissionError, LaunchAdmissionState, OwnershipState, RuntimeDisposition,
    TerminalLifecycleError, WindowsTerminalRuntime,
)
from terminal_runtime_fakes import target_for, on_owner, forbid_live_loader
from terminal_win32_fakes import FakeBindings, run_native, pointer, wide


LAYOUTS = [
    (W.SECURITY_ATTRIBUTES, 24, 8, {"nLength": 0, "lpSecurityDescriptor": 8, "bInheritHandle": 16}),
    (W.STARTUPINFOW, 104, 8, {"cb": 0, "lpReserved": 8, "dwX": 32, "dwFlags": 60,
                            "wShowWindow": 64, "lpReserved2": 72, "hStdInput": 80, "hStdError": 96}),
    (W.STARTUPINFOEXW, 112, 8, {"StartupInfo": 0, "lpAttributeList": 104}),
    (W.PROCESS_INFORMATION, 24, 8, {"hProcess": 0, "hThread": 8, "dwProcessId": 16, "dwThreadId": 20}),
    (W.OVERLAPPED, 32, 8, {"Internal": 0, "InternalHigh": 8, "position": 16, "hEvent": 24}),
    (W.JOBOBJECT_BASIC_LIMIT_INFORMATION, 64, 8, {"LimitFlags": 16, "MinimumWorkingSetSize": 24,
                                              "ActiveProcessLimit": 40, "Affinity": 48, "PriorityClass": 56}),
    (W.IO_COUNTERS, 48, 8, {"ReadOperationCount": 0, "OtherTransferCount": 40}),
    (W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION, 144, 8, {"BasicLimitInformation": 0, "IoInfo": 64,
                                                 "ProcessMemoryLimit": 112, "PeakJobMemoryUsed": 136}),
    (W.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, 48, 8, {"TotalPageFaultCount": 32, "ActiveProcesses": 40}),
    (W.BY_HANDLE_FILE_INFORMATION, 52, 4, {"dwVolumeSerialNumber": 28, "nFileSizeHigh": 32, "nFileIndexLow": 48}),
    (W.SID_AND_ATTRIBUTES, 16, 8, {"Sid": 0, "Attributes": 8}),
    (W.TOKEN_GROUPS, 24, 8, {"GroupCount": 0, "Groups": 8}),
]


@pytest.mark.parametrize("structure,size,alignment,offsets", LAYOUTS)
def test_windows_x64_layout_without_native_calls(structure, size, alignment, offsets):
    assert C.sizeof(W.HANDLE) == 8
    assert C.sizeof(structure) == size and C.alignment(structure) == alignment
    for field, expected in offsets.items():
        assert getattr(structure, field).offset == expected
    assert not hasattr(structure, "_pack_")


@pytest.mark.parametrize("scalar,size", [(W.BOOL, 4), (W.BYTE, 1), (W.WORD, 2), (W.WCHAR, 2),
                                         (W.DWORD, 4), (W.LONG, 4), (W.ULONG, 4), (W.ULONG_PTR, 8),
                                         (W.SIZE_T, 8), (W.HANDLE, 8), (W.LPVOID, 8), (W.LPCVOID, 8)])
def test_explicit_scalar_widths(scalar, size):
    assert C.sizeof(scalar) == size
    assert W.BOOL is not C.c_bool


def test_all_live_entry_points_have_explicit_signatures_and_no_opaque_internals():
    assert W.API_SIGNATURES["CreateProcessW"][0] is W.BOOL
    assert len(W.API_SIGNATURES["CreateProcessW"][1]) == 10
    assert W.API_SIGNATURES["UpdateProcThreadAttribute"][1][2] is W.ULONG_PTR
    assert W.API_SIGNATURES["InitializeProcThreadAttributeList"][1][0] is W.LPVOID
    assert W.API_SIGNATURES["DeleteProcThreadAttributeList"][0] is None
    assert not hasattr(W, "PROC_THREAD_ATTRIBUTE_LIST")
    assert "AssignProcessToJobObject" not in W.API_SIGNATURES
    assert "GetProcessId" not in W.API_SIGNATURES and "GetThreadId" not in W.API_SIGNATURES


@pytest.mark.parametrize("argv", [(), ("",), ("a b", "ends\\", 'a"b'), ("🌍", "空 白", " a ")])
def test_exact_createprocess_marshaling_uses_existing_transport(argv):
    api = FakeBindings()
    target = target_for(api.image, argv=argv)
    outcome, api, _ = run_native(api, target=target)
    assert outcome.disposition is RuntimeDisposition.COMPLETED
    record = api.launch_record
    assert record["application"] == target.executable_resolved
    assert record["command"] == serialize_windows_command_line(target.executable_resolved, target.argv)
    assert record["cwd"] == target.cwd
    assert record["environment"] == build_windows_environment_block(target.environment_mapping())
    assert record["environment"].endswith("\0\0")
    assert record["inherit"] is True
    assert record["flags"] == W.CREATION_FLAGS == 0x08080404
    assert record["process_security"] is record["thread_security"] is None
    assert record["cb"] == C.sizeof(W.STARTUPINFOEXW) and record["startup_flags"] == W.STARTF_USESTDHANDLES
    assert record["verification_held"]
    attrs = record["attributes"]
    assert set(attrs) == {W.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, W.PROC_THREAD_ATTRIBUTE_JOB_LIST}
    assert attrs[W.PROC_THREAD_ATTRIBUTE_HANDLE_LIST] == record["std_handles"]
    assert len(record["std_handles"]) == 3 and len(attrs[W.PROC_THREAD_ATTRIBUTE_JOB_LIST]) == 1
    assert all(api.handles[h]["inheritable"] for h in record["std_handles"])
    assert all(not v["inheritable"] for h, v in api.handles.items() if h not in record["std_handles"])
    assert api.counts["CreateProcessW"] == api.counts["ResumeThread"] == 1


def test_mutable_utf16_buffers_preserve_non_bmp_and_embedded_separators():
    buffer = W._utf16("a🌍")
    assert C.sizeof(buffer) == 8
    buffer[0] = ord("b")
    assert W._decode_utf16(buffer, len(buffer) - 1) == "b🌍"
    environment = "PATH=A=B\0SYSTEMROOT=C:\\Windows\0\0"
    buffer = W._utf16(environment, terminated=False)
    assert W._decode_utf16(buffer, len(buffer)) == environment


def test_exact_environment_does_not_read_or_merge_ambient(monkeypatch):
    api = FakeBindings()
    target = target_for(api.image)
    monkeypatch.setenv("PATH", "different after approval")
    monkeypatch.setenv("NEW_SECRET", "excluded")
    outcome, _, _ = run_native(api, target=target)
    assert outcome.execution.environment_identity == target.environment_identity
    assert api.launch_record["environment"] == build_windows_environment_block(dict(target.environment))
    assert "NEW_SECRET" not in api.launch_record["environment"]


def test_restrictive_executable_cwd_and_nul_open_flags():
    _, api, _ = run_native()
    opens = [args for name, args in api.calls if name == "CreateFileW"]
    exe = next(args for args in opens if wide(args[0]) == r"C:\Tools\tool.exe")
    cwd = next(args for args in opens if wide(args[0]) == r"C:\Work")
    nul = next(args for args in opens if wide(args[0]) == "NUL")
    assert exe[1:6] == (W.GENERIC_READ, W.FILE_SHARE_READ, None, W.OPEN_EXISTING, 0)
    assert cwd[1:6] == (W.FILE_READ_ATTRIBUTES, W.FILE_SHARE_READ | W.FILE_SHARE_WRITE,
                        None, W.OPEN_EXISTING, W.FILE_FLAG_BACKUP_SEMANTICS)
    assert nul[1:6] == (W.GENERIC_READ, W.FILE_SHARE_READ | W.FILE_SHARE_WRITE, None, W.OPEN_EXISTING, 0)
    executable = next(h for h, info in api.handles.items() if info["kind"] == "executable")
    reads = [args for name, args in api.calls if name == "ReadFile" and args[0] == executable]
    assert len(reads) == 2 and all(args[2] == 65536 and args[4] is None for args in reads)
    assert all(args[0] == executable for name, args in api.calls if name == "GetFileInformationByHandle" and args[0] == executable)


def test_job_kill_on_close_only_and_creation_time_attributes_lifetime():
    _, api, _ = run_native()
    policy = W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION.from_buffer_copy(api.job_policy)
    assert policy.BasicLimitInformation.LimitFlags == W.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert policy.BasicLimitInformation.ActiveProcessLimit == 0
    assert policy.ProcessMemoryLimit == policy.JobMemoryLimit == 0
    sequence = [name for name, _ in api.calls]
    assert sequence.count("InitializeProcThreadAttributeList") == 2
    assert sequence.count("UpdateProcThreadAttribute") == 2
    assert sequence.count("DeleteProcThreadAttributeList") == 1
    assert sequence.index("CreateProcessW") < sequence.index("DeleteProcThreadAttributeList") < sequence.index("ResumeThread")
    record = api.launch_record
    address = record["attribute_address"]
    deletion = next(i for i, (name, args) in enumerate(api.calls) if name == "DeleteProcThreadAttributeList")
    freeing = next(i for i, (name, args) in enumerate(api.calls) if name == "HeapFree" and args[2] == address)
    assert deletion < freeing
    job = record["attributes"][W.PROC_THREAD_ATTRIBUTE_JOB_LIST][0]
    closes = [args[0] for name, args in api.calls if name == "CloseHandle"]
    assert closes[-1] == job
    resume = sequence.index("ResumeThread")
    for handle in record["std_handles"]:
        assert next(i for i, (name, args) in enumerate(api.calls) if name == "CloseHandle" and args[0] == handle) < resume
    assert sequence.index("IsProcessInJob") < resume


def test_named_pipe_security_and_exact_modes_are_reviewable():
    _, api, _ = run_native()
    pipes = [args for name, args in api.calls if name == "CreateNamedPipeW"]
    assert len(pipes) == 2
    pipe_names = [wide(args[0]) for args in pipes]
    assert len(set(pipe_names)) == 2
    for name in pipe_names:
        assert name.startswith(r"\\.\pipe\jarvis-terminal-") and len(name.rsplit("-", 1)[1]) == 32
    for args in pipes:
        assert args[1] == W.PIPE_ACCESS_INBOUND | W.FILE_FLAG_OVERLAPPED | W.FILE_FLAG_FIRST_PIPE_INSTANCE
        assert args[2] == W.PIPE_REJECT_REMOTE_CLIENTS
        assert args[3:6] == (1, 16384, 16384)
        security = pointer(args[7], W.SECURITY_ATTRIBUTES).contents
        assert security.bInheritHandle == 0 and security.lpSecurityDescriptor
    assert api.security_sddl == "D:P(A;;0x0012019f;;;S-1-5-5-100-200)"
    assert "WD" not in api.security_sddl and "AN" not in api.security_sddl
    writer_opens = [args for name, args in api.calls if name == "CreateFileW" and wide(args[0]) in pipe_names]
    assert all(args[1] == W.FILE_WRITE_DATA | W.SYNCHRONIZE and args[2:6] == (0, None, W.OPEN_EXISTING, 0)
               for args in writer_opens)
    assert api.counts["GetNamedPipeClientProcessId"] == 2
    assert api.counts["DuplicateHandle"] == 3
    assert all(args[-1] == W.DUPLICATE_SAME_ACCESS for name, args in api.calls if name == "DuplicateHandle")


@pytest.mark.parametrize("creation", ["C1", "P1", "CU"])
def test_exactly_one_create_commit_with_populated_native_output_storage(creation):
    outcome, api, domain = run_native(FakeBindings(creation=creation))
    expected = {"C1": CC.CREATE_CALL_FAILED, "P1": CC.PROCESS_CREATED, "CU": CC.CREATE_OUTCOME_UNCERTAIN}[creation]
    assert outcome.creation_classification is expected and api.counts["CreateProcessW"] == 1
    assert outcome.process_created is (creation == "P1")
    if creation != "P1":
        assert api.counts["ResumeThread"] == api.counts["IsProcessInJob"] == 0
        assert not any(name == "CloseHandle" and args[0] in {0xE001, 0xE002} for name, args in api.calls)
    if creation == "CU":
        assert domain.snapshot().state is LaunchAdmissionState.POISONED
        assert outcome.cleanup.job_empty and not outcome.cleanup_completed
        assert outcome.disposition is RuntimeDisposition.FAILED and not outcome.process_exited
        assert outcome.terminal_trigger is CleanupReason.CREATION_UNCERTAIN
        assert api.counts["TerminateJobObject"] == 1 and api.counts["WaitForSingleObject"] == 0
        assert api.counts["GetExitCodeProcess"] == 0
        with domain.quarantine_owner._ledger._condition:
            launch = domain.quarantine_owner.resource("launch")._payload
            assert launch._information.hProcess == 0xE001 and launch._information.hThread == 0xE002
            assert not launch._closed
        with pytest.raises(LaunchAdmissionError, match="poisoned"):
            run_native(api, domain=domain)
        assert api.counts["CreateProcessW"] == 1
    else:
        assert outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.OPEN


@pytest.mark.parametrize("interruption", ["cancel_active", "shutdown", "timeout"])
@pytest.mark.parametrize("creation", ["P1", "CU"])
def test_native_create_cancellation_or_deadline_cannot_erase_result(creation, interruption):
    api = FakeBindings(creation=creation)
    domain = LaunchAdmissionDomain(clock=api.clock)
    api.create_hook = (lambda: api.clock.advance(31)) if interruption == "timeout" else getattr(domain, interruption)
    outcome, _, _ = run_native(api, domain=domain)
    assert outcome.creation_classification is (CC.PROCESS_CREATED if creation == "P1" else CC.CREATE_OUTCOME_UNCERTAIN)
    assert api.counts["CreateProcessW"] == 1 and api.counts["ResumeThread"] == 0
    assert api.counts["TerminateJobObject"] == 1
    if creation == "CU":
        assert outcome.terminal_trigger is CleanupReason.CREATION_UNCERTAIN
        assert not outcome.cleanup_completed


@pytest.mark.parametrize("fault", [MemoryError, KeyboardInterrupt, SystemExit])
def test_known_native_success_commits_p1_before_fallible_attachment(monkeypatch, fault):
    def fail_attachment(self, payload):
        assert self.snapshot().process_created
        raise fault()

    monkeypatch.setattr(CreationReceipt, "attach_process", fail_attachment)
    outcome, api, domain = run_native()
    assert outcome.creation_classification is CC.PROCESS_CREATED and outcome.process_created
    assert api.counts["CreateProcessW"] == 1 and api.counts["ResumeThread"] == 0
    assert api.counts["TerminateJobObject"] == 1
    assert not outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.POISONED


@pytest.mark.parametrize("commit", ["record_success", "record_failed"])
def test_unavailable_immediate_commit_stays_cu(monkeypatch, commit):
    def unavailable(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(CreationReceipt, commit, unavailable)
    api = FakeBindings(creation="P1" if commit == "record_success" else "C1")
    outcome, api, domain = run_native(api)
    assert outcome.creation_classification is CC.CREATE_OUTCOME_UNCERTAIN
    assert api.counts["CreateProcessW"] == 1 and api.counts["ResumeThread"] == 0
    assert api.counts["TerminateJobObject"] == 1
    assert not any(name == "CloseHandle" and args[0] in {0xE001, 0xE002} for name, args in api.calls)
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


@pytest.mark.parametrize("failure", [0, 2, 0xFFFFFFFF])
def test_native_resume_count_is_checked_once(failure):
    api = FakeBindings()
    api.suspend_count = failure
    outcome, _, _ = run_native(api)
    assert outcome.process_created and not outcome.process_resumed
    assert api.counts["ResumeThread"] == 1 and api.counts["CreateProcessW"] == 1
    assert outcome.disposition is RuntimeDisposition.FAILED


@pytest.mark.parametrize("path", [r"\\server\share\a.exe", r"\\?\UNC\server\share\a.exe",
                                  r"\\.\C:\a.exe", r"\\?\Volume{abc}\a.exe", r"\??\C:\a.exe",
                                  r"C:\dir\..\a.exe", r"C:\a.exe:stream"])
def test_unsupported_final_path_forms_fail_closed(path):
    with pytest.raises((ValueError, TypeError)):
        W.normalize_final_path(path)


def test_only_prefix_and_drive_letter_are_normalized():
    assert W.normalize_final_path(r"\\?\c:\Tools\tool.exe") == r"C:\Tools\tool.exe"
    api = FakeBindings()
    api.final_paths["executable"] = r"\\?\C:\tools\tool.exe"
    outcome, _, _ = run_native(api)
    assert outcome.creation_classification is CC.PRE_CREATE and api.counts["CreateProcessW"] == 0


@pytest.mark.parametrize("drive", [0, 1, 4])
def test_native_drive_locality_rejects_unknown_remote_and_unsupported(drive):
    api = FakeBindings()
    api.drive_type = drive
    outcome, _, _ = run_native(api)
    assert outcome.creation_classification is CC.PRE_CREATE and api.counts["CreateProcessW"] == 0


@pytest.mark.parametrize("security_failure", ["client", "impersonation"])
def test_unexpected_client_or_identity_fails_closed_before_creation(security_failure):
    api = FakeBindings()
    if security_failure == "client":
        api.client_pid = 123
    else:
        api.impersonating = True
    outcome, _, _ = run_native(api)
    assert outcome.creation_classification is CC.PRE_CREATE
    assert api.counts["CreateProcessW"] == 0
    assert outcome.disposition is RuntimeDisposition.FAILED


# Every native setup/acquisition stage supports known error or unavailable return.
SETUP_FAILURES = [
    ("CreateFileW", 1, W.INVALID_HANDLE_VALUE), ("GetFileInformationByHandle", 1, 0),
    ("GetFinalPathNameByHandleW", 1, 0), ("ReadFile", 1, 0), ("CreateFileW", 2, W.INVALID_HANDLE_VALUE),
    ("CreateJobObjectW", 1, None), ("SetInformationJobObject", 1, 0),
    ("OpenProcessToken", 1, 0), ("GetTokenInformation", 2, 0),
    ("ConvertSidToStringSidW", 1, 0), ("ConvertStringSecurityDescriptorToSecurityDescriptorW", 1, 0),
    ("CreateFileW", 3, W.INVALID_HANDLE_VALUE), ("DuplicateHandle", 1, 0),
    ("CreateNamedPipeW", 1, W.INVALID_HANDLE_VALUE), ("CreateFileW", 4, W.INVALID_HANDLE_VALUE),
    ("DuplicateHandle", 2, 0), ("CreateEventW", 1, None), ("HeapAlloc", 1, None),
    ("CreateNamedPipeW", 2, W.INVALID_HANDLE_VALUE), ("CreateFileW", 5, W.INVALID_HANDLE_VALUE),
    ("DuplicateHandle", 3, 0), ("CreateEventW", 2, None), ("HeapAlloc", 2, None),
    ("ResetEvent", 1, 0), ("ConnectNamedPipe", 1, 0), ("GetNamedPipeClientProcessId", 1, 0),
    ("InitializeProcThreadAttributeList", 1, 1), ("HeapAlloc", 3, None),
    ("InitializeProcThreadAttributeList", 2, 0), ("UpdateProcThreadAttribute", 1, 0),
    ("UpdateProcThreadAttribute", 2, 0),
]


@pytest.mark.parametrize("name,ordinal,result", SETUP_FAILURES)
@pytest.mark.parametrize("uncertain", [False, True])
def test_partial_native_setup_failure_has_no_creation_and_keeps_obligations(name, ordinal, result, uncertain):
    api = FakeBindings()
    api.failures[(name, ordinal)] = KeyboardInterrupt() if uncertain else (result, 5)
    outcome, api, domain = run_native(api)
    assert api.counts["CreateProcessW"] == api.counts["ResumeThread"] == 0
    assert outcome.creation_classification is CC.PRE_CREATE
    assert outcome.disposition is RuntimeDisposition.FAILED
    if not outcome.cleanup_completed:
        assert domain.snapshot().state is LaunchAdmissionState.POISONED
        assert domain.quarantine_owner is not None
    assert all(not read["cancelled"] or outcome.cleanup.pending_operations for read in api.reads.values())


@pytest.mark.parametrize("name,ordinal,result", [("IsProcessInJob", 1, 0), ("ResumeThread", 1, 0xFFFFFFFF),
                                              ("QueryInformationJobObject", 1, 0), ("GetExitCodeProcess", 1, 0)])
def test_post_creation_api_failure_permanently_retains_p1(name, ordinal, result):
    api = FakeBindings()
    api.failures[(name, ordinal)] = (result, 5)
    outcome, _, _ = run_native(api)
    assert outcome.creation_classification is CC.PROCESS_CREATED
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert api.counts["CreateProcessW"] == 1 and api.counts["ResumeThread"] <= 1


@pytest.mark.parametrize("name", ["CloseHandle", "DeleteProcThreadAttributeList", "HeapFree", "LocalFree"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_native_release_failure_quarantines_and_never_blindly_retries(name, uncertain):
    api = FakeBindings()
    if name == "DeleteProcThreadAttributeList" and not uncertain:
        # VOID API cannot report known FALSE; only unavailable return is meaningful.
        api.failures[(name, 1)] = RuntimeError()
    else:
        api.failures[(name, 1)] = KeyboardInterrupt() if uncertain else (123 if name == "LocalFree" else 0, 5)
    outcome, _, domain = run_native(api)
    assert outcome.disposition is RuntimeDisposition.FAILED and not outcome.cleanup_completed
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    uncertain_resources = [r for r in outcome.cleanup.resources if r.state in {OwnershipState.RELEASE_UNCERTAIN, OwnershipState.QUARANTINED}]
    assert uncertain_resources
    assert all(r.release_attempted for r in uncertain_resources if r.pending_operations == 0)
    if name == "CloseHandle":
        first = next(args[0] for fn, args in api.calls if fn == name)
        assert sum(fn == name and args[0] == first for fn, args in api.calls) == 1
        assert api.counts["ResumeThread"] == 0


def test_unresolved_overlapped_memory_is_not_freed_or_closed():
    api = FakeBindings()
    api.outputs = {"stdout": deque(), "stderr": deque()}
    api.cancel_unresolved = True
    outcome, api, domain = run_native(api)
    assert outcome.cleanup.pending_operations == 2 and not outcome.cleanup_completed
    assert outcome.execution.failure_stage == "cleanup"
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    assert len(api.reads) == 2 and len(api.allocations) == 2
    assert api.counts["CancelIoEx"] == 2
    for handle, read in api.reads.items():
        assert not api.handles[handle]["closed"]
        address = C.addressof(pointer(read["overlapped"], W.OVERLAPPED).contents)
        assert address in api.allocations


def test_successful_zero_byte_read_is_not_eof():
    api = FakeBindings()
    api.outputs["stdout"] = deque([b"", b"next", None])
    outcome, _, _ = run_native(api)
    assert outcome.execution.stdout.text == "next" and outcome.execution.stdout.complete
    assert outcome.disposition is RuntimeDisposition.COMPLETED


def test_native_wrong_thread_rejected_before_any_binding_call():
    api = FakeBindings()
    backend = W.Win32TerminalBackend(owner_thread=Thread(), bindings=api)
    domain = LaunchAdmissionDomain(clock=api.clock)
    runtime = WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=Thread())
    with pytest.raises(TerminalLifecycleError):
        runtime.execute(target_for())
    with pytest.raises(TerminalLifecycleError):
        backend.create_job(None)
    assert api.calls == [] and domain.snapshot().state is LaunchAdmissionState.OPEN


@pytest.mark.parametrize("relative", ["capabilities/terminal_win32.py", "capabilities/terminal_runtime.py", "capabilities/terminal_handler.py"])
def test_import_safety_with_api_loader_and_registration_sentinels(monkeypatch, relative):
    import core.runtime as registry
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("import attempted native acquisition/registration")

    monkeypatch.setattr(C, "WinDLL", forbidden)
    monkeypatch.setattr(registry.CapabilityRuntimeRegistry, "register", forbidden)
    path = Path(__file__).parents[1] / relative
    module_name = "_d1b_import_sentinel_" + path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[module_name]
    assert calls == []


def test_live_loader_sentinel_is_installed_without_constructing_live_backend():
    with pytest.raises(AssertionError, match="MUST NOT"):
        W._load_live_bindings()
    # This calls only a failing sentinel, never a live loader or live constructor.


def test_native_sources_have_no_fallback_reader_threads_or_production_reachability():
    root = Path(__file__).parents[1]
    native = (root / "capabilities/terminal_win32.py").read_text(encoding="utf8")
    for forbidden in ("AssignProcessToJobObject", "CREATE_BREAKAWAY", "subprocess", "ShellExecute",
                      "PeekNamedPipe", "CreateIoCompletionPort", "ReadFileEx", "__del__"):
        assert forbidden not in native
    tree = ast.parse(native)
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "Thread" for node in ast.walk(tree))
    for relative in ("core/bootstrap.py", "core/action_worker.py", "core/runtime.py", "core/planner.py",
                     "core/orchestrator.py", "jarvis_v0_2_6.py", "integrations/local_approval_defaults.py"):
        source = (root / relative).read_text(encoding="utf-8-sig")
        for forbidden in ("terminal_win32", "terminal_handler", "WindowsTerminalRuntime", "Win32TerminalBackend"):
            assert forbidden not in source


@pytest.mark.parametrize("creation", ["C1", "P1"])
def test_scalar_result_commit_precedes_any_receipt_snapshot_or_output_inspection(monkeypatch, creation):
    api = FakeBindings(creation=creation)
    original_snapshot = CreationReceipt.snapshot
    original_raw = W._CreationHandle._raw

    def guarded_snapshot(receipt):
        assert receipt._success or receipt._failed, "snapshot happened before known result commit"
        return original_snapshot(receipt)

    def guarded_raw(token):
        assert token._launch._receipt._success, "output inspection preceded P1"
        return original_raw(token)

    # Install at the fake native return boundary, after all preflight evidence.
    api.create_hook = lambda: monkeypatch.setattr(CreationReceipt, "snapshot", guarded_snapshot)
    monkeypatch.setattr(W._CreationHandle, "_raw", guarded_raw)
    outcome, _, _ = run_native(api)
    assert outcome.creation_classification is (CC.PROCESS_CREATED if creation == "P1" else CC.CREATE_CALL_FAILED)
    assert api.counts["CreateProcessW"] == 1


def test_creation_storage_slots_job_and_typed_buffers_are_prebound_before_call(monkeypatch):
    api = FakeBindings()
    domain = LaunchAdmissionDomain(clock=api.clock)
    original = api._CreateProcessW

    def inspect_before_call(*args):
        invocation = domain._active
        launch = invocation.resources.resource("launch")._payload
        assert invocation.creation.snapshot().result_pending
        assert invocation.resources.resource("job").snapshot().state is OwnershipState.OWNED
        assert invocation.resources.resource("attributes").snapshot().state is OwnershipState.OWNED
        assert invocation.resources.resource("root-process").snapshot().state is OwnershipState.RESERVED
        assert invocation.resources.resource("primary-thread").snapshot().state is OwnershipState.RESERVED
        assert launch._process._launch is launch and launch._thread._launch is launch
        assert launch._information.hProcess is None and launch._information.hThread is None
        assert launch._application is not None and launch._command is not None and launch._environment is not None
        assert isinstance(args[4], W.BOOL) and isinstance(args[5], W.DWORD)
        assert launch._attributes._initialized

        def forbidden_identity(*args):
            raise AssertionError("post-create conceptual ownership allocation")

        monkeypatch.setattr(domain, "_resource_identity_unlocked", forbidden_identity)
        return original(*args)

    api._CreateProcessW = inspect_before_call
    outcome, _, _ = run_native(api, domain=domain)
    assert outcome.disposition is RuntimeDisposition.COMPLETED


def test_modified_native_command_buffer_never_changes_approved_target():
    api = FakeBindings()
    target = target_for(api.image, argv=("", "🌍", "a b"))
    original = api._CreateProcessW

    def modify(*args):
        result = original(*args)
        pointer(args[1], W.WCHAR)[0] = ord("X")
        return result

    api._CreateProcessW = modify
    outcome, _, _ = run_native(api, target=target)
    assert outcome.disposition is RuntimeDisposition.COMPLETED
    assert target.executable_resolved == r"C:\Tools\tool.exe" and target.argv == ("", "🌍", "a b")
    assert api.launch_record["command"] == serialize_windows_command_line(target.executable_resolved, target.argv)


@pytest.mark.parametrize("empty", [True, False])
def test_cu_containment_uses_only_preexisting_job_and_never_heals(empty):
    api = FakeBindings(creation="CU")
    api.keep_job = not empty
    outcome, api, domain = run_native(api)
    job = api.launch_record["attributes"][W.PROC_THREAD_ATTRIBUTE_JOB_LIST][0]
    assert all(args[0] == job for name, args in api.calls
               if name in {"TerminateJobObject", "QueryInformationJobObject"})
    assert not any(name in {"ResumeThread", "GetProcessId", "GetThreadId", "GetExitCodeProcess", "IsProcessInJob"}
                   for name, args in api.calls)
    assert not any(name == "CloseHandle" and args[0] in {0xE001, 0xE002, job} for name, args in api.calls)
    assert outcome.cleanup.job_empty is empty
    assert outcome.creation_classification is CC.CREATE_OUTCOME_UNCERTAIN
    assert outcome.disposition is RuntimeDisposition.FAILED and not outcome.cleanup_completed
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_membership_failure_after_root_exit_still_fails_and_requests_known_job_termination():
    api = FakeBindings()
    original = api._IsProcessInJob

    def member(process, job, result):
        if api.counts["IsProcessInJob"] == 2:
            pointer(result, W.BOOL)[0] = 0
            return 1
        return original(process, job, result)

    api._IsProcessInJob = member
    outcome, _, _ = run_native(api)
    assert api.counts["IsProcessInJob"] == 2 and api.counts["TerminateJobObject"] == 1
    assert outcome.terminal_trigger is CleanupReason.ROOT_EXIT and outcome.cleanup_completed
    assert outcome.disposition is RuntimeDisposition.FAILED


@pytest.mark.parametrize("method,arguments", [
    ("open_executable", (None, r"C:\tool.exe")), ("open_cwd", (None, r"C:\Work")),
    ("file_evidence", (None,)), ("read_executable", (None, 16384)), ("create_job", (None,)),
    ("configure_job", (None,)), ("make_stdio", (None,)), ("connect_pipe", (None,)),
    ("create_process", (None, None)), ("membership", (None, None)), ("resume", (None,)),
    ("submit_read", (None,)), ("inspect_read", (None,)), ("cancel_read", (None,)),
    ("root_exited", (None,)), ("exit_code", (None,)), ("job_active", (None,)),
    ("terminate_job", (None,)), ("wait", (None, (), 0.01)), ("release", (None,)),
])
def test_native_primitives_reject_nonowner_before_binding_calls(method, arguments):
    api = FakeBindings()
    backend = W.Win32TerminalBackend(owner_thread=Thread(), bindings=api)
    with pytest.raises(TerminalLifecycleError, match="owner thread"):
        getattr(backend, method)(*arguments)
    assert api.calls == []



def test_approved_drive_letter_case_normalizes_only_for_verification_not_launch_authority():
    api = FakeBindings()
    target = replace(target_for(api.image), executable_resolved=r"c:\Tools\tool.exe", cwd=r"c:\Work")
    outcome, _, _ = run_native(api, target=target)
    assert outcome.disposition is RuntimeDisposition.COMPLETED
    assert api.launch_record["application"] == target.executable_resolved
    assert api.launch_record["cwd"] == target.cwd


@pytest.mark.parametrize("operation", ["TerminateJobObject", "QueryInformationJobObject"])
def test_cu_recovery_failure_retains_known_job_and_untrusted_creation_storage(operation):
    api = FakeBindings(creation="CU")
    api.keep_job = True
    api.failures[(operation, 1)] = (0, 5)
    outcome, api, domain = run_native(api)
    assert outcome.creation_classification is CC.CREATE_OUTCOME_UNCERTAIN
    assert outcome.terminal_trigger is CleanupReason.CREATION_UNCERTAIN
    assert api.counts["CreateProcessW"] == 1 and api.counts["ResumeThread"] == 0
    assert domain.quarantine_owner.resource("job").snapshot().state is OwnershipState.QUARANTINED
    assert domain.quarantine_owner.resource("launch").snapshot().state is OwnershipState.QUARANTINED
    assert not outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.POISONED


def test_native_error_carries_known_last_error_without_calling_error_formatter():
    api = FakeBindings()
    api.failures[("SetInformationJobObject", 1)] = (0, 87)

    def work(owner):
        from capabilities.terminal_runtime import BackendResourceScope
        domain = LaunchAdmissionDomain(clock=api.clock)
        invocation = domain.admit()
        scope = BackendResourceScope(invocation, checkpoint=lambda: None)
        backend = W.Win32TerminalBackend(owner_thread=owner, bindings=api)
        job = backend.create_job(scope)
        with invocation.resources.borrow(job) as lease:
            with pytest.raises(W.NativeCallError) as error:
                backend.configure_job(lease.payload)
        assert error.value.operation == "SetInformationJobObject" and error.value.code == 87
        invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
        payload = invocation.resources.begin_release(job)
        assert backend.release(payload)
        invocation.resources.confirm_released(job)
        invocation.finish_cleanup()
        return invocation.outcome()

    assert on_owner(work).cleanup_completed



@pytest.mark.parametrize("drive", [W.DRIVE_REMOVABLE, W.DRIVE_FIXED, W.DRIVE_CDROM, W.DRIVE_RAMDISK])
def test_documented_local_disk_drive_types_preserve_approved_local_path_policy(drive):
    api = FakeBindings()
    api.drive_type = drive
    outcome, _, _ = run_native(api)
    assert outcome.disposition is RuntimeDisposition.COMPLETED
