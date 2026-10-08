"""ctypes-shaped, entirely in-memory Win32 binding double. No live APIs."""
from collections import Counter, deque
import ctypes as C

import capabilities.terminal_win32 as W
from capabilities.terminal_runtime import LaunchAdmissionDomain, WindowsTerminalRuntime
from terminal_fixtures import console_pe_bytes
from terminal_runtime_fakes import FakeClock, on_owner, target_for, forbid_live_loader


def pointer(argument, kind):
    return C.cast(argument, C.POINTER(kind))


def wide(argument):
    if not argument:
        return None
    values = []
    ptr = pointer(argument, W.WCHAR)
    for index in range(32768):
        unit = ptr[index]
        if unit == 0:
            return bytes((W.WCHAR * len(values))(*values)).decode("utf-16-le")
        values.append(unit)
    raise AssertionError("unterminated fake wide input")


class FakeBindings:
    def __init__(self, clock=None, *, creation="P1"):
        self.clock = FakeClock() if clock is None else clock
        self.creation = creation
        self.error = 0
        self.calls = []
        self.counts = Counter()
        self.failures = {}
        self.handles = {}
        self.next_handle = 100
        self.allocations = {}
        self.locals = {}
        self.reads = {}
        self.image = console_pe_bytes()
        self.position = 0
        self.outputs = {"stdout": deque([b"out", None]), "stderr": deque([b"err", None])}
        self.pipe_index = 0
        self.pipe_names = {}
        self.attributes = {}
        self.attribute_deleted = set()
        self.active = 0
        self.exited = False
        self.resumed = False
        self.exit = 0
        self.suspend_count = 1
        self.member = True
        self.client_pid = 500
        self.current_pid = 500
        self.create_hook = None
        self.cancel_unresolved = False
        self.keep_job = False
        self.impersonating = False
        self.final_paths = {}
        self.drive_type = 3
        self.launch_record = None
        self.security_sddl = None
        self.job_policy = None

    def __getattr__(self, name):
        if name not in W.API_SIGNATURES:
            raise AttributeError(name)

        def call(*args):
            self.counts[name] += 1
            self.calls.append((name, args))
            failure = self.failures.get((name, self.counts[name]))
            if failure is not None:
                if isinstance(failure, BaseException):
                    raise failure
                value, self.error = failure
                return value
            return getattr(self, "_" + name)(*args)

        return call

    def last_error(self):
        return self.error

    def _new(self, kind, path=None):
        self.next_handle += 1
        self.handles[self.next_handle] = {"kind": kind, "path": path, "closed": False, "inheritable": False}
        return self.next_handle

    def _CreateFileW(self, path, access, share, security, disposition, flags, template):
        path = wide(path)
        if path in self.pipe_names:
            return self._new("pipe-writer", path)
        if path == "NUL":
            return self._new("nul", path)
        return self._new("cwd" if flags & W.FILE_FLAG_BACKUP_SEMANTICS else "executable", path)

    def _GetFileType(self, handle):
        return W.FILE_TYPE_DISK

    def _GetFileInformationByHandle(self, handle, result):
        info = pointer(result, W.BY_HANDLE_FILE_INFORMATION).contents
        kind = self.handles[handle]["kind"]
        info.dwFileAttributes = W.FILE_ATTRIBUTE_DIRECTORY if kind == "cwd" else 0
        info.nFileSizeLow = 0 if kind == "cwd" else len(self.image)
        info.dwVolumeSerialNumber = 1
        info.nFileIndexLow = handle
        info.ftLastWriteTime.dwLowDateTime = 20
        return 1

    def _GetFinalPathNameByHandleW(self, handle, output, length, flags):
        path = self.final_paths.get(self.handles[handle]["kind"], "\\\\?\\" + self.handles[handle]["path"])
        buffer = W._utf16(path)
        units = len(buffer) - 1
        if output is None:
            return units + 1
        if length <= units:
            return units + 1
        C.memmove(output, buffer, C.sizeof(buffer))
        return units

    def _GetDriveTypeW(self, path):
        return self.drive_type

    def _ReadFile(self, handle, buffer, size, count, overlapped):
        if self.handles[handle]["kind"] == "executable":
            chunk = self.image[self.position:self.position + size]
            self.position += len(chunk)
            C.memmove(buffer, chunk, len(chunk))
            pointer(count, W.DWORD)[0] = len(chunk)
            return 1
        assert overlapped is not None and count is None
        assert size == W.READ_BYTES
        self.reads[handle] = {"buffer": buffer, "overlapped": overlapped, "cancelled": False}
        self.error = W.ERROR_IO_PENDING
        return 0

    def _CreateJobObjectW(self, security, name):
        assert name is None and security is None
        return self._new("job")

    def _SetInformationJobObject(self, job, kind, info, size):
        assert kind == W.JobObjectExtendedLimitInformation
        self.job_policy = bytes(pointer(info, W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION).contents)
        return 1

    def _QueryInformationJobObject(self, job, kind, info, size, length):
        assert self.handles[job]["kind"] == "job"
        pointer(info, W.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).contents.ActiveProcesses = self.active
        pointer(length, W.DWORD)[0] = C.sizeof(W.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
        return 1

    def _TerminateJobObject(self, job, exit_code):
        assert self.handles[job]["kind"] == "job"
        if not self.keep_job:
            self.active = 0
        self.exited = True
        return 1

    def _IsProcessInJob(self, process, job, result):
        assert process == 0xE001
        pointer(result, W.BOOL)[0] = self.member
        return 1

    def _InitializeProcThreadAttributeList(self, address, count, flags, size):
        assert count == 2 and flags == 0
        if address is None:
            pointer(size, W.SIZE_T)[0] = 128
            self.error = W.ERROR_INSUFFICIENT_BUFFER
            return 0
        self.attributes[address] = {}
        return 1

    def _UpdateProcThreadAttribute(self, address, flags, key, values, size, previous, length):
        assert flags == 0 and previous is None and length is None
        self.attributes[address][key] = tuple(pointer(values, W.HANDLE)[i] for i in range(size // C.sizeof(W.HANDLE)))
        return 1

    def _DeleteProcThreadAttributeList(self, address):
        assert address in self.attributes and address not in self.attribute_deleted
        self.attribute_deleted.add(address)

    def _CreateProcessW(self, app, command, process_sa, thread_sa, inherit, flags, env, cwd, startup, output):
        inherit = bool(inherit.value) if isinstance(inherit, W.BOOL) else inherit
        flags = flags.value if isinstance(flags, W.DWORD) else flags
        process_sa = None if not process_sa else process_sa
        thread_sa = None if not thread_sa else thread_sa
        start = pointer(startup, W.STARTUPINFOEXW).contents
        # Preserve the exact full environment, including separators/double NUL.
        env_pointer = pointer(env, W.WCHAR)
        units = []
        for index in range(32768):
            units.append(env_pointer[index])
            if len(units) >= 2 and units[-2:] == [0, 0]:
                break
        self.launch_record = {
            "application": wide(app), "command": wide(command), "cwd": wide(cwd),
            "environment": bytes((W.WCHAR * len(units))(*units)).decode("utf-16-le"),
            "inherit": inherit, "flags": flags, "process_security": process_sa,
            "thread_security": thread_sa, "cb": start.StartupInfo.cb,
            "startup_flags": start.StartupInfo.dwFlags,
            "std_handles": (start.StartupInfo.hStdInput, start.StartupInfo.hStdOutput, start.StartupInfo.hStdError),
            "attributes": dict(self.attributes[start.lpAttributeList]),
            "attribute_address": start.lpAttributeList,
            "verification_held": all(not h["closed"] for h in self.handles.values() if h["kind"] in {"cwd", "executable"}),
        }
        info = pointer(output, W.PROCESS_INFORMATION).contents
        info.hProcess, info.hThread = 0xE001, 0xE002
        info.dwProcessId, info.dwThreadId = 1, 2
        if self.create_hook:
            self.create_hook()
        if self.creation == "CU":
            self.active = 1
            raise KeyboardInterrupt("lost native result")
        if self.creation == "C1":
            self.error = 5
            return 0
        self.handles[0xE001] = {"kind": "process", "closed": False, "inheritable": False}
        self.handles[0xE002] = {"kind": "thread", "closed": False, "inheritable": False}
        self.active = 1
        return 1

    def _ResumeThread(self, thread):
        assert thread == 0xE002
        self.resumed = self.suspend_count == 1
        self.exited = True
        self.active = 0
        return self.suspend_count

    def _GetCurrentProcess(self):
        return W.INVALID_HANDLE_VALUE

    def _GetCurrentThread(self):
        return W.INVALID_HANDLE_VALUE - 1

    def _GetCurrentProcessId(self):
        return self.current_pid

    def _DuplicateHandle(self, source, original, destination, output, access, inherit, options):
        assert options == W.DUPLICATE_SAME_ACCESS
        handle = self._new(self.handles[original]["kind"] + "-duplicate", self.handles[original]["path"])
        self.handles[handle]["inheritable"] = bool(inherit)
        pointer(output, W.HANDLE)[0] = handle
        return 1

    def _CreateNamedPipeW(self, name, access, mode, instances, out_size, in_size, timeout, security):
        name = wide(name)
        kind = ("stdout", "stderr")[self.pipe_index]
        self.pipe_index += 1
        handle = self._new(kind + "-reader", name)
        self.pipe_names[name] = handle
        return handle

    def _ConnectNamedPipe(self, reader, overlapped):
        assert overlapped is not None
        self.error = W.ERROR_PIPE_CONNECTED
        return 0

    def _GetNamedPipeClientProcessId(self, pipe, result):
        pointer(result, W.ULONG)[0] = self.client_pid
        return 1

    def _CreateEventW(self, security, manual, initial, name):
        assert security is None and manual is True and initial is False and name is None
        return self._new("event")

    def _ResetEvent(self, event):
        return 1

    def _GetOverlappedResult(self, handle, overlapped, count, wait):
        assert wait is False
        read = self.reads.get(handle)
        if read is None:
            self.error = 87
            return 0
        if read["cancelled"]:
            self.error = W.ERROR_IO_INCOMPLETE if self.cancel_unresolved else W.ERROR_OPERATION_ABORTED
            if not self.cancel_unresolved:
                self.reads.pop(handle)
            return 0
        if not self.resumed and not self.exited:
            self.error = W.ERROR_IO_INCOMPLETE
            return 0
        stream = "stdout" if self.handles[handle]["kind"].startswith("stdout") else "stderr"
        if not self.outputs[stream]:
            self.error = W.ERROR_IO_INCOMPLETE
            return 0
        chunk = self.outputs[stream].popleft()
        self.reads.pop(handle)
        if chunk is None:
            self.error = W.ERROR_BROKEN_PIPE
            return 0
        assert len(chunk) <= W.READ_BYTES
        C.memmove(read["buffer"], chunk, len(chunk))
        pointer(count, W.DWORD)[0] = len(chunk)
        return 1

    def _CancelIoEx(self, handle, overlapped):
        if handle not in self.reads:
            self.error = W.ERROR_NOT_FOUND
            return 0
        self.reads[handle]["cancelled"] = True
        return 1

    def _WaitForSingleObject(self, process, timeout):
        assert process == 0xE001 and timeout == 0
        return W.WAIT_OBJECT_0 if self.exited else W.WAIT_TIMEOUT

    def _WaitForMultipleObjects(self, count, handles, all_handles, timeout):
        assert count > 0 and not all_handles and 0 <= timeout <= 20
        self.clock.advance(max(timeout / 1000, 0.001))
        return W.WAIT_TIMEOUT

    def _Sleep(self, milliseconds):
        self.clock.advance(max(milliseconds / 1000, 0.001))

    def _GetExitCodeProcess(self, process, output):
        pointer(output, W.DWORD)[0] = self.exit
        return 1

    def _CloseHandle(self, handle):
        assert handle in self.handles, "uncertain output handle used"
        assert not self.handles[handle]["closed"], "blind close retry"
        assert handle not in self.reads, "reader closed before completion"
        self.handles[handle]["closed"] = True
        return 1

    def _GetProcessHeap(self):
        return 999

    def _HeapAlloc(self, heap, flags, size):
        assert heap == 999 and flags == W.HEAP_ZERO_MEMORY
        memory = C.create_string_buffer(size)
        address = C.addressof(memory)
        self.allocations[address] = memory
        return address

    def _HeapFree(self, heap, flags, address):
        assert address in self.allocations
        assert not any(C.addressof(pointer(read["overlapped"], W.OVERLAPPED).contents) == address
                       for read in self.reads.values()), "pending I/O memory freed"
        self.allocations.pop(address)
        return 1

    def _LocalFree(self, address):
        assert address in self.locals
        self.locals.pop(address)
        return None

    def _OpenProcessToken(self, process, access, output):
        assert access == W.TOKEN_QUERY
        pointer(output, W.HANDLE)[0] = self._new("token")
        return 1

    def _OpenThreadToken(self, thread, access, self_access, output):
        if self.impersonating:
            pointer(output, W.HANDLE)[0] = self._new("thread-token")
            return 1
        self.error = W.ERROR_NO_TOKEN
        return 0

    def _GetTokenInformation(self, token, kind, buffer, size, required):
        assert kind == W.TokenGroups
        pointer(required, W.DWORD)[0] = 64
        if buffer is None:
            self.error = W.ERROR_INSUFFICIENT_BUFFER
            return 0
        pointer(buffer, W.DWORD)[0] = 1
        entry = W.SID_AND_ATTRIBUTES.from_buffer(buffer, W.TOKEN_GROUPS.Groups.offset)
        entry.Sid = 10
        entry.Attributes = W.SE_GROUP_LOGON_ID
        return 1

    def _ConvertSidToStringSidW(self, sid, result):
        memory = W._utf16("S-1-5-5-100-200")
        address = C.addressof(memory)
        self.locals[address] = memory
        pointer(result, W.LPVOID)[0] = address
        return 1

    def _ConvertStringSecurityDescriptorToSecurityDescriptorW(self, text, revision, output, length):
        self.security_sddl = wide(text)
        memory = C.create_string_buffer(64)
        address = C.addressof(memory)
        self.locals[address] = memory
        pointer(output, W.LPVOID)[0] = address
        return 1


def run_native(api=None, *, target=None, domain=None, configure=None):
    api = FakeBindings() if api is None else api
    domain = LaunchAdmissionDomain(clock=api.clock) if domain is None else domain
    target = target_for(api.image) if target is None else target

    def run(owner):
        backend = W.Win32TerminalBackend(owner_thread=owner, bindings=api)
        runtime = WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=owner)
        if configure:
            configure(runtime, backend)
        return runtime.execute(target)

    return on_owner(run), api, domain
