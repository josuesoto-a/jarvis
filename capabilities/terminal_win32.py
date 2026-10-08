"""Unregistered Windows x64 native terminal primitives.

Import is inert. Only explicit construction without injected bindings calls the
live loader. D1b tests inject fake bindings and globally forbid that loader.
No destructor, callback, reader thread, fallback or delayed Job assignment.
Native values are private to opaque tokens owned by the invocation ledger.
"""
from __future__ import annotations

import ctypes as C
import os
import re
from threading import Thread, current_thread, main_thread
from uuid import uuid4

from capabilities.terminal import _validate_local_path_form, _validate_windows_host, TerminalContractError
from capabilities.terminal_runtime import (
    BackendResourceScope, CreationReceipt, FileEvidence, ReadResult,
    StdioResources, StreamResources, TerminalLifecycleError,
)

BOOL = C.c_int32
BYTE = C.c_uint8
WORD = C.c_uint16
WCHAR = C.c_uint16
DWORD = C.c_uint32
LONG = C.c_int32
ULONG = C.c_uint32
ULONG_PTR = C.c_size_t
SIZE_T = C.c_size_t
HANDLE = C.c_void_p
LPVOID = C.c_void_p
LPCVOID = C.c_void_p
LPWSTR = C.POINTER(WCHAR)
LPCWSTR = C.POINTER(WCHAR)
LARGE_INTEGER = C.c_int64
ULONGLONG = C.c_uint64


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", BOOL)]


class STARTUPINFOW(C.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", LPWSTR), ("lpDesktop", LPWSTR), ("lpTitle", LPWSTR),
        ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD), ("wShowWindow", WORD), ("cbReserved2", WORD),
        ("lpReserved2", C.POINTER(BYTE)),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class _OVERLAPPED_OFFSETS(C.Structure):
    _fields_ = [("Offset", DWORD), ("OffsetHigh", DWORD)]


class _OVERLAPPED_UNION(C.Union):
    _fields_ = [("offsets", _OVERLAPPED_OFFSETS), ("Pointer", LPVOID)]


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("position", _OVERLAPPED_UNION), ("hEvent", HANDLE)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER), ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD), ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR), ("PriorityClass", DWORD), ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, ULONGLONG) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(C.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(C.Structure):
    _fields_ = [("TotalUserTime", LARGE_INTEGER), ("TotalKernelTime", LARGE_INTEGER),
                ("ThisPeriodTotalUserTime", LARGE_INTEGER), ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
                ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD),
                ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]


