"""D1G-D1a pure/recording tests: never launch fixtures or real child processes."""

import ast
from copy import copy
from dataclasses import fields, replace, FrozenInstanceError
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest

import capabilities.terminal as terminal
from core.approval import ApprovalSubject
from core.contracts import ActionStatus, RiskLevel
from integrations.local_terminal_approval import format_terminal_approval, preview_terminal_target
from terminal_fixtures import console_pe_bytes
from test_terminal_approval_integration import system, waiting, claim


def pure_target():
    environment = (("SYSTEMROOT", r"C:\Windows"), ("PATH", r"C:\Tools"))
    return terminal.TerminalExecutionTarget(
        executable_requested="tool.exe", executable_resolved=r"C:\Tools\tool.exe",
        executable_identity="sha256:" + "1" * 64, argv=(), cwd=r"C:\Work",
        environment=environment, environment_identity=terminal._environment_identity(environment),
    )


def fingerprint(target):
    return ApprovalSubject(request_id=REQUEST_ID, step_number=1, capability="terminal",
                           risk=RiskLevel.LOW,
                           arguments=terminal.terminal_approval_arguments(target)).fingerprint


REQUEST_ID = uuid4()


@pytest.mark.parametrize("machine", [0x014C, 0x8664])
def test_supported_console_header_shapes_without_execution(machine):
    image = console_pe_bytes(machine=machine)
    assert terminal.inspect_windows_console_pe(image, file_size=len(image)) == machine


@pytest.mark.parametrize("kind", [
    "empty", "short", "dos", "signature", "offset", "offset_inside_dos", "oversized_prefix",
    "truncated_table", "dll", "not_executable", "arm", "gui", "driver", "optional_size",
    "optional_magic", "zero_sections", "many_sections", "zero_entrypoint", "large_entrypoint",
    "headers_size", "directories", "managed", "raw_offset", "raw_size", "virtual_size",
    "section_address", "nonexecutable_entrypoint", "file_size", "oversized_file",
])
def test_unsupported_or_malformed_pe_shapes_fail_closed(kind):
    image = bytearray(console_pe_bytes())
    file_size = len(image)
    mutations = {
        "offset": (0x3C, "I", 0xFFFFFFFF), "offset_inside_dos": (0x3C, "I", 2),
        "dll": (150, "H", 0x2002), "not_executable": (150, "H", 0),
        "arm": (132, "H", 0xAA64), "gui": (220, "H", 2), "driver": (220, "H", 1),
        "optional_size": (148, "H", 32), "optional_magic": (152, "H", 0x10B),
        "zero_sections": (134, "H", 0), "many_sections": (134, "H", 97),
        "zero_entrypoint": (168, "I", 0), "large_entrypoint": (168, "I", 0xFFFFFFFF),
        "headers_size": (212, "I", 16), "directories": (260, "I", 17),
        "managed": (376, "I", 0x1000), "raw_offset": (412, "I", 1),
        "raw_size": (408, "I", 0xFFFFFFFF), "virtual_size": (400, "I", 0xFFFFFFFF),
        "section_address": (404, "I", 0xFFFFFFFF), "nonexecutable_entrypoint": (428, "I", 0),
    }
    if kind in mutations:
        offset, form, value = mutations[kind]
        struct.pack_into("<" + form, image, offset, value)
    elif kind == "empty":
        image = bytearray()
    elif kind == "short":
        image = image[:63]
    elif kind == "dos":
        image[:2] = b"NE"
    elif kind == "signature":
        image[128:132] = b"NE\x00\x00"
    elif kind == "oversized_prefix":
        image.extend(bytes(terminal.TERMINAL_PE_HEADER_BYTES))
        file_size = len(image)
    elif kind == "truncated_table":
        image = image[:420]
    elif kind == "file_size":
        file_size = 100
    elif kind == "oversized_file":
        file_size = terminal.TERMINAL_EXECUTABLE_BYTES_LIMIT + 1
    with pytest.raises(terminal.TerminalContractError):
        terminal.inspect_windows_console_pe(bytes(image), file_size=file_size)


