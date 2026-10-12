"""Fake native host evidence only: no helper, shell, backend or live child."""
import builtins
import ctypes as C
import importlib.util
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest

from capabilities import terminal as T
from terminal_fixtures import console_pe_bytes


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.implementation(*args)


def host(monkeypatch, *, major=10, build=22000, product=1, bits=64):
    monkeypatch.setattr(T, "os", SimpleNamespace(
        name="nt", environ={"SYSTEMROOT": r"C:\Windows"}))
    monkeypatch.setattr(T, "sys", SimpleNamespace(
        getwindowsversion=lambda: SimpleNamespace(
            major=major, build=build, product_type=product),
        maxsize=2**(bits - 1) - 1))


def modern_query(monkeypatch, *, native=0x8664, process=0, result=1, error=None):
    def query(handle, process_out, native_out):
        assert handle.value == C.c_void_p(-1).value
        if error is not None:
            raise error
        if process is not None:
            C.cast(process_out, C.POINTER(C.c_uint16))[0] = process
        if native is not None:
            C.cast(native_out, C.POINTER(C.c_uint16))[0] = native
        return result

    function = Function(query)
    loads = []

    def load(name, **kwargs):
        loads.append((name, kwargs))
        return SimpleNamespace(IsWow64Process2=function)

    monkeypatch.setattr(C, "WinDLL", load, raising=False)
    return function, loads


def legacy_query(monkeypatch, *, architecture=9, error=None):
    def query(info_out):
        # Validate the real SYSTEM_INFO ABI, not a fake replacement structure.
        info = info_out._obj
        assert C.sizeof(info) == (48 if C.sizeof(C.c_void_p) == 8 else 36)
        assert type(info).wProcessorArchitecture.offset == 0
        assert type(info).dwPageSize.offset == 4
        assert type(info).lpMinimumApplicationAddress.offset == 8
        assert type(info).dwActiveProcessorMask.offset == (24 if C.sizeof(C.c_void_p) == 8 else 16)
        if error is not None:
            raise error
        if architecture is not None:
            info.wProcessorArchitecture = architecture

    function = Function(query)
    monkeypatch.setattr(C, "WinDLL", lambda *a, **k: SimpleNamespace(
        GetNativeSystemInfo=function), raising=False)
    return function


