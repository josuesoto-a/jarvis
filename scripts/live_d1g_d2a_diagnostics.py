"""D1G-D2a: explicit live Win32 diagnostics; never a process-launch smoke test.

Run with the official x64 Python using -I -S -B. No site initialization or
third-party dependencies are needed. Guards precede all optional imports.
No runtime.execute(), backend.create_process(), registration, or approval wiring.
An unresolved native cleanup writes STOP evidence and keeps its owner alive.
"""
from __future__ import annotations

import sys


class CreationForbidden(RuntimeError):
    pass


GUARDS = {
    "installed_before_optional_imports": True,
    "python_launcher_attempts": {},
    "child_audit_events": [],
    "platform_api_attempts": [],
    "real_platform_module_loaded_at_start": "platform" in sys.modules,
    "real_subprocess_module_loaded_at_start": "subprocess" in sys.modules,
}


def prohibit_children(event, args):
    if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
        GUARDS["child_audit_events"].append(event)
        raise CreationForbidden("D2a child-process audit guard: " + event)
    if event == "import" and args[0].split(".")[0] in {"subprocess", "platform", "multiprocessing", "wmi", "win32com", "pythoncom", "win32process"}:
        raise CreationForbidden("D2a forbids importing real helper-capable module: " + args[0])


sys.addaudithook(prohibit_children)
if GUARDS["real_platform_module_loaded_at_start"] or GUARDS["real_subprocess_module_loaded_at_start"]:
    raise CreationForbidden("D2a requires isolated -I -S startup without preloaded helper modules")

# os and _winapi are in-process modules; replace launch functions before any
# optional diagnostic imports. Never import or call the real subprocess module.
import os
from types import ModuleType
import _winapi


def forbidden_launcher(name):
    def blocked(*args, **kwargs):
        attempts = GUARDS["python_launcher_attempts"]
        attempts[name] = attempts.get(name, 0) + 1
        raise CreationForbidden("D2a Python launcher guard: " + name)
    return blocked


for _name in ("system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
              "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty"):
    if hasattr(os, _name):
        setattr(os, _name, forbidden_launcher("os." + _name))
_winapi.CreateProcess = forbidden_launcher("_winapi.CreateProcess")
_subprocess_guard = ModuleType("subprocess")
for _name in ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
    setattr(_subprocess_guard, _name, forbidden_launcher("subprocess." + _name))
sys.modules["subprocess"] = _subprocess_guard


def forbidden_platform_attribute(name):
    if name.startswith("__"):
        raise AttributeError(name)
    GUARDS["platform_api_attempts"].append(name)
    raise CreationForbidden("D2a forbids platform APIs: " + name)


# terminal.py has an inert `import platform`. Supply a fail-fast sentinel so no
# real platform module or API is used, even by accidentally reached code.
_platform_guard = ModuleType("platform")
_platform_guard.__getattr__ = forbidden_platform_attribute
sys.modules["platform"] = _platform_guard

import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes as C
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import struct
from threading import Event, Thread, current_thread
from time import monotonic, time as utc_epoch
import traceback


OFFICIAL_PYTHON = r"C:\Users\josue\AppData\Local\Programs\Python\Python311\python.exe"
EXPECTED_SHA256 = "5f7b89a612c9b8af1d6456cdfcd1dbe5ca630849e79aebced9bee9a6694952ec"
ROOT = Path(__file__).resolve().parents[1]
SMOKE_ARGV = ("-I", "-S", "-c", 'import sys; sys.stdout.write("D2b stdout\\n"); sys.stderr.write("D2b stderr\\n")')


class Tripwire:
    """No native callable is retained by this replacement."""
    def __init__(self):
        self.attempts = 0
        self.native_invocations = 0
        self.resolved_symbols = []

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        raise CreationForbidden("D2a forbids CreateProcessW before native dispatch")


CREATION_TRIPWIRE = Tripwire()
_original_windll = C.WinDLL
_original_cdll = C.CDLL


class GuardedDLL:
    """Resolve launch symbols but expose only non-forwarding Python guards."""
    def __init__(self, dll):
        self._dll = dll

    def __getattr__(self, name):
        if name == "CreateProcessW":
            native = getattr(self._dll, name)
            if not C.cast(native, C.c_void_p).value:
                raise CreationForbidden("CreateProcessW symbol could not resolve")
            CREATION_TRIPWIRE.resolved_symbols.append(name)
            # The raw function is not returned, captured, or retained by a guard.
            self.CreateProcessW = CREATION_TRIPWIRE
            return CREATION_TRIPWIRE
        if name in {"CreateProcessA", "CreateProcessAsUserW", "CreateProcessAsUserA", "CreateProcessWithLogonW", "CreateProcessWithTokenW",
                    "ShellExecuteW", "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA", "WinExec", "ResumeThread", "TerminateJobObject"}:
            native = getattr(self._dll, name)  # Symbol availability only.
            del native
            guard = forbidden_launcher("native." + name)
            setattr(self, name, guard)
            return guard
        return getattr(self._dll, name)