@pytest.mark.parametrize("path", [
    r"\\server\share\tool.exe", r"\\?\C:\tool.exe", r"\\.\C:\tool.exe",
    r"\??\C:\tool.exe", r"C:\tool.exe:stream", r"C:\dir:stream\tool.exe",
    r"C:\NUL.exe", r"C:\con\tool.exe", r"C:\tool.exe.", "C:\\tool.exe ",
    r"C:\bad?\tool.exe", r"C:\bad|\tool.exe", "C:\\bad\n\\tool.exe",
    r"C:tool.exe", r".\tool.exe", r"C:\tool.bat", r"C:\tool.cmd",
])
def test_unsupported_executable_path_forms_rejected_without_filesystem_access(path):
    with pytest.raises(terminal.TerminalContractError):
        terminal.TerminalExecutionRequest(executable=path, argv=(), cwd=r"C:\Work")


@pytest.mark.parametrize("digest", [
    "SHA256:" + "1" * 64, "sha256:" + "A" * 64, "sha256:" + "z" * 64,
    "sha256:" + "0" * 63, "sha256:" + "0" * 65, "md5:" + "0" * 64,
    "sha256:" + "0" * 63 + " ",
])
def test_digest_is_exact_canonical_identity(digest):
    with pytest.raises(terminal.TerminalContractError, match="SHA-256"):
        replace(pure_target(), executable_identity=digest)


def test_canonical_digest_accepted_without_normalization():
    value = "sha256:" + "0123456789abcdef" * 4
    assert replace(pure_target(), executable_identity=value).executable_identity == value


@pytest.mark.parametrize("argv,tail", [
    ((), ""), (("",), ' ""'), (("", ""), ' "" ""'),
    (("a b",), ' "a b"'), (("a\tb",), ' "a\tb"'),
    (('a"b',), ' a\\"b'), (('a\\"b',), ' a\\\\\\"b'),
    (("end\\",), " end\\"), (("with space\\",), ' "with space\\\\"'),
    (("  leading", "trailing  "), ' "  leading" "trailing  "'),
    (("caf\u00e9", "\U0001f680"), " caf\u00e9 \U0001f680"),
    (("&&", "|", "%PATH%", "$(literal)"), " && | %PATH% $(literal)"),
    (("a\nb",), " a\nb"),
])
def test_pinned_crt_serialization_vectors(argv, tail):
    executable = r"C:\Tools\tool.exe"
    assert terminal.serialize_windows_command_line(executable, argv) == executable + tail
    assert terminal.serialize_windows_command_line(executable, list(argv)) == executable + tail


def test_executable_with_spaces_synthesizes_argv_zero():
    assert terminal.serialize_windows_command_line(r"C:\Program Files\tool.exe", ()) == '"C:\\Program Files\\tool.exe"'


@pytest.mark.parametrize("argv", [("a\x00b",), ("\ud800",), ("\udfff",)])
def test_invalid_launch_text_rejected(argv):
    with pytest.raises(terminal.TerminalContractError):
        terminal.serialize_windows_command_line(r"C:\tool.exe", argv)