@pytest.fixture(autouse=True)
def forbid_helpers(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("host validation reached a helper-capable API")

    for name in ("machine", "uname", "win32_ver", "_syscmd_ver"):
        monkeypatch.setattr(platform, name, forbidden)
    for name in ("Popen", "check_output", "run", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, forbidden)
    import os
    for name in ("system", "popen", "startfile", "spawnl", "spawnle", "spawnlp",
                 "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    if sys.platform == "win32":
        import _winapi
        monkeypatch.setattr(_winapi, "CreateProcess", forbidden)
    # Every test must supply fake query bindings; no test can load a live DLL.
    monkeypatch.setattr(C, "WinDLL", forbidden, raising=False)
    yield calls
    assert calls == []


@pytest.mark.parametrize("build", [10240, 10586, 15063, 16299, 19045, 22000, 26100])
def test_valid_windows_native_amd64_and_64_bit_python_pass_without_helpers(
        monkeypatch, build, forbid_helpers):
    host(monkeypatch, build=build)
    query, loads = modern_query(monkeypatch)
    T._validate_windows_host()
    assert len(query.calls) == 1
    assert loads == [("kernel32", {"use_last_error": True})]
    assert query.restype is C.c_int32
    assert query.argtypes == [C.c_void_p, C.POINTER(C.c_uint16), C.POINTER(C.c_uint16)]
    assert forbid_helpers == []


def test_non_windows_rejects_before_version_or_native_query(monkeypatch):
    monkeypatch.setattr(T, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(T, "sys", SimpleNamespace())
    with pytest.raises(T.TerminalContractError, match="Windows only"):
        T._validate_windows_host()


@pytest.mark.parametrize("metadata", [
    {"major": 6}, {"major": 11}, {"build": 10239}, {"product": 2},
    {"product": 3}, {"bits": 32},
])
def test_unsupported_os_or_interpreter_rejects_before_native_query(monkeypatch, metadata):
    host(monkeypatch, **metadata)
    with pytest.raises(T.TerminalContractError, match="Windows 10/11"):
        T._validate_windows_host()


@pytest.mark.parametrize("native", [0, 0x014C, 0x01C4, 0x0200, 0xAA64, 0xFFFF, None])
def test_unsupported_or_unwritten_native_machine_fails_closed(monkeypatch, native):
    host(monkeypatch)
    # AMD64 process evidence cannot override an unsupported native host.
    query, _ = modern_query(monkeypatch, native=native, process=0x8664)
    with pytest.raises(T.TerminalContractError, match="Windows 10/11"):
        T._validate_windows_host()
    assert len(query.calls) == 1


@pytest.mark.parametrize("native", [0x8664, 0xAA64])
def test_32_bit_python_is_rejected_on_supported_and_unsupported_native_hosts(monkeypatch, native):
    host(monkeypatch, bits=32)
    query, _ = modern_query(monkeypatch, native=native, process=0x014C)
    with pytest.raises(T.TerminalContractError, match="64-bit Python"):
        T._validate_windows_host()
    assert query.calls == []


@pytest.mark.parametrize("build", [10240, 22000])
@pytest.mark.parametrize("failure", ["false", "exception"])
def test_native_query_failure_does_not_fall_back(monkeypatch, build, failure):
    host(monkeypatch, build=build)
    query, _ = modern_query(monkeypatch, result=0 if failure == "false" else 1,
                           error=OSError("query unavailable") if failure == "exception" else None)
    with pytest.raises(T.TerminalContractError, match="cannot determine native"):
        T._validate_windows_host()
    assert len(query.calls) == 1


def test_native_library_load_failure_fails_closed(monkeypatch):
    host(monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(C, "WinDLL", unavailable)
    with pytest.raises(T.TerminalContractError, match="cannot determine native"):
        T._validate_windows_host()


@pytest.mark.parametrize("build", [10240, 10586, 15063, 16298])
def test_legacy_windows_build_retains_native_amd64_support(monkeypatch, build):
    host(monkeypatch, build=build)
    query = legacy_query(monkeypatch)
    T._validate_windows_host()
    assert len(query.calls) == 1 and query.restype is None
    assert query.argtypes == [C.POINTER(type(query.calls[0][0]._obj))]


@pytest.mark.parametrize("architecture", [0, 5, 6, 12, 0xFFFF, None])
def test_legacy_unsupported_or_unwritten_architecture_fails_closed(monkeypatch, architecture):
    host(monkeypatch, build=10240)
    query = legacy_query(monkeypatch, architecture=architecture)
    with pytest.raises(T.TerminalContractError, match="Windows 10/11"):
        T._validate_windows_host()
    assert len(query.calls) == 1


def test_legacy_query_exception_fails_closed(monkeypatch):
    host(monkeypatch, build=10240)
    query = legacy_query(monkeypatch, error=OSError("query unavailable"))
    with pytest.raises(T.TerminalContractError, match="cannot determine native"):
        T._validate_windows_host()
    assert len(query.calls) == 1


@pytest.mark.parametrize("build", [16299, 22000, 26100])
def test_missing_modern_query_never_accepts_emulated_system_info(monkeypatch, build):
    host(monkeypatch, build=build)
    query = legacy_query(monkeypatch, architecture=9)
    with pytest.raises(T.TerminalContractError, match="cannot determine native"):
        T._validate_windows_host()
    assert query.calls == []


def test_architecture_environment_cannot_override_native_evidence(monkeypatch):
    host(monkeypatch)
    T.os.environ.update(PROCESSOR_ARCHITECTURE="AMD64", PROCESSOR_ARCHITEW6432="AMD64")
    modern_query(monkeypatch, native=0xAA64)
    with pytest.raises(T.TerminalContractError):
        T._validate_windows_host()
    T.os.environ.update(PROCESSOR_ARCHITECTURE="ARM64", PROCESSOR_ARCHITEW6432="ARM64")
    modern_query(monkeypatch, native=0x8664)
    T._validate_windows_host()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path preparation")
def test_preparation_reaches_real_host_check_without_any_helper_or_backend(
        monkeypatch, tmp_path, forbid_helpers):
    # Structural test PE bytes only; never a runnable child or runtime instance.
    image = tmp_path / "host-check.exe"
    image.write_bytes(console_pe_bytes())
    request = T.TerminalExecutionRequest(executable=str(image), argv=["literal"], cwd=str(tmp_path))
    query, _ = modern_query(monkeypatch)
    target = T.prepare_terminal_execution(request)
    assert len(query.calls) == 1
    assert target.contract_version == "terminal-execution-target/v2"
    assert target.shell is False and target.argv == ("literal",)
    assert forbid_helpers == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path preparation")
def test_native_query_failure_stops_preparation_before_filesystem_or_target(monkeypatch, tmp_path):
    request = T.TerminalExecutionRequest(executable=str(tmp_path / "fake.exe"), argv=[], cwd=str(tmp_path))
    modern_query(monkeypatch, result=0)

    def forbidden(*args, **kwargs):
        raise AssertionError("preparation continued after native query failure")

    monkeypatch.setattr(T, "_freeze_windows_environment", forbidden)
    with pytest.raises(T.TerminalContractError, match="cannot determine native"):
        T.prepare_terminal_execution(request)


def test_import_does_not_load_native_library_backend_or_register_runtime(monkeypatch):
    import core.runtime as registry
    imports = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        imports.append(name)
        assert name not in {"capabilities.terminal_runtime", "capabilities.terminal_win32",
                            "capabilities.terminal_handler"}
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("import attempted runtime registration")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(registry.CapabilityRuntimeRegistry, "register", forbidden)
    name = "_terminal_host_import_probe"
    spec = importlib.util.spec_from_file_location(name, T.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert "ctypes" not in imports