class FILETIME(C.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class BY_HANDLE_FILE_INFORMATION(C.Structure):
    _fields_ = [("dwFileAttributes", DWORD), ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME), ("ftLastWriteTime", FILETIME),
                ("dwVolumeSerialNumber", DWORD), ("nFileSizeHigh", DWORD), ("nFileSizeLow", DWORD),
                ("nNumberOfLinks", DWORD), ("nFileIndexHigh", DWORD), ("nFileIndexLow", DWORD)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", DWORD)]


class TOKEN_GROUPS(C.Structure):
    # Documented ANYSIZE_ARRAY header, not an invented opaque structure.
    _fields_ = [("GroupCount", DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


GENERIC_READ = 0x80000000
FILE_READ_ATTRIBUTES = 0x80
FILE_WRITE_DATA = 0x2
SYNCHRONIZE = 0x100000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_TYPE_DISK = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6
PIPE_ACCESS_INBOUND = 1
PIPE_REJECT_REMOTE_CLIENTS = 8
PIPE_TYPE_BYTE = 0
PIPE_READMODE_BYTE = 0
PIPE_WAIT = 0
DUPLICATE_SAME_ACCESS = 2
STARTF_USESTDHANDLES = 0x100
CREATE_SUSPENDED = 0x4
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_NO_WINDOW = 0x08000000
CREATION_FLAGS = CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9
TOKEN_QUERY = 8
TokenGroups = 2
SE_GROUP_LOGON_ID = 0xC0000000
HEAP_ZERO_MEMORY = 8
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_TOKEN = 1008
ERROR_IO_PENDING = 997
ERROR_IO_INCOMPLETE = 996
ERROR_OPERATION_ABORTED = 995
ERROR_BROKEN_PIPE = 109
ERROR_HANDLE_EOF = 38
ERROR_PIPE_CONNECTED = 535
ERROR_NOT_FOUND = 1168
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INVALID_HANDLE_VALUE = C.c_void_p(-1).value
READ_BYTES = 16384

# All live entry points have explicit ABI declarations. Attribute-list internals
# stay opaque: only Windows computes their size and initializes their storage.
_P = C.POINTER
API_SIGNATURES = {
    "CreateFileW": (HANDLE, [LPCWSTR, DWORD, DWORD, _P(SECURITY_ATTRIBUTES), DWORD, DWORD, HANDLE]),
    "GetFileType": (DWORD, [HANDLE]),
    "GetFileInformationByHandle": (BOOL, [HANDLE, _P(BY_HANDLE_FILE_INFORMATION)]),
    "GetFinalPathNameByHandleW": (DWORD, [HANDLE, LPWSTR, DWORD, DWORD]),
    "GetDriveTypeW": (DWORD, [LPCWSTR]),
    "ReadFile": (BOOL, [HANDLE, LPVOID, DWORD, _P(DWORD), _P(OVERLAPPED)]),
    "CreateJobObjectW": (HANDLE, [_P(SECURITY_ATTRIBUTES), LPCWSTR]),
    "SetInformationJobObject": (BOOL, [HANDLE, C.c_int32, LPVOID, DWORD]),
    "QueryInformationJobObject": (BOOL, [HANDLE, C.c_int32, LPVOID, DWORD, _P(DWORD)]),
    "TerminateJobObject": (BOOL, [HANDLE, DWORD]),
    "IsProcessInJob": (BOOL, [HANDLE, HANDLE, _P(BOOL)]),
    "InitializeProcThreadAttributeList": (BOOL, [LPVOID, DWORD, DWORD, _P(SIZE_T)]),
    "UpdateProcThreadAttribute": (BOOL, [LPVOID, DWORD, ULONG_PTR, LPVOID, SIZE_T, LPVOID, _P(SIZE_T)]),
    "DeleteProcThreadAttributeList": (None, [LPVOID]),
    "CreateProcessW": (BOOL, [LPCWSTR, LPWSTR, _P(SECURITY_ATTRIBUTES), _P(SECURITY_ATTRIBUTES),
                             BOOL, DWORD, LPVOID, LPCWSTR, _P(STARTUPINFOW), _P(PROCESS_INFORMATION)]),
    "ResumeThread": (DWORD, [HANDLE]),
    "GetCurrentProcess": (HANDLE, []),
    "GetCurrentThread": (HANDLE, []),
    "GetCurrentProcessId": (DWORD, []),
    "DuplicateHandle": (BOOL, [HANDLE, HANDLE, HANDLE, _P(HANDLE), DWORD, BOOL, DWORD]),
    "CreateNamedPipeW": (HANDLE, [LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, _P(SECURITY_ATTRIBUTES)]),
    "ConnectNamedPipe": (BOOL, [HANDLE, _P(OVERLAPPED)]),
    "GetNamedPipeClientProcessId": (BOOL, [HANDLE, _P(ULONG)]),
    "CreateEventW": (HANDLE, [_P(SECURITY_ATTRIBUTES), BOOL, BOOL, LPCWSTR]),
    "ResetEvent": (BOOL, [HANDLE]),
    "GetOverlappedResult": (BOOL, [HANDLE, _P(OVERLAPPED), _P(DWORD), BOOL]),
    "CancelIoEx": (BOOL, [HANDLE, _P(OVERLAPPED)]),
    "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
    "WaitForMultipleObjects": (DWORD, [DWORD, _P(HANDLE), BOOL, DWORD]),
    "Sleep": (None, [DWORD]),
    "GetExitCodeProcess": (BOOL, [HANDLE, _P(DWORD)]),
    "CloseHandle": (BOOL, [HANDLE]),
    "GetProcessHeap": (HANDLE, []),
    "HeapAlloc": (LPVOID, [HANDLE, DWORD, SIZE_T]),
    "HeapFree": (BOOL, [HANDLE, DWORD, LPVOID]),
    "LocalFree": (LPVOID, [LPVOID]),
    "OpenProcessToken": (BOOL, [HANDLE, DWORD, _P(HANDLE)]),
    "OpenThreadToken": (BOOL, [HANDLE, DWORD, BOOL, _P(HANDLE)]),
    "GetTokenInformation": (BOOL, [HANDLE, C.c_int32, LPVOID, DWORD, _P(DWORD)]),
    "ConvertSidToStringSidW": (BOOL, [LPVOID, _P(LPWSTR)]),
    "ConvertStringSecurityDescriptorToSecurityDescriptorW": (BOOL, [LPCWSTR, DWORD, _P(LPVOID), _P(ULONG)]),
}
_ADVAPI = frozenset({"OpenProcessToken", "OpenThreadToken", "GetTokenInformation",
                     "ConvertSidToStringSidW", "ConvertStringSecurityDescriptorToSecurityDescriptorW"})


class _LiveBindings:
    def __init__(self):
        self._kernel = C.WinDLL("kernel32", use_last_error=True)
        self._advapi = C.WinDLL("advapi32", use_last_error=True)
        for name, (result, arguments) in API_SIGNATURES.items():
            function = getattr(self._advapi if name in _ADVAPI else self._kernel, name)
            function.restype = result
            function.argtypes = arguments
            setattr(self, name, function)

    def last_error(self):
        return C.get_last_error()


def _load_live_bindings():
    if os.name != "nt" or C.sizeof(HANDLE) != 8 or C.sizeof(C.c_wchar) != 2:
        raise TerminalLifecycleError("native backend requires Windows x64 ABI")
    _validate_windows_host()
    return _LiveBindings()


def _utf16(text: str, *, terminated=True):
    encoded = text.encode("utf-16-le") + (b"\0\0" if terminated else b"")
    return (WCHAR * (len(encoded) // 2)).from_buffer_copy(encoded)


def _decode_utf16(array, units):
    return C.string_at(C.addressof(array), units * 2).decode("utf-16-le")


def normalize_final_path(value: str) -> str:
    if value.startswith("\\\\?\\"):
        value = value[4:]
    _validate_local_path_form(value, canonical=True)
    # Only a recognized DOS prefix and the drive LETTER may change.
    return value[0].upper() + value[1:]


class NativeCallError(TerminalLifecycleError):
    def __init__(self, operation: str, code: int):
        self.operation = operation
        self.code = code
        super().__init__(operation + " failed")


class _Handle:
    def __init__(self, backend):
        self._backend = backend
        self._value = None
        self._output = HANDLE()
        self._from_output = False
        self._attempted = False
        self._resolved = False
        self._acquired = False
        self._closed = False

    def _raw(self):
        if self._closed or not self._resolved or not self._acquired:
            raise TerminalLifecycleError("native handle is not known-owned")
        value = self._output.value if self._from_output else self._value
        if value in {None, 0, INVALID_HANDLE_VALUE}:
            raise TerminalLifecycleError("native handle is invalid")
        return value


class _LocalMemory:
    def __init__(self, backend, *, string=False):
        self._backend = backend
        self._output = LPWSTR() if string else LPVOID()
        self._attempted = False
        self._resolved = False
        self._acquired = False
        self._closed = False

    def _raw(self):
        if not self._resolved or not self._acquired or self._closed:
            raise TerminalLifecycleError("local allocation is not known-owned")
        return C.cast(self._output, LPVOID).value


class _AttributeList:
    def __init__(self, backend):
        self._backend = backend
        self._heap = None
        self._address = None
        self._allocation_pending = False
        self._initialized = False
        self._initialization_pending = False
        self._closed = False
        self._handles = None
        self._jobs = None


class _ReadMemory:
    def __init__(self, backend, reader, event):
        self._backend = backend
        self._reader = reader
        self._event = event
        self._heap = None
        self._address = None
        self._allocation_pending = False
        self._overlapped = None
        self._pending = False
        self._kind = None
        self._last = ReadResult(True)
        self._closed = False


class _CreationHandle:
    def __init__(self, launch, field):
        self._backend = launch._backend
        self._launch = launch
        self._field = field
        self._closed = False

    def _raw(self):
        receipt = self._launch._receipt
        if self._closed or receipt is None or not receipt.snapshot().process_created:
            raise TerminalLifecycleError("creation output is not known-owned")
        value = getattr(self._launch._information, self._field)
        if value in {None, 0, INVALID_HANDLE_VALUE}:
            raise TerminalLifecycleError("known creation output is invalid")
        return value


class _PreparedLaunch:
    def __init__(self, backend, application, command, environment, cwd):
        self._backend = backend
        self._application = _utf16(application)
        self._command = _utf16(command)
        # Input includes final double NUL; never truncate at the first separator.
        self._environment = _utf16(environment, terminated=False)
        self._cwd = _utf16(cwd)
        self._startup = STARTUPINFOEXW()
        self._information = PROCESS_INFORMATION()
        self._receipt = None
        self._process = _CreationHandle(self, "hProcess")
        self._thread = _CreationHandle(self, "hThread")
        self._attributes = None
        self._closed = False
        # All pointers / argument conversion are prepared before begin_call.
        self._call_arguments = None
        self._create_call = None


class Win32TerminalBackend:
    """Explicit construction is the ONLY live-loader boundary.

    Supplying bindings is the fake-native path. It makes zero live calls and
    never constructs _LiveBindings. Owner checks precede every primitive.
    """
    def __init__(self, *, owner_thread: Thread, bindings=None):
        if not isinstance(owner_thread, Thread) or owner_thread is main_thread():
            raise ValueError("explicit non-main native owner required")
        self._owner = owner_thread
        if bindings is None:
            self._check()  # Even live DLL loading belongs to the designated owner.
            self._api = _load_live_bindings()
        else:
            self._api = bindings

    def _check(self, token=None):
        if current_thread() is not self._owner or current_thread() is main_thread():
            raise TerminalLifecycleError("native primitive called outside owner thread")
        if token is not None and getattr(token, "_backend", None) is not self:
            raise TerminalLifecycleError("foreign native resource")

    def _error(self, operation):
        return NativeCallError(operation, self._api.last_error())

    def _bool(self, name, *arguments):
        if not getattr(self._api, name)(*arguments):
            raise self._error(name)

    def _handle(self, scope, name, function, *arguments, invalid=None, creation_only=False):
        self._check()
        token = _Handle(self)
        resource = scope.own(name, token, creation_only=creation_only)
        token._attempted = True
        value = function(*arguments)
        token._value = value
        token._acquired = value not in {None, 0, invalid}
        token._resolved = True
        if not token._acquired:
            raise self._error(name)
        return resource, token

    def _output_handle(self, scope, name, function, *arguments):
        self._check()
        token = _Handle(self)
        resource = scope.own(name, token)
        token._from_output = True
        token._attempted = True
        success = function(*arguments, C.byref(token._output))
        token._acquired = bool(success)
        token._resolved = True
        return resource, token, success

    def open_executable(self, scope, path):
        return self._handle(scope, "executable", self._api.CreateFileW,
                            _utf16(path), GENERIC_READ, FILE_SHARE_READ, None,
                            OPEN_EXISTING, 0, None, invalid=INVALID_HANDLE_VALUE)[0]

    def open_cwd(self, scope, path):
        return self._handle(scope, "cwd", self._api.CreateFileW,
                            _utf16(path), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
                            invalid=INVALID_HANDLE_VALUE)[0]

    def _final_path(self, token):
        needed = self._api.GetFinalPathNameByHandleW(token._raw(), None, 0, 0)
        if not 0 < needed <= 264:
            raise TerminalContractError("unsupported final path size")
        buffer = (WCHAR * (needed + 1))()
        used = self._api.GetFinalPathNameByHandleW(token._raw(), buffer, len(buffer), 0)
        if not 0 < used < len(buffer):
            raise TerminalContractError("final path unavailable or changed")
        path = normalize_final_path(_decode_utf16(buffer, used))
        if self._api.GetDriveTypeW(_utf16(path[:3])) not in {
                DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_CDROM, DRIVE_RAMDISK}:
            raise TerminalContractError("final path is not on a supported local drive")
        return path

    def file_evidence(self, resource):
        self._check(resource)
        info = BY_HANDLE_FILE_INFORMATION()
        self._bool("GetFileInformationByHandle", resource._raw(), C.byref(info))
        disk = self._api.GetFileType(resource._raw()) == FILE_TYPE_DISK
        return FileEvidence(self._final_path(resource),
                            (info.nFileSizeHigh << 32) | info.nFileSizeLow,
                            bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY), disk,
                            (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow,
                             info.ftLastWriteTime.dwHighDateTime, info.ftLastWriteTime.dwLowDateTime))

    def read_executable(self, resource, size):
        self._check(resource)
        if type(size) is not int or not 0 < size <= 65536:
            raise ValueError("bounded verification read required")
        buffer = (BYTE * size)()
        read = DWORD()
        self._bool("ReadFile", resource._raw(), buffer, size, C.byref(read), None)
        if read.value > size:
            raise TerminalLifecycleError("native verification byte count invalid")
        return bytes(buffer[:read.value])

    def create_job(self, scope):
        return self._handle(scope, "job", self._api.CreateJobObjectW, None, None)[0]

    def configure_job(self, job):
        self._check(job)
        policy = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        policy.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._bool("SetInformationJobObject", job._raw(), JobObjectExtendedLimitInformation,
                   C.byref(policy), C.sizeof(policy))

    def job_active(self, job):
        self._check(job)
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        length = DWORD()
        self._bool("QueryInformationJobObject", job._raw(), JobObjectBasicAccountingInformation,
                   C.byref(info), C.sizeof(info), C.byref(length))
        if length.value != C.sizeof(info):
            raise TerminalLifecycleError("Job accounting result size differs")
        return int(info.ActiveProcesses)

    def terminate_job(self, job):
        self._check(job)
        self._bool("TerminateJobObject", job._raw(), 1)

    def membership(self, process, job):
        self._check(process)
        self._check(job)
        member = BOOL()
        self._bool("IsProcessInJob", process._raw(), job._raw(), C.byref(member))
        return bool(member.value)

    def _logon_security(self, scope):
        # Thread impersonation is unsupported; process token is the only identity.
        _, thread_token, impersonating = self._output_handle(
            scope, "thread-token-check", self._api.OpenThreadToken,
            self._api.GetCurrentThread(), TOKEN_QUERY, True)
        if impersonating or self._api.last_error() != ERROR_NO_TOKEN:
            raise TerminalLifecycleError("thread identity is unsupported")
        _, process_token, success = self._output_handle(
            scope, "process-token", self._api.OpenProcessToken,
            self._api.GetCurrentProcess(), TOKEN_QUERY)
        if not success:
            raise self._error("OpenProcessToken")
        needed = DWORD()
        ok = self._api.GetTokenInformation(process_token._raw(), TokenGroups, None, 0, C.byref(needed))
        if ok or self._api.last_error() != ERROR_INSUFFICIENT_BUFFER or needed.value < TOKEN_GROUPS.Groups.offset:
            raise TerminalLifecycleError("token-group sizing failed")
        scope.checkpoint()
        buffer = C.create_string_buffer(needed.value)
        used = DWORD()
        self._bool("GetTokenInformation", process_token._raw(), TokenGroups,
                   buffer, len(buffer), C.byref(used))
        if used.value > len(buffer) or used.value < TOKEN_GROUPS.Groups.offset:
            raise TerminalLifecycleError("invalid token-group storage")
        count = DWORD.from_buffer(buffer).value
        if count > (used.value - TOKEN_GROUPS.Groups.offset) // C.sizeof(SID_AND_ATTRIBUTES):
            raise TerminalLifecycleError("invalid token-group count")
        groups = (SID_AND_ATTRIBUTES * count).from_buffer(buffer, TOKEN_GROUPS.Groups.offset)
        sids = [group.Sid for group in groups if group.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID]
        if len(sids) != 1:
            raise TerminalLifecycleError("unique logon identity required")
        text = _LocalMemory(self, string=True)
        scope.own("logon-sid-string", text)
        text._attempted = True
        success = self._api.ConvertSidToStringSidW(sids[0], C.byref(text._output))
        text._acquired = bool(success)
        text._resolved = True
        if not success:
            raise self._error("ConvertSidToStringSidW")
        units = []
        for index in range(184):
            unit = text._output[index]
            if unit == 0:
                break
            units.append(unit)
        else:
            raise TerminalLifecycleError("invalid SID string size")
        sid = bytes((WCHAR * len(units))(*units)).decode("utf-16-le")
        if not re.fullmatch(r"S-1-5-5-\d+-\d+", sid):
            raise TerminalLifecycleError("unsupported logon SID")
        # Protected DACL: one ACE for this logon only. The server needs pipe-
        # instance creation; the client requests only write-data + synchronize.
        sddl = "D:P(A;;0x0012019f;;;" + sid + ")"
        descriptor = _LocalMemory(self)
        scope.own("pipe-security", descriptor)
        descriptor._attempted = True
        success = self._api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _utf16(sddl), 1, C.byref(descriptor._output), None)
        descriptor._acquired = bool(success)
        descriptor._resolved = True
        if not success:
            raise self._error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        return SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), descriptor._raw(), False)

    def _duplicate(self, scope, name, original):
        token = _Handle(self)
        resource = scope.own(name, token, creation_only=True)
        token._from_output = True
        source = self._api.GetCurrentProcess()
        token._attempted = True
        success = self._api.DuplicateHandle(source, original._raw(), source,
                                            C.byref(token._output), 0, True, DUPLICATE_SAME_ACCESS)
        token._acquired = bool(success)
        token._resolved = True
        if not success:
            raise self._error("DuplicateHandle")
        return resource

    def _allocate(self, token, size):
        token._heap = self._api.GetProcessHeap()
        if not token._heap:
            raise TerminalLifecycleError("process heap unavailable")
        token._allocation_pending = True
        token._address = self._api.HeapAlloc(token._heap, HEAP_ZERO_MEMORY, size)
        token._allocation_pending = False
        if not token._address:
            raise TerminalLifecycleError("native allocation failed")

    def make_stdio(self, scope):
        self._check()
        security = self._logon_security(scope)
        _, nul = self._handle(scope, "stdin-original", self._api.CreateFileW,
                              _utf16("NUL"), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              None, OPEN_EXISTING, 0, None,
                              invalid=INVALID_HANDLE_VALUE, creation_only=True)
        stdin = self._duplicate(scope, "stdin-child", nul)
        children = [stdin]
        for name in ("stdout", "stderr"):
            pipe_name = "\\\\.\\pipe\\jarvis-terminal-" + uuid4().hex
            reader_resource, reader = self._handle(
                scope, name + "-reader", self._api.CreateNamedPipeW,
                _utf16(pipe_name), PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1, READ_BYTES, READ_BYTES, 0, C.byref(security), invalid=INVALID_HANDLE_VALUE)
            _, writer = self._handle(
                scope, name + "-original", self._api.CreateFileW,
                _utf16(pipe_name), FILE_WRITE_DATA | SYNCHRONIZE, 0, None,
                OPEN_EXISTING, 0, None, invalid=INVALID_HANDLE_VALUE, creation_only=True)
            children.append(self._duplicate(scope, name + "-child", writer))
            event_resource, event = self._handle(scope, name + "-event", self._api.CreateEventW,
                                                  None, True, False, None)
            memory = _ReadMemory(self, reader, event)
            memory_resource = scope.own(name + "-memory", memory)
            self._allocate(memory, C.sizeof(OVERLAPPED) + READ_BYTES)
            memory._overlapped = OVERLAPPED.from_address(memory._address)
            memory._overlapped.hEvent = event._raw()
            scope.streams.append(StreamResources(name, reader_resource, event_resource, memory_resource))
        return StdioResources(tuple(children))

    def _prepare_io(self, memory, kind):
        self._check(memory)
        if memory._closed or memory._pending or memory._overlapped is None:
            raise TerminalLifecycleError("I/O storage unavailable")
        self._bool("ResetEvent", memory._event._raw())
        C.memset(memory._address, 0, C.sizeof(OVERLAPPED))
        memory._overlapped.hEvent = memory._event._raw()
        memory._kind = kind
        memory._pending = True

    def connect_pipe(self, memory):
        self._prepare_io(memory, "connect")
        success = self._api.ConnectNamedPipe(memory._reader._raw(), C.byref(memory._overlapped))
        error = 0 if success else self._api.last_error()
        if error == ERROR_IO_PENDING:
            return ReadResult(False)
        memory._pending = False
        memory._last = ReadResult(True, error=None if success or error == ERROR_PIPE_CONNECTED else "connect_failed")
        if memory._last.error is None:
            pid = ULONG()
            self._bool("GetNamedPipeClientProcessId", memory._reader._raw(), C.byref(pid))
            if pid.value != self._api.GetCurrentProcessId():
                memory._last = ReadResult(True, error="unexpected_pipe_client")
        return memory._last

    def submit_read(self, memory):
        self._prepare_io(memory, "read")
        success = self._api.ReadFile(memory._reader._raw(), memory._address + C.sizeof(OVERLAPPED),
                                    READ_BYTES, None, C.byref(memory._overlapped))
        if not success:
            error = self._api.last_error()
            if error == ERROR_IO_PENDING:
                return ReadResult(False)
            memory._pending = False
            memory._last = ReadResult(True, eof=error in {ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF},
                                      error=None if error in {ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF} else "read_failed")
            return memory._last
        return self.inspect_read(memory)

    def inspect_read(self, memory):
        self._check(memory)
        if not memory._pending:
            return memory._last
        count = DWORD()
        success = self._api.GetOverlappedResult(memory._reader._raw(), C.byref(memory._overlapped),
                                              C.byref(count), False)
        if not success:
            error = self._api.last_error()
            if error == ERROR_IO_INCOMPLETE:
                return ReadResult(False)
            if error not in {ERROR_OPERATION_ABORTED, ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF}:
                # Invalid observation is not proof the kernel stopped using storage.
                raise self._error("GetOverlappedResult")
            memory._pending = False
            memory._last = ReadResult(True, eof=error in {ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF},
                                      error="read_cancelled" if error == ERROR_OPERATION_ABORTED else None)
            return memory._last
        memory._pending = False
        if count.value > READ_BYTES:
            raise TerminalLifecycleError("native output count exceeds fixed buffer")
        data = (C.string_at(memory._address + C.sizeof(OVERLAPPED), count.value)
                if memory._kind == "read" else b"")
        # A successful zero-byte pipe read is NOT EOF.
        memory._last = ReadResult(True, data)
        return memory._last

    def cancel_read(self, memory):
        self._check(memory)
        if memory._pending:
            success = self._api.CancelIoEx(memory._reader._raw(), C.byref(memory._overlapped))
            if not success and self._api.last_error() != ERROR_NOT_FOUND:
                raise self._error("CancelIoEx")
        # Never turn a cancellation request into completion.

    def prepare_launch(self, scope, target, command_line, environment, job, children):
        self._check(job)
        if len(children) != 3:
            raise TerminalLifecycleError("exactly three inherited handles required")
        for child in children:
            self._check(child)
        launch = _PreparedLaunch(self, target.executable_resolved, command_line, environment, target.cwd)
        resource = scope.own("launch", launch, creation_only=True)
        attributes = _AttributeList(self)
        scope.own("attributes", attributes, creation_only=True)
        launch._attributes = attributes
        size = SIZE_T()
        success = self._api.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
        if success or self._api.last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value:
            raise TerminalLifecycleError("attribute-list sizing failed")
        scope.checkpoint()
        self._allocate(attributes, size.value)
        attributes._initialization_pending = True
        success = self._api.InitializeProcThreadAttributeList(attributes._address, 2, 0, C.byref(size))
        attributes._initialized = bool(success)
        attributes._initialization_pending = False
        if not success:
            raise self._error("InitializeProcThreadAttributeList")
        attributes._handles = (HANDLE * 3)(*(child._raw() for child in children))
        attributes._jobs = (HANDLE * 1)(job._raw())
        for key, array in ((PROC_THREAD_ATTRIBUTE_HANDLE_LIST, attributes._handles),
                           (PROC_THREAD_ATTRIBUTE_JOB_LIST, attributes._jobs)):
            scope.checkpoint()
            self._bool("UpdateProcThreadAttribute", attributes._address, 0, key,
                       array, C.sizeof(array), None, None)
        startup = launch._startup
        startup.StartupInfo.cb = C.sizeof(STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = attributes._handles
        startup.lpAttributeList = attributes._address
        launch._create_call = self._api.CreateProcessW
        launch._call_arguments = (
            C.cast(launch._application, LPCWSTR), C.cast(launch._command, LPWSTR),
            C.POINTER(SECURITY_ATTRIBUTES)(), C.POINTER(SECURITY_ATTRIBUTES)(),
            BOOL(1), DWORD(CREATION_FLAGS), C.cast(launch._environment, LPVOID),
            C.cast(launch._cwd, LPCWSTR),
            C.cast(C.pointer(startup), C.POINTER(STARTUPINFOW)), C.pointer(launch._information))
        return resource

    def create_process(self, receipt: CreationReceipt, launch):
        self._check(launch)
        if launch._receipt is not None or launch._closed or launch._call_arguments is None:
            raise TerminalLifecycleError("launch cannot be attempted again")
        launch._receipt = receipt
        if not receipt.begin_call():
            return
        # If this call or immediate commitment raises, begin_call leaves CU.
        # No outputs are read before this scalar known-result commitment.
        success = launch._create_call(*launch._call_arguments)
        if success:
            receipt.record_success()
            receipt.attach_process(launch._process)
            receipt.attach_thread(launch._thread)
        else:
            receipt.record_failed()

    def resume(self, thread):
        self._check(thread)
        return int(self._api.ResumeThread(thread._raw()))

    def root_exited(self, process):
        self._check(process)
        result = self._api.WaitForSingleObject(process._raw(), 0)
        if result not in {WAIT_OBJECT_0, WAIT_TIMEOUT}:
            raise self._error("WaitForSingleObject")
        return result == WAIT_OBJECT_0

    def exit_code(self, process):
        self._check(process)
        code = DWORD()
        self._bool("GetExitCodeProcess", process._raw(), C.byref(code))
        return int(code.value)

    def wait(self, process, memories, seconds):
        self._check()
        handles = []
        if process is not None:
            self._check(process)
            handles.append(process._raw())
        for memory in memories:
            self._check(memory)
            if memory._pending:
                handles.append(memory._event._raw())
        milliseconds = max(0, min(20, int(seconds * 1000)))
        if handles:
            array = (HANDLE * len(handles))(*handles)
            result = self._api.WaitForMultipleObjects(len(handles), array, False, milliseconds)
            if result == WAIT_FAILED or result != WAIT_TIMEOUT and not 0 <= result < len(handles):
                raise self._error("WaitForMultipleObjects")
        else:
            self._api.Sleep(milliseconds)

    def release(self, token):
        self._check(token)
        if token._closed:
            raise TerminalLifecycleError("native release cannot repeat")
        if type(token) in {_Handle, _CreationHandle}:
            if type(token) is _Handle and not token._acquired:
                if token._attempted and not token._resolved:
                    return False
            elif not self._api.CloseHandle(token._raw()):
                return False
        elif type(token) is _LocalMemory:
            if not token._resolved and token._attempted:
                return False
            if token._acquired and self._api.LocalFree(token._raw()):
                return False
        elif type(token) in {_ReadMemory, _AttributeList}:
            if token._allocation_pending:
                return False
            if type(token) is _ReadMemory and token._pending:
                return False
            if type(token) is _AttributeList:
                if token._initialization_pending:
                    return False
                if token._initialized:
                    self._api.DeleteProcThreadAttributeList(token._address)
                    token._initialized = False
            if token._address and not self._api.HeapFree(token._heap, 0, token._address):
                return False
            token._address = None
        elif type(token) is _PreparedLaunch:
            if token._receipt is not None:
                snapshot = token._receipt.snapshot()
                if snapshot.result_pending or snapshot.process_created and not snapshot.adoption_complete:
                    return False
            token._application = token._command = token._environment = token._cwd = None
            token._call_arguments = None
            token._create_call = None
        else:
            raise TerminalLifecycleError("unsupported native resource")
        token._closed = True
        return True