def test_command_line_size_counts_utf16_and_final_nul():
    executable = r"C:\t.exe"
    remaining = terminal.TERMINAL_COMMAND_LINE_UTF16_LIMIT - len(executable) - 2
    assert len(terminal.serialize_windows_command_line(executable, ("x" * remaining,))) + 1 == terminal.TERMINAL_COMMAND_LINE_UTF16_LIMIT
    with pytest.raises(terminal.TerminalContractError, match="UTF-16"):
        terminal.serialize_windows_command_line(executable, ("x" * (remaining + 1),))
    with pytest.raises(terminal.TerminalContractError, match="UTF-16"):
        terminal.serialize_windows_command_line(executable, ("\U0001f680" * (remaining // 2 + 1),))
    with pytest.raises(terminal.TerminalContractError, match="count"):
        terminal.serialize_windows_command_line(executable, ("",) * (terminal.TERMINAL_ARGV_LIMIT + 1))


@pytest.mark.parametrize("argv", [("a b",), ("a", "b"), ("", "a", "b")])
def test_argv_structure_and_serialization_policy_bind_identity(argv):
    target = replace(pure_target(), argv=argv)
    assert fingerprint(target) != fingerprint(pure_target())
    assert fingerprint(target) != fingerprint(replace(pure_target(), argv=("a b", "")))


@pytest.mark.parametrize("key", ["PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR", "USERPROFILE", "PATHEXT"])
def test_allowlisted_case_collisions_rejected_even_if_equal(key):
    source = {"SYSTEMROOT": r"C:\Windows", key: r"C:\Windows", key.lower(): r"C:\Windows"}
    with pytest.raises(terminal.TerminalContractError, match="colliding"):
        terminal._freeze_windows_environment(source)


@pytest.mark.parametrize("value", [None, "", "relative", r"\\server\windows", r"C:\Windows:stream", "C:\\bad\x00"])
def test_systemroot_required_and_valid_before_identity(value):
    source = {} if value is None else {"SYSTEMROOT": value}
    with pytest.raises(terminal.TerminalContractError):
        terminal._freeze_windows_environment(source)


def test_environment_identity_and_transport_order_are_independent_and_exact():
    source = {"Path": r"C:\Tools", "TEMP": r"C:\Temp", "SystemRoot": r"C:\Windows", "OPENAI_API_KEY": "excluded"}
    frozen = terminal._freeze_windows_environment(source)
    assert frozen == (("SYSTEMROOT", r"C:\Windows"), ("TEMP", r"C:\Temp"), ("PATH", r"C:\Tools"))
    assert terminal._freeze_windows_environment(dict(reversed(list(source.items())))) == frozen
    assert terminal.build_windows_environment_block(dict(frozen)) == "PATH=C:\\Tools\x00SYSTEMROOT=C:\\Windows\x00TEMP=C:\\Temp\x00\x00"
    before = terminal._environment_identity(frozen)
    source["Path"] = "changed after freeze"
    assert terminal._environment_identity(frozen) == before
    assert terminal._environment_identity(terminal._freeze_windows_environment(source)) != before
    with pytest.raises(terminal.TerminalContractError):
        terminal.build_windows_environment_block({"SYSTEMROOT": r"C:\Windows", "SECRET": "bad"})


def test_environment_size_counts_utf16_terminators():
    base = {"SYSTEMROOT": r"C:\Windows", "PATH": ""}
    size = len(terminal.build_windows_environment_block(base).encode("utf-16-le")) // 2
    base["PATH"] = "x" * (terminal.TERMINAL_ENVIRONMENT_UTF16_LIMIT - size)
    assert len(terminal.build_windows_environment_block(base).encode("utf-16-le")) // 2 == terminal.TERMINAL_ENVIRONMENT_UTF16_LIMIT
    base["PATH"] += "x"
    with pytest.raises(terminal.TerminalContractError, match="UTF-16"):
        terminal.build_windows_environment_block(base)


@pytest.mark.parametrize("name", [descriptor.name for descriptor in fields(terminal.TerminalRuntimePolicy)])
def test_every_nested_policy_field_is_identity_bound_and_rejected_if_changed(name):
    target = pure_target()
    changed = copy(target)
    policy = copy(target.runtime_policy)
    value = getattr(policy, name)
    replacement = (tuple(reversed(value)) if type(value) is tuple else
                   value + 1 if type(value) in {int, float} else value + "/changed")
    object.__setattr__(policy, name, replacement)
    object.__setattr__(changed, "runtime_policy", policy)
    assert fingerprint(changed) != fingerprint(target)
    with pytest.raises(terminal.TerminalContractError, match="policy"):
        changed.validate()


def test_policy_projection_complete_detached_and_versioned():
    target = pure_target()
    projection = terminal.terminal_approval_arguments(target)
    assert set(projection) == {descriptor.name for descriptor in fields(target)}
    assert set(projection["runtime_policy"]) == {descriptor.name for descriptor in fields(target.runtime_policy)}
    assert json.loads(json.dumps(projection)) == projection
    projection["runtime_policy"]["creation_flags"].append("forged")
    assert len(target.runtime_policy.creation_flags) == 4
    with pytest.raises(FrozenInstanceError):
        target.runtime_policy.stdout_bytes_limit = 1
    for field, old in (("contract_version", "terminal-execution-target/v1"),
                       ("environment_policy_id", "windows-minimal/v1"),
                       ("platform_contract", "windows-native/v1")):
        changed = copy(target)
        object.__setattr__(changed, field, old)
        with pytest.raises(terminal.TerminalContractError):
            changed.validate()
    assert target.runtime_policy.stdout_bytes_limit == target.runtime_policy.stderr_bytes_limit == 64 * 1024
    assert target.timeout_seconds == 30.0 and target.runtime_policy.cleanup_seconds == 5.0
    assert target.runtime_policy.job_resource_policy == "unresolved-no-activation/v1"
    assert target.stdin_mode == "devnull" and target.shell is False


@pytest.mark.parametrize("major,build,product,machine,bits", [
    (6, 9600, 1, "AMD64", 64), (10, 10000, 1, "AMD64", 64),
    (10, 22000, 3, "AMD64", 64), (10, 22000, 1, "ARM64", 64),
    (10, 22000, 1, "AMD64", 32),
])
def test_unsupported_host_metadata_fails_without_api_calls(monkeypatch, major, build, product, machine, bits):
    monkeypatch.setattr(terminal, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(
        getwindowsversion=lambda: SimpleNamespace(major=major, build=build, product_type=product),
        maxsize=2**(bits - 1) - 1))
    monkeypatch.setattr(terminal, "platform", SimpleNamespace(machine=lambda: machine))
    with pytest.raises(terminal.TerminalContractError, match="Windows 10/11"):
        terminal._validate_windows_host()


@pytest.mark.parametrize("kind", ["missing_systemroot", "invalid_systemroot", "nonexistent_systemroot", "collision", "oversized_argv", "malformed_executable"])
def test_preparation_failure_produces_no_pending_approval_or_recording_dispatch(system, monkeypatch, kind):
    if kind in {"missing_systemroot", "invalid_systemroot", "nonexistent_systemroot", "collision"}:
        source = dict(os.environ)
        source = {key: value for key, value in source.items() if key.upper() != "SYSTEMROOT"}
        if kind != "missing_systemroot":
            source["SYSTEMROOT"] = "invalid" if kind == "invalid_systemroot" else str(system.tmp_path / "missing") if kind == "nonexistent_systemroot" else r"C:\Windows"
        if kind == "collision":
            source["PATH"], source["Path"] = "one", "two"
        monkeypatch.setattr(terminal, "os", SimpleNamespace(name=os.name, environ=source, pathsep=os.pathsep, fstat=os.fstat))
    elif kind == "oversized_argv":
        from core.contracts import ExecutionArgument
        arguments = dict(system.plan.steps[0].arguments)
        arguments["argv"] = ExecutionArgument.literal_string_sequence(["x" * terminal.TERMINAL_COMMAND_LINE_UTF16_LIMIT])
        system.plan = replace(system.plan, steps=(replace(system.plan.steps[0], arguments=arguments),))
    else:
        Path(system.plan.steps[0].arguments["executable"].value).write_bytes(b"legacy, not PE")
    result = system.executor.execute(system.plan)
    assert result.status is ActionStatus.FAILED
    assert result.pending_approval_target is None
    assert system.calls == system.prepared == []


def test_final_resolved_gui_image_rejected_by_preparation(system, monkeypatch):
    requested = Path(system.plan.steps[0].arguments["executable"].value)
    final = system.tmp_path / "gui.exe"
    final.write_bytes(console_pe_bytes(subsystem=2))
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: final if path == requested else resolve(path, *a, **k))
    result = system.executor.execute(system.plan)
    assert result.status is ActionStatus.FAILED
    assert result.pending_approval_target is None and system.calls == []


@pytest.mark.parametrize("kind", ["executable", "cwd"])
def test_resolved_remote_alias_fails_before_remote_stat(system, monkeypatch, kind):
    source = Path(system.plan.steps[0].arguments[kind].value)
    remote = Path(r"\\server\share\tool.exe" if kind == "executable" else r"\\server\share\cwd")
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: remote if path == source else resolve(path, *a, **k))
    result = system.executor.execute(system.plan)
    assert result.status is ActionStatus.FAILED and result.pending_approval_target is None
    assert system.calls == []


def test_hash_includes_exact_inspected_bytes_and_no_exec(tmp_path):
    executable = tmp_path / "fixture.exe"
    contents = console_pe_bytes()
    executable.write_bytes(contents)
    assert terminal._digest_file(executable) == "sha256:" + sha256(contents).hexdigest()


@pytest.mark.parametrize("kind", ["oversized", "nonfile", "changed_mtime", "changed_size"])
def test_opened_file_metadata_and_bounded_reads_fail_closed(monkeypatch, kind):
    image = console_pe_bytes()
    reads = []

    class Opened(BytesIO):
        def fileno(self):
            return 99

        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    observations = []

    def metadata(_):
        observations.append(True)
        return SimpleNamespace(
            st_mode=0o040000 if kind == "nonfile" else 0o100000,
            st_size=terminal.TERMINAL_EXECUTABLE_BYTES_LIMIT + 1 if kind == "oversized"
            else len(image) + (1 if kind == "changed_size" and len(observations) > 1 else 0),
            st_mtime_ns=len(observations) if kind == "changed_mtime" else 1)

    monkeypatch.setattr(Path, "open", lambda *a, **k: Opened(image))
    monkeypatch.setattr(terminal, "os", SimpleNamespace(fstat=metadata))
    with pytest.raises(terminal.TerminalContractError):
        terminal._digest_file(Path("fixture.exe"))
    if kind in {"oversized", "nonfile"}:
        assert reads == []
    else:
        assert reads[0] == terminal.TERMINAL_PE_HEADER_BYTES
        assert all(0 < size <= 1024 * 1024 for size in reads)


def test_first_existing_unsupported_candidate_never_falls_through(system, monkeypatch):
    first, second = system.tmp_path / "first", system.tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "tool.exe").write_bytes(b"unsupported legacy bytes")
    (second / "tool.exe").write_bytes(console_pe_bytes())
    monkeypatch.setenv("PATH", str(first) + os.pathsep + str(second))
    monkeypatch.setenv("PATHEXT", ".EXE")
    with pytest.raises(terminal.TerminalContractError, match="PE"):
        terminal.prepare_terminal_execution(terminal.TerminalExecutionRequest(
            executable="tool.exe", argv=(), cwd=str(system.tmp_path)))