def guarded_windll(*args, **kwargs):
    return GuardedDLL(_original_windll(*args, **kwargs))


def guarded_cdll(*args, **kwargs):
    return GuardedDLL(_original_cdll(*args, **kwargs))


C.WinDLL, C.CDLL = guarded_windll, guarded_cdll
C.windll, C.cdll = C.LibraryLoader(guarded_windll), C.LibraryLoader(guarded_cdll)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run_diagnostics(T, R, W, report, retained):
    # These extra diagnostic-only APIs inspect security, inheritance and deliver
    # in-process pipe bytes. Their ABI is explicit, just like the backend APIs.
    extras = {
        "WriteFile": (W.BOOL, [W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.OVERLAPPED)]),
        "GetHandleInformation": (W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]),
        "IsWow64Process2": (W.BOOL, [W.HANDLE, C.POINTER(W.WORD), C.POINTER(W.WORD)]),
        "GetSecurityInfo": (W.DWORD, [W.HANDLE, C.c_int32, W.DWORD, C.POINTER(W.LPVOID), C.POINTER(W.LPVOID), C.POINTER(W.LPVOID), C.POINTER(W.LPVOID), C.POINTER(W.LPVOID)]),
        "GetSecurityDescriptorControl": (W.BOOL, [W.LPVOID, C.POINTER(W.WORD), C.POINTER(W.DWORD)]),
        "GetSecurityDescriptorDacl": (W.BOOL, [W.LPVOID, C.POINTER(W.BOOL), C.POINTER(W.LPVOID), C.POINTER(W.BOOL)]),
        "GetAce": (W.BOOL, [W.LPVOID, W.DWORD, C.POINTER(W.LPVOID)]),
        "EqualSid": (W.BOOL, [W.LPVOID, W.LPVOID]),
    }
    security_apis = {"GetSecurityInfo", "GetSecurityDescriptorControl", "GetSecurityDescriptorDacl", "GetAce", "EqualSid"}
    tripwire = CREATION_TRIPWIRE
    calls = Counter()
    acquisitions = Counter()
    releases = Counter()
    live = {"handles": set(), "heap": set(), "local": set(), "attributes": set()}
    pipe_creations = []
    security_sddl = []
    native_errors = []
    io_observations = []
    locality_observations = []

    def output_value(argument):
        return C.cast(argument, C.POINTER(W.HANDLE))[0]

    def observe(name, function):
        def observed(*args):
            calls[name] += 1
            value = function(*args)
            error = C.get_last_error()
            if name in {"CreateFileW", "CreateJobObjectW", "CreateNamedPipeW", "CreateEventW"}:
                if value not in {None, 0, W.INVALID_HANDLE_VALUE}:
                    live["handles"].add(value)
                    acquisitions["handles"] += 1
            elif name == "DuplicateHandle" and value:
                live["handles"].add(output_value(args[3]))
                acquisitions["handles"] += 1
            elif name in {"OpenProcessToken", "OpenThreadToken"} and value:
                live["handles"].add(output_value(args[-1]))
                acquisitions["handles"] += 1
            elif name == "HeapAlloc" and value:
                live["heap"].add(value)
                acquisitions["heap"] += 1
            elif name in {"ConvertSidToStringSidW", "ConvertStringSecurityDescriptorToSecurityDescriptorW"} and value:
                live["local"].add(output_value(args[1 if name == "ConvertSidToStringSidW" else 2]))
                acquisitions["local"] += 1
            elif name == "GetSecurityInfo" and value == 0:
                live["local"].add(output_value(args[-1]))
                acquisitions["local"] += 1
            elif name == "InitializeProcThreadAttributeList" and args[0] and value:
                live["attributes"].add(args[0])
                acquisitions["attributes"] += 1
            elif name == "CloseHandle" and value:
                live["handles"].remove(args[0])
                releases["handles"] += 1
            elif name == "HeapFree" and value:
                live["heap"].remove(args[2])
                releases["heap"] += 1
            elif name == "LocalFree" and not value:
                live["local"].remove(args[0])
                releases["local"] += 1
            elif name == "DeleteProcThreadAttributeList":
                live["attributes"].remove(args[0])
                releases["attributes"] += 1
            if name == "CreateNamedPipeW":
                pipe_creations.append({"name": C.wstring_at(args[0]), "open_mode": args[1], "pipe_mode": args[2], "max_instances": args[3]})
            if name == "GetDriveTypeW":
                locality_observations.append({"root": C.wstring_at(args[0]), "drive_type": value})
            if name in {"ReadFile", "CancelIoEx", "GetOverlappedResult"}:
                io_observations.append({"api": name, "success": bool(value), "error": 0 if value else error})
            if name == "ConvertStringSecurityDescriptorToSecurityDescriptorW":
                security_sddl.append(C.wstring_at(args[0]))
            if name in {"CreateFileW", "OpenThreadToken", "InitializeProcThreadAttributeList", "ReadFile", "ConnectNamedPipe", "GetOverlappedResult"} and (not value or value == W.INVALID_HANDLE_VALUE):
                native_errors.append({"api": name, "last_error": error})
            C.set_last_error(error)
            return value
        return observed

    original_loader = W._load_live_bindings
    original_target_host_check = T._validate_windows_host
    original_backend_host_check = W._validate_windows_host
    native_host = {}

    def direct_host_check():
        # Same production host policy, substituting directly measured native
        # architecture for platform.machine(). Only these isolated module aliases
        # are adapted; no production source, contracts, or policy fields change.
        version = sys.getwindowsversion()
        require(sys.platform == "win32" and version.major == 10 and version.build >= 10240
                and version.product_type == 1 and sys.maxsize > 2**32
                and C.sizeof(C.c_void_p) == 8 and C.sizeof(C.c_wchar) == 2
                and native_host.get("process_machine") == 0 and native_host.get("native_machine") == 0x8664,
                "unsupported Windows 10/11 native x64 workstation / ABI")

    def guarded_loader():
        bindings = original_loader()
        # Resolve only. Drop every exposed callable reference before returning
        # bindings to the backend or preparing launch storage.
        require(bindings.CreateProcessW is tripwire and tripwire.resolved_symbols, "CreateProcessW symbol/guard did not resolve")
        report["create_process_resolved"] = True
        bindings.CreateProcessW = tripwire
        bindings._kernel.CreateProcessW = tripwire
        for name in W.API_SIGNATURES:
            if name != "CreateProcessW":
                setattr(bindings, name, observe(name, getattr(bindings, name)))
        for name, (result, arguments) in extras.items():
            function = getattr(bindings._advapi if name in security_apis else bindings._kernel, name)
            function.restype, function.argtypes = result, arguments
            setattr(bindings, name, observe(name, function))
        report["binding_symbols"] = sorted(W.API_SIGNATURES)
        return bindings

    domain = R.LaunchAdmissionDomain()
    invocation = domain.admit()
    ledger = invocation.resources

    def checkpoint():
        require(monotonic() < invocation.snapshot().execution_deadline, "diagnostic preflight deadline expired")

    scope = R.BackendResourceScope(invocation, checkpoint=checkpoint)
    # Retain the ledger before native acquisition, including unexpected harness
    # exceptions. The owner stays alive on any unresolved cleanup.
    retained.extend((domain, invocation, scope, live))
    backend = None
    target = None
    failure = None
    cleanup_failure = None

    def call(method, *resources, extra=()):
        with ExitStack() as stack:
            payloads = [stack.enter_context(ledger.borrow(resource)).payload for resource in resources]
            return method(*payloads, *extra)

    def io(stream, method):
        if stream.operation is None:
            stream.operation = ledger.begin_operation(stream.reader, stream.event, stream.memory)
        elif stream.operation.completed:
            stream.operation.rearm_for_drain()
        result = call(method, stream.memory)
        if result.completed:
            stream.operation.confirm_completed()
        return result

    def complete(stream):
        deadline = monotonic() + 2.0
        while True:
            result = io(stream, backend.inspect_read)
            if result.completed:
                return result
            require(monotonic() < deadline, "native I/O completion not established")
            call(lambda memory: backend.wait(None, (memory,), 0.02), stream.memory)

    def descriptor_ace(descriptor):
        control, revision = W.WORD(), W.DWORD()
        backend._bool("GetSecurityDescriptorControl", descriptor, C.byref(control), C.byref(revision))
        present, defaulted, dacl = W.BOOL(), W.BOOL(), W.LPVOID()
        backend._bool("GetSecurityDescriptorDacl", descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted))
        require(control.value & 0x1000 and present.value and dacl.value and not defaulted.value,
                "pipe DACL is not protected, explicit, and non-null")
        require(W.WORD.from_address(dacl.value + 4).value == 1, "pipe DACL must have exactly one ACE")
        ace = W.LPVOID()
        backend._bool("GetAce", dacl, 0, C.byref(ace))
        require(W.BYTE.from_address(ace.value).value == 0 and W.BYTE.from_address(ace.value + 1).value == 0,
                "unexpected pipe ACE type or inheritance flags")
        require(W.DWORD.from_address(ace.value + 4).value == 0x0012019F, "unexpected pipe ACE access mask")
        return ace.value + 8

    try:
        require(sys.executable == OFFICIAL_PYTHON, "run diagnostics with the exact official Python candidate")
        require(sys.platform == "win32" and C.sizeof(W.HANDLE) == 8 and C.sizeof(C.c_wchar) == 2,
                "unsupported Windows native ABI")
        # Query architecture in-process before adapting the two diagnostic-local
        # host-validator aliases. All platform APIs remain prohibited.
        host_kernel = C.WinDLL("kernel32", use_last_error=True)
        retained.append(host_kernel)
        current_process = host_kernel.GetCurrentProcess
        current_process.restype, current_process.argtypes = W.HANDLE, []
        architecture = host_kernel.IsWow64Process2
        architecture.restype, architecture.argtypes = extras["IsWow64Process2"]
        process_machine, native_machine = W.WORD(), W.WORD()
        require(observe("IsWow64Process2", architecture)(observe("GetCurrentProcess", current_process)(),
                C.byref(process_machine), C.byref(native_machine)), "native host architecture query failed")
        require(process_machine.value == 0 and native_machine.value == 0x8664, "unsupported WOW64/emulated host")
        native_host.update(process_machine=process_machine.value, native_machine=native_machine.value)
        T._validate_windows_host = W._validate_windows_host = direct_host_check
        T._validate_windows_host()
        require(C.sizeof(W.HANDLE) == 8 and C.sizeof(C.c_wchar) == 2, "unsupported native ABI")
        version = sys.getwindowsversion()
        report["host"] = {"windows": list(version), "product_type": version.product_type,
                          "architecture": "AMD64 (IsWow64Process2 native evidence)", "python": sys.version,
                          "implementation": sys.implementation.name, "version_info": list(sys.version_info),
                          "maxsize": sys.maxsize, "host_check": "diagnostic-local direct-native adapter; same host policy",
                          "interpreter": sys.executable, "pointer_bytes": C.sizeof(W.HANDLE), "wchar_bytes": C.sizeof(C.c_wchar)}
        W._load_live_bindings = guarded_loader
        try:
            backend = W.Win32TerminalBackend(owner_thread=current_thread())
        finally:
            W._load_live_bindings = original_loader
        report["live_backend_constructed"] = True
        retained.append(backend)
        process_machine, native_machine = W.WORD(), W.WORD()
        backend._bool("IsWow64Process2", backend._api.GetCurrentProcess(), C.byref(process_machine), C.byref(native_machine))
        require(process_machine.value == 0 and native_machine.value == 0x8664, "unsupported WOW64/emulated host")
        expected_layouts = {"SECURITY_ATTRIBUTES": 24, "STARTUPINFOW": 104, "STARTUPINFOEXW": 112,
                            "PROCESS_INFORMATION": 24, "OVERLAPPED": 32, "JOBOBJECT_BASIC_LIMIT_INFORMATION": 64,
                            "IO_COUNTERS": 48, "JOBOBJECT_EXTENDED_LIMIT_INFORMATION": 144,
                            "JOBOBJECT_BASIC_ACCOUNTING_INFORMATION": 48, "BY_HANDLE_FILE_INFORMATION": 52,
                            "SID_AND_ATTRIBUTES": 16, "TOKEN_GROUPS": 24}
        actual_layouts = {name: C.sizeof(getattr(W, name)) for name in expected_layouts}
        require(actual_layouts == expected_layouts, "Windows x64 structure layouts differ")
        report["host"].update({"process_machine": process_machine.value, "native_machine": native_machine.value,
                               "abi_structure_sizes": actual_layouts})
        for blocked in (backend._api.CreateProcessW, backend._api._kernel.CreateProcessW):
            try:
                blocked()
            except CreationForbidden:
                pass
            else:
                raise RuntimeError("creation tripwire failed")

        executable = backend.open_executable(scope, OFFICIAL_PYTHON)
        initial = call(backend.file_evidence, executable)
        require(initial.final_path == OFFICIAL_PYTHON and initial.disk and not initial.directory,
                "official Python native final-path/type mismatch")
        require(0 < initial.size <= T.TERMINAL_EXECUTABLE_BYTES_LIMIT, "unsupported executable size")
        digest = sha256()
        header = call(backend.read_executable, executable, extra=(T.TERMINAL_PE_HEADER_BYTES,))
        machine = T.inspect_windows_console_pe(header, file_size=initial.size)
        digest.update(header)
        total = len(header)
        while chunk := call(backend.read_executable, executable, extra=(65536,)):
            total += len(chunk)
            require(total <= T.TERMINAL_EXECUTABLE_BYTES_LIMIT, "native executable read exceeded limit")
            digest.update(chunk)
        final = call(backend.file_evidence, executable)
        require(initial == final and total == initial.size, "executable changed during held native verification")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        subsystem = struct.unpack_from("<H", header, pe_offset + 24 + 68)[0]
        report["executable"] = {**asdict(final), "sha256": digest.hexdigest(), "machine": machine,
                                "machine_hex": hex(machine), "subsystem": subsystem,
                                "open_access": W.GENERIC_READ, "share_mode": W.FILE_SHARE_READ}
        require(digest.hexdigest() == EXPECTED_SHA256 and machine == 0x8664 and subsystem == 3 and total == 103192,
                "fresh native executable evidence differs unexpectedly from the supplied candidate identity")
        # Request rights only; never write/delete. Both competing opens must fail
        # while the approved verification handle is held.
        for role, rights in (("write", W.FILE_WRITE_DATA), ("delete", 0x10000)):
            try:
                backend._handle(scope, "sharing-probe-" + role, backend._api.CreateFileW,
                                W._utf16(OFFICIAL_PYTHON), rights, 7, None, W.OPEN_EXISTING, 0, None,
                                invalid=W.INVALID_HANDLE_VALUE)
            except W.NativeCallError as error:
                require(error.code == 32, "restrictive sharing probe did not report sharing violation")
            else:
                raise RuntimeError("restrictive sharing admitted write/delete rights")
        report["executable"]["write_and_delete_opens_denied"] = True

        cwd = backend.open_cwd(scope, str(ROOT))
        cwd_evidence = call(backend.file_evidence, cwd)
        require(cwd_evidence.final_path == str(ROOT) and cwd_evidence.disk and cwd_evidence.directory,
                "cwd native final-path/type mismatch")
        report["cwd"] = asdict(cwd_evidence)
        target = T.prepare_terminal_execution(T.TerminalExecutionRequest(executable=OFFICIAL_PYTHON, argv=SMOKE_ARGV, cwd=str(ROOT)))
        target.validate()
        require(target.executable_identity == "sha256:" + digest.hexdigest(), "target/v2 and held native digest differ")
        report["target_v2"] = {"compatible": True, "version": target.contract_version,
                               "identity_matches_native": True, "activation_resource_policy": target.runtime_policy.job_resource_policy}

        job = backend.create_job(scope)
        call(backend.configure_job, job)
        job_policy = W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = W.DWORD()
        call(lambda token: backend._bool("QueryInformationJobObject", token._raw(), W.JobObjectExtendedLimitInformation,
                                       C.byref(job_policy), C.sizeof(job_policy), C.byref(returned)), job)
        require(returned.value == C.sizeof(job_policy) and job_policy.BasicLimitInformation.LimitFlags == W.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                "Job kill-on-close configuration query mismatch")
        require(call(backend.job_active, job) == 0, "new diagnostic Job is not empty")
        report["job"] = {"configured_flags": job_policy.BasicLimitInformation.LimitFlags, "active_processes": 0}
        report["job"]["policy_matches_d1b"] = True

        stdio = backend.make_stdio(scope)
        children = tuple(stdio.children)
        require(len(children) == 3, "future child handle count differs")
        child_values = set()
        for child in children:
            with ledger.borrow(child) as lease:
                child_values.add(lease.payload._raw())
        require(len(child_values) == 3, "future child handles are not distinct")
        inheritable_values = set()
        for resource in scope.ordered:
            with ledger.borrow(resource) as lease:
                token = lease.payload
                if isinstance(token, W._Handle) and token._acquired:
                    flags = W.DWORD()
                    backend._bool("GetHandleInformation", token._raw(), C.byref(flags))
                    if flags.value & 1:
                        inheritable_values.add(token._raw())
        require(inheritable_values == child_values, "inheritable handle set differs from exact three future children")
        report["future_child_handles"] = {"count": 3, "roles": ["stdin-child", "stdout-child", "stderr-child"], "exact_inheritable_set": True}
        report["nul"] = {"opened": True, "duplicated": True}

        with ledger.borrow(ledger.resource("pipe-security")) as lease:
            expected_sid = descriptor_ace(lease.payload._raw())
            for stream in scope.streams:
                descriptor = W._LocalMemory(backend)
                scope.own(stream.name + "-actual-security", descriptor)
                descriptor._attempted = True
                status = call(lambda token: backend._api.GetSecurityInfo(token._raw(), 6, 4, None, None, None, None,
                                                                        C.byref(descriptor._output)), stream.reader)
                descriptor._resolved = True
                descriptor._acquired = status == 0
                require(status == 0, "actual pipe security query failed")
                actual_sid = descriptor_ace(descriptor._raw())
                require(backend._api.EqualSid(expected_sid, actual_sid), "actual pipe ACE logon SID differs")
        require(len(security_sddl) == 1 and security_sddl[0].startswith("D:P(A;;0x0012019f;;;S-1-5-5-"), "unexpected logon SID policy")
        require(len(pipe_creations) == 2 and len({p["name"] for p in pipe_creations}) == 2
                and all(re.fullmatch(r"\\\\\.\\pipe\\jarvis-terminal-[0-9a-f]{32}", p["name"])
                                               and p["open_mode"] == W.PIPE_ACCESS_INBOUND | W.FILE_FLAG_OVERLAPPED | W.FILE_FLAG_FIRST_PIPE_INSTANCE
                                               and p["pipe_mode"] == W.PIPE_REJECT_REMOTE_CLIENTS and p["max_instances"] == 1 for p in pipe_creations),
                "local-only pipe creation flags differ")
        for stream in scope.streams:
            result = io(stream, backend.connect_pipe)
            if not result.completed:
                result = complete(stream)
            require(result.error is None, "pipe connection failed")
        require(calls["GetNamedPipeClientProcessId"] == 2, "pipe client identity was not checked")
        report["pipes"] = {"count": 2, "protected_dacl": True, "single_logon_sid_ace": True,
                           "ace_mask": hex(0x0012019F), "remote_clients_rejected": True,
                           "first_instance": True, "max_instances": 1, "noninheritable_parent_handles": True,
                           "connected_client_is_current_process": True, "actual_object_security_verified": True}
        report["pipes"]["native_creation_observations"] = pipe_creations

        launch = call(lambda job_token, *child_tokens: backend.prepare_launch(
            scope, target, T.serialize_windows_command_line(target.executable_resolved, target.argv),
            T.build_windows_environment_block(target.environment_mapping()), job_token, child_tokens), job, *children)
        with ledger.borrow(launch) as lease:
            token = lease.payload
            require(tuple(token._attributes._handles) == tuple(token._startup.StartupInfo.__getattribute__(field)
                    for field in ("hStdInput", "hStdOutput", "hStdError")), "attribute handle order differs from stdio")
            require(set(token._attributes._handles) == child_values and len(token._attributes._jobs) == 1,
                    "HANDLE_LIST/JOB_LIST content mismatch")
            with ledger.borrow(job) as job_lease:
                require(token._attributes._jobs[0] == job_lease.payload._raw(), "JOB_LIST does not contain the owned diagnostic Job")
            require(token._create_call is tripwire, "prepared creation callable bypasses tripwire")
            try:
                token._create_call(*token._call_arguments)
            except CreationForbidden:
                pass
            else:
                raise RuntimeError("prepared-call tripwire failed")
        report["attributes"] = {"initialized": True, "handle_list_count": 3, "job_list_count": 1,
                                "update_calls": calls["UpdateProcThreadAttribute"]}

        completed_reads = []
        cancelled_reads = []
        for stream in scope.streams:
            result = io(stream, backend.submit_read)
            require(not result.completed, "read was not pending before diagnostic in-process write")
            payload = ("D2a " + stream.name + " in-process bytes\n").encode("ascii")
            buffer, written = C.create_string_buffer(payload), W.DWORD()
            call(lambda writer: backend._bool("WriteFile", writer._raw(), buffer, len(payload), C.byref(written), None),
                 ledger.resource(stream.name + "-original"))
            require(written.value == len(payload), "diagnostic pipe write was incomplete")
            result = complete(stream)
            require(result.data == payload and not result.eof and result.error is None, "overlapped read byte mismatch")
            completed_reads.append({"stream": stream.name, "bytes": len(result.data), "completed": True})
            result = io(stream, backend.submit_read)
            require(not result.completed, "cancellation read was not pending")
            stream.operation.request_cancel()
            before_cancel_observation = len(io_observations)
            call(backend.cancel_read, stream.memory)
            require(not stream.operation.completed, "cancel request fabricated final completion")
            result = complete(stream)
            require(result.error == "read_cancelled" and result.completed, "cancellation final completion differs")
            cancel_observations = io_observations[before_cancel_observation:]
            final_observation = cancel_observations[-1]
            require(final_observation["api"] == "GetOverlappedResult" and not final_observation["success"]
                    and final_observation["error"] == W.ERROR_OPERATION_ABORTED,
                    "cancellation final native error observation differs")
            cancelled_reads.append({"stream": stream.name, "final_native_observation": final_observation,
                                    "completed": stream.operation.completed, "request_was_not_completion": True})
        report["overlapped_reads"] = completed_reads
        report["cancel_io_ex"] = cancelled_reads
        require(call(backend.job_active, job) == 0 and calls["Sleep"] == 0 and calls["ResumeThread"] == 0,
                "unexpected Job membership, sleep, or real process resume")
        require(tripwire.attempts == 3 and calls["CreateProcessW"] == 0 and tripwire.native_invocations == 0,
                "CreateProcessW zero-call proof failed")
    except BaseException:
        failure = traceback.format_exc()
    finally:
        T._validate_windows_host = original_target_host_check
        W._validate_windows_host = original_backend_host_check
        invocation.begin_cleanup(R.CleanupReason.PREFLIGHT_FAILURE)
        if backend is not None:
            # Cancel outstanding operations, then establish final completion
            # BEFORE any dependency is released. Never retry uncertain releases.
            for stream in scope.streams:
                if stream.operation is not None and not stream.operation.completed:
                    try:
                        stream.operation.request_cancel()
                        call(backend.cancel_read, stream.memory)
                        complete(stream)
                    except BaseException:
                        cleanup_failure = traceback.format_exc()
                        break
            if cleanup_failure is None:
                for resource in reversed(scope.ordered):
                    payload = ledger.begin_release(resource)
                    try:
                        released = backend.release(payload)
                    except BaseException:
                        ledger.mark_release_uncertain(resource)
                        cleanup_failure = traceback.format_exc()
                        break
                    if released is not True:
                        ledger.mark_still_owned(resource)
                        cleanup_failure = "native release not confirmed: " + resource.snapshot().name
                        break
                    ledger.confirm_released(resource)
        if cleanup_failure is not None:
            invocation.quarantine(R.QuarantineReason.INCOMPLETE_CLEANUP)
        elif any(live.values()):
            # Accounting disagreements are STOP, even if the ledger is clear.
            cleanup_failure = "native accounting retained allocations after ledger cleanup"
        else:
            invocation.finish_cleanup()
        if failure is None and cleanup_failure is None:
            require(acquisitions == releases and not any(live.values()) and invocation.cleanup_evidence().cleanup_completed,
                    "final native acquisition/release accounting differs")
        calls["CreateProcessW"] = tripwire.native_invocations
        snapshots = ledger.snapshot()
        report.update({"status": "PASS" if failure is None and cleanup_failure is None else "STOP",
                       "failure": failure, "cleanup_failure": cleanup_failure,
                       "real_win32_apis_called": bool(sum(calls.values())),
                       "create_process_called": False, "create_process_native_invocations": calls["CreateProcessW"],
                       "tripwire_attempt_count": tripwire.attempts,
                       "terminal_child_launched": False, "creation_receipt": asdict(invocation.creation.snapshot()),
                       "native_call_counts": dict(calls), "native_expected_error_observations": native_errors,
                       "native_io_observations": io_observations, "native_locality_observations": locality_observations,
                       "resources": [{"name": item.name, "state": item.state, "borrowers": item.borrowers,
                                      "pending_operations": item.pending_operations, "release_attempted": item.release_attempted,
                                      "generation": item.identity.generation, "kind": item.identity.kind} for item in snapshots],
                       "native_acquisitions": dict(acquisitions), "native_releases": dict(releases),
                       "native_remaining": {kind: len(values) for kind, values in live.items()},
                       "cleanup_completed": invocation.cleanup_evidence().cleanup_completed,
                       "domain_state": domain.snapshot().state,
                       "quarantined": domain.quarantine_owner is not None,
                       "poisoned": domain.snapshot().state is R.LaunchAdmissionState.POISONED,
                       "pending_io": invocation.cleanup_evidence().pending_operations,
                       "final_borrow_count": sum(item.borrowers for item in ledger.snapshot()),
                       "ledger_released_owners": sum(item.state is R.OwnershipState.RELEASED for item in snapshots),
                       "ledger_unacquired_reserved_slots": sum(item.state is R.OwnershipState.RESERVED for item in snapshots),
                       "d2b_performed": False})
        if report["status"] == "PASS":
            report["proposed_d2b_target"] = {
                "status": "prepared-proposal-only; no D2b authorization",
                "target": asdict(target), "approval_arguments": T.terminal_approval_arguments(target),
                "expected_exit_code": 0, "expected_stdout": "D2b stdout\n", "expected_stderr": "D2b stderr\n",
                "limitations": "D2a does not validate child creation, resume, containment, or process capture; production resource policy remains unresolved-no-activation/v1",
            }
    return domain, invocation, backend, scope, cleanup_failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,
            "start this diagnostic with -I -S -B")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    from capabilities import terminal as T, terminal_runtime as R, terminal_win32 as W

    report = {"phase": "D1G-D2a", "run_kind": "clean-revalidation",
              "superseded_previous_attempt": "previous_procedural_stop/d1g_d2a_result.json (STOP, not PASS)",
              "scope": "this isolated -I -S -B guarded diagnostic interpreter only; ordinary tests excluded",
              "diagnostic_pid": os.getpid(), "start_utc_epoch": utc_epoch(),
              "startup_flags": {"isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
                                "dont_write_bytecode": sys.flags.dont_write_bytecode},
              "live_backend_constructed": False, "create_process_resolved": False}
    retained = []
    hold = Event()

    def owner():
        try:
            retained.append(run_diagnostics(T, R, W, report, retained))
        except BaseException:
            report["status"] = "STOP"
            report["harness_error"] = traceback.format_exc()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({key: report.get(key) for key in (
            "status", "failure", "cleanup_failure", "harness_error", "tripwire_attempt_count",
            "create_process_native_invocations", "cleanup_completed", "domain_state", "native_remaining")}), flush=True)
        if report.get("cleanup_failure") or report.get("harness_error"):
            print("STOP: owner and retained native state remain alive; no further native calls.", flush=True)
            hold.wait()

    worker = Thread(target=owner, name="D2a-native-diagnostic-owner", daemon=False)
    worker.start()
    worker.join()
    report["owner_thread"] = {"explicit_non_main": True, "daemon": worker.daemon,
                               "joined": not worker.is_alive(), "exception_propagation": "captured owner failure is raised on main after join and saved STOP evidence"}
    report["child_creation_guards"] = GUARDS
    report["platform_module_used"] = False
    report["platform_api_calls"] = len(GUARDS["platform_api_attempts"])
    report["subprocess_module_invoked"] = False
    report["real_subprocess_module_imported"] = sys.modules.get("subprocess") is not _subprocess_guard
    report["real_platform_module_imported"] = sys.modules.get("platform") is not _platform_guard
    report["helper_child_count"] = 0
    report["terminal_child_count"] = 0
    report["diagnostic_external_executable_calls"] = 0
    report["guards_remained_installed"] = (_winapi.CreateProcess.__name__ == "blocked"
            and os.system.__name__ == "blocked" and os.popen.__name__ == "blocked"
            and C.WinDLL is guarded_windll and C.CDLL is guarded_cdll)
    guard_clean = (not GUARDS["python_launcher_attempts"] and not GUARDS["child_audit_events"]
            and not GUARDS["platform_api_attempts"] and not report["real_subprocess_module_imported"]
            and not report["real_platform_module_imported"] and report["guards_remained_installed"])
    if not guard_clean:
        report["status"] = "STOP"
        report["guard_failure"] = "clean execution reached a forbidden helper path or a guard was displaced"
        report.pop("proposed_d2b_target", None)
    report["end_utc_epoch"] = utc_epoch()
    report["zero_child_proof"] = {"result": bool(guard_clean and report.get("status") == "PASS"),
        "basis": "first-action audit hook; non-forwarding Python launch guards including _winapi.CreateProcess; forbidden-module sentinels; guarded native DLL loaders and three exercised CreateProcessW tripwires; static no-launcher harness path",
        "ordinary_suite_excluded": True, "external_command_runner_excluded": True}
    report["d2b_readiness"] = "eligible for separate authorization; not authorized or performed" if report.get("status") == "PASS" else "blocked"
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    if report.get("status") != "PASS":
        raise RuntimeError(report.get("failure") or report.get("guard_failure") or report.get("harness_error") or "diagnostic STOP")
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