def test_directory_executable_rejected_no_alternate_selection(system):
    directory = system.tmp_path / "directory.exe"
    directory.mkdir()
    with pytest.raises(terminal.TerminalContractError, match="regular file"):
        terminal.prepare_terminal_execution(terminal.TerminalExecutionRequest(
            executable=str(directory), argv=(), cwd=str(system.tmp_path)))


def test_stored_validation_and_transport_do_not_discover_ambient_state(monkeypatch):
    target = pure_target()
    monkeypatch.setattr(terminal, "os", SimpleNamespace())
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("stored target rediscovered filesystem"))
    target.validate()
    assert terminal.serialize_windows_command_line(target.executable_resolved, target.argv) == target.executable_resolved
    assert terminal.build_windows_environment_block(target.environment_mapping()).endswith("\x00\x00")


def test_policy_is_rendered_from_same_subject_target_with_no_execution_claim(system):
    from integrations.openai_live import PendingPermissionUpdate
    pending = waiting(system)
    projection = PendingPermissionUpdate(delegation_id="d", call_id="c",
        request_id=str(system.request.request_id), confirmation_steps=(1,),
        pending_confirmation_steps=(1,), message="Confirm")
    approval = preview_terminal_target(projection, pending)
    assert approval.subject is pending.subject and approval.prepared_target is pending.prepared_target
    text = format_terminal_approval(approval)
    encoded = json.dumps(pending.prepared_target.runtime_policy.approval_arguments(),
                         ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert json.dumps(encoded, ensure_ascii=True) in text
    assert "no lanza procesos" in text and system.calls == []


def test_documented_versions_and_policy_values_match_source():
    text = (Path(__file__).parents[1] / "capabilities/terminal_runtime_policy.md").read_text(encoding="utf-8")
    target = pure_target()
    for value in (target.contract_version, target.environment_policy_id, target.platform_contract,
                  target.runtime_policy.policy_id, target.runtime_policy.serialization_policy,
                  target.runtime_policy.output_policy, target.runtime_policy.decoding_policy,
                  target.runtime_policy.environment_transport_policy, target.runtime_policy.job_resource_policy):
        assert value in text
    assert f"{target.runtime_policy.stdout_bytes_limit // 1024} KiB" in text
    assert f"{target.runtime_policy.read_chunk_bytes // 1024} KiB" in text
    assert f"{int(target.timeout_seconds)}-second" in text
    assert f"{int(target.runtime_policy.cleanup_seconds)} seconds" in text
    for flag in target.runtime_policy.creation_flags:
        assert flag in text


def test_policy_reaches_recording_runtime_unchanged_and_ambient_changes_do_not_merge(system, monkeypatch):
    pending = waiting(system)
    target = pending.prepared_target
    before = target.environment
    monkeypatch.setenv("PATH", "changed after approval")
    monkeypatch.setenv("OPENAI_API_KEY", "still excluded")
    assert claim(system, pending.subject).completed
    assert system.calls == [target] and system.calls[0] is target
    assert target.environment == before
    assert len(system.prepared) == 1


def test_changed_policy_with_old_subject_rejected_no_dispatch(system):
    pending = waiting(system)
    changed = copy(pending.prepared_target)
    policy = replace(changed.runtime_policy, serialization_policy="other/v1")
    object.__setattr__(changed, "runtime_policy", policy)
    report = system.executor.execute(system.plan, confirmed_steps=frozenset({1}),
                                     pending_approval_target=replace(pending, prepared_target=changed))
    assert report.status is ActionStatus.FAILED and system.calls == []


def test_changed_production_sources_have_no_launch_or_runtime_activation():
    root = Path(__file__).parents[1]
    banned = {"subprocess", "Popen", "system", "popen", "create_subprocess_exec",
              "create_subprocess_shell", "CreateProcess", "CreateProcessW", "ShellExecute",
              "WinExec", "WinDLL", "windll"}
    for filename in ("capabilities/terminal.py", "integrations/local_terminal_approval.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [item.name.split(".")[0] for item in node.names]
                modules.append((getattr(node, "module", None) or "").split(".")[0])
                assert not set(modules) & {"subprocess", "ctypes", "_winapi", "win32process", "win32job"}
            if isinstance(node, (ast.Name, ast.Attribute)):
                assert getattr(node, "id", getattr(node, "attr", "")) not in banned
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert not (isinstance(node.value, ast.Constant) and node.value.value is True)
    assert "terminal" not in (root / "core/bootstrap.py").read_text(encoding="utf-8-sig")
