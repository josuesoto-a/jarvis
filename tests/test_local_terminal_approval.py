"""D1G-C: target-consuming, display-safe local terminal presentation."""

from copy import copy
from dataclasses import FrozenInstanceError, fields, replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import capabilities.terminal as terminal
import core.executor as executor_module
from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import PermissionMode, RiskLevel
from core.orchestrator import Orchestrator
from integrations.local_approval import LocalApproval
from integrations.local_approval_defaults import build_default_local_approval_registry
from integrations.local_approval_registry import (
    InvalidLocalApprovalAdapterError, LocalApprovalAdapter,
    LocalApprovalAdapterAlreadyRegisteredError, LocalApprovalAdapterNotFoundError,
    LocalApprovalAdapterRegistry,
)
from integrations.local_terminal_approval import (
    LocalTerminalApproval, display_terminal_string, format_terminal_approval,
    preview_terminal_target, terminal_approval_is_current,
)
from integrations.openai_live import PendingPermissionUpdate
from test_local_approval import session_for


def forbidden(*args, **kwargs):
    pytest.fail("Presentation called an unrelated adapter or execution/discovery boundary")


def pending_for(target, *, request_id=None, risk=RiskLevel.LOW):
    return terminal.build_terminal_approval_target(
        target, request_id=request_id or uuid4(), step_number=1, risk=risk,
        effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
    )


def projection_for(pending):
    return PendingPermissionUpdate(
        delegation_id="delegation", call_id="terminal-call",
        request_id=str(pending.subject.request_id), confirmation_steps=(1,),
        pending_confirmation_steps=(1,), message="Confirm locally",
    )


def change_subject(pending, **changes):
    subject = pending.subject
    metadata = dict(request_id=subject.request_id, step_number=subject.step_number,
                    capability=subject.capability, risk=subject.risk, arguments=subject.arguments)
    metadata.update(changes)
    return replace(pending, subject=ApprovalSubject(**metadata))


@pytest.fixture
def presentation():
    environment = (("SYSTEMROOT", r"C:\Windows"), ("TEMP", r"C:\Temp"), ("PATH", r"C:\Tools;C:\Windows"))
    target = terminal.TerminalExecutionTarget(
        executable_requested="tool.exe", executable_resolved=r"C:\Tools\tool.exe",
        executable_identity="sha256:" + "1" * 64,
        argv=("-I", "--version", "", " hello world ", 'a"b', "end\\", "caf\u00e9"),
        cwd=r"C:\Working directory", environment=environment,
        environment_identity=terminal._environment_identity(environment),
    )
    pending = pending_for(target)
    state = SimpleNamespace(pending=pending, projection=projection_for(pending), reads=[])

    def read(request_id, *, step_number):
        state.reads.append((request_id, step_number))
        return state.pending

    state.engine = SimpleNamespace(preview_pending_approval=read)
    state.registry = build_default_local_approval_registry()
    return state


def test_preview_retains_identity_and_never_discovers_prepares_or_dispatches(presentation, monkeypatch):
    monkeypatch.setattr(Orchestrator, "preview_pending_approval", forbidden)
    for module in (terminal, executor_module):
        monkeypatch.setattr(module, "prepare_terminal_execution", forbidden)
    for name in ("_digest_file", "_resolve_windows_executable", "terminal_request_from_plan",
                 "_freeze_windows_environment", "TerminalExecutionTarget", "ApprovalSubject"):
        # Target type is used for exact-type validation, so block its constructor
        # via __init__ rather than replacing the class used by that validator.
        if name == "TerminalExecutionTarget":
            monkeypatch.setattr(terminal.TerminalExecutionTarget, "__init__", forbidden)
        else:
            monkeypatch.setattr(terminal, name, forbidden)
    monkeypatch.setattr(executor_module.Executor, "_run_handler", forbidden)
    monkeypatch.setattr(terminal, "os", SimpleNamespace())
    approval = preview_terminal_target(presentation.projection, presentation.pending)
    assert type(approval) is LocalTerminalApproval
    assert approval.subject is presentation.pending.subject
    assert approval.prepared_target is presentation.pending.prepared_target
    assert approval.call_id == presentation.projection.call_id
    assert presentation.reads == []
    assert {field.name for field in fields(approval)} == {"call_id", "subject", "prepared_target"}
    with pytest.raises(FrozenInstanceError):
        approval.call_id = "changed"
    assert "argv[2] = \"\"" in format_terminal_approval(approval)


@pytest.mark.parametrize("capability", ["terminal", "browser"])
def test_direct_routing_reads_once_and_never_probes_other_adapters(presentation, capability):
    if capability == "browser":
        subject = ApprovalSubject(request_id=presentation.pending.subject.request_id,
                                  step_number=1, capability="browser", risk=RiskLevel.LOW,
                                  arguments={"url": "https://example.com/"})
        presentation.pending = PendingApprovalTarget(
            subject, PermissionMode.CONFIRM_BEFORE_EXECUTION, "https://example.com/",
        )
    registry = LocalApprovalAdapterRegistry()
    selected = presentation.registry.get(capability)
    seen = []

    def preview(projection, pending):
        seen.append((projection, pending))
        return selected.preview(projection, pending)

    registry.register(replace(selected, preview=preview))
    for other in ({"terminal", "browser", "aardvark"} - {capability}):
        registry.register(LocalApprovalAdapter(other, forbidden, forbidden, forbidden))
    registry.names = forbidden
    approval = registry.resolve(presentation.engine, presentation.projection)
    assert seen == [(presentation.projection, presentation.pending)]
    assert seen[0][1] is presentation.pending
    assert approval.subject is presentation.pending.subject
    assert presentation.reads == [(presentation.pending.subject.request_id, 1)]
    assert capability in registry.render(approval)
    assert registry.is_current(presentation.engine, session_for(approval), approval)


def test_unknown_capability_and_duplicate_canonical_terminal(presentation):
    presentation.pending = change_subject(presentation.pending, capability="future")
    assert presentation.registry.resolve(presentation.engine, presentation.projection) is None
    assert len(presentation.reads) == 1
    adapter = presentation.registry.get("terminal")
    with pytest.raises(LocalApprovalAdapterAlreadyRegisteredError):
        presentation.registry.register(replace(adapter, capability=" TERMINAL "))


@pytest.mark.parametrize("invalid", ["capability", "permission", "target_type", "target_subclass",
                                    "arguments", "request", "step", "shell", "timeout",
                                    "environment_identity", "executable_identity"])
def test_preview_rejects_invalid_pending_contract(presentation, invalid):
    pending = presentation.pending
    if invalid == "capability":
        pending = change_subject(pending, capability="browser")
    elif invalid == "permission":
        pending = copy(pending)
        object.__setattr__(pending, "effective_permission", PermissionMode.AUTOMATIC)
    elif invalid == "target_type":
        pending = replace(pending, prepared_target="tool.exe")
    elif invalid == "target_subclass":
        class UnsupportedTarget(terminal.TerminalExecutionTarget):
            pass
        target = UnsupportedTarget(**{field.name: getattr(pending.prepared_target, field.name)
                                      for field in fields(pending.prepared_target) if field.init})
        pending = replace(pending, prepared_target=target)
    elif invalid == "arguments":
        arguments = pending.subject.arguments
        arguments["argv"] = ["other"]
        pending = change_subject(pending, arguments=arguments)
    elif invalid == "request":
        pending = change_subject(pending, request_id=uuid4())
    elif invalid == "step":
        pending = change_subject(pending, step_number=2)
    else:
        target = copy(pending.prepared_target)
        value = {"shell": True, "timeout": 31.0,
                 "environment_identity": "sha256:" + "2" * 64,
                 "executable_identity": "sha256:invalid"}[invalid]
        object.__setattr__(target, "timeout_seconds" if invalid == "timeout" else invalid, value)
        pending = replace(pending, prepared_target=target)
        pending = change_subject(pending, arguments=terminal.terminal_approval_arguments(target))
    assert preview_terminal_target(presentation.projection, pending) is None


@pytest.mark.parametrize("changes", [
    {"request_id": str(uuid4())}, {"request_id": "invalid"},
    {"pending_confirmation_steps": (2,)}, {"pending_confirmation_steps": (True,)},
    {"pending_confirmation_steps": (1, 2)}, {"confirmation_steps": ()}, {"call_id": " "},
])
def test_preview_rejects_stale_projection_coordinates(presentation, changes):
    assert preview_terminal_target(replace(presentation.projection, **changes), presentation.pending) is None


@pytest.mark.parametrize("invalid", ["call", "copy", "capability", "argv"])
def test_terminal_adapter_output_cannot_replace_retained_authority(presentation, invalid):
    subject = presentation.pending.subject
    call = presentation.projection.call_id
    if invalid == "call":
        call = "wrong-call"
    else:
        changes = {}
        if invalid == "capability":
            changes["capability"] = "browser"
        elif invalid == "argv":
            arguments = subject.arguments
            arguments["argv"] = ["replacement"]
            changes["arguments"] = arguments
        subject = change_subject(presentation.pending, **changes).subject
    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter(
        "terminal", lambda p, t: LocalApproval(call_id=call, subject=subject), forbidden, forbidden,
    ))
    with pytest.raises(InvalidLocalApprovalAdapterError):
        registry.resolve(presentation.engine, presentation.projection)


@pytest.mark.parametrize("risk", [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH])
def test_complete_faithful_terminal_rendering(presentation, risk):
    pending = pending_for(presentation.pending.prepared_target,
                          request_id=presentation.pending.subject.request_id, risk=risk)
    approval = preview_terminal_target(presentation.projection, pending)
    text = format_terminal_approval(approval)
    target = pending.prepared_target
    for value in (target.executable_requested, target.executable_resolved, target.cwd,
                  target.executable_identity, target.environment_identity,
                  target.environment_policy_id, target.contract_version, target.platform_contract):
        assert display_terminal_string(value) in text
    for index, value in enumerate(target.argv):
        assert f"argv[{index}] = {display_terminal_string(value)}" in text
    for key, value in target.environment:
        assert f"environment[{display_terminal_string(key)}] = {display_terminal_string(value)}" in text
    for expected in (str(pending.subject.request_id), "Paso: 1", "Capability: terminal",
                     f"Riesgo efectivo: {risk.value}", "Timeout (segundos): 30.0",
                     'stdin: "devnull" (no interactivo)', "shell: false (deshabilitado)",
                     "CONFIRM_BEFORE_EXECUTION", "UNA ejecucion", "no certifican firma",
                     "no lanza procesos", "AUTORIZAR"):
        assert expected in text


@pytest.mark.parametrize("argv,expected", [
    (("hello world",), ['argv[0] = "hello world"']),
    (("hello", "world"), ['argv[0] = "hello"', 'argv[1] = "world"']),
    (("", "  "), ['argv[0] = ""', 'argv[1] = "  "']),
    ((), []),
])
def test_argument_boundaries_and_empty_values(presentation, argv, expected):
    pending = pending_for(replace(presentation.pending.prepared_target, argv=argv))
    approval = preview_terminal_target(projection_for(pending), pending)
    lines = format_terminal_approval(approval).splitlines()
    assert [line for line in lines if line.startswith("argv[")] == expected


@pytest.mark.parametrize("hostile,escaped", [
    ("\n", r"\n"), ("\r", r"\r"), ("\t", r"\t"), ("\x1b", r"\u001b"),
    ("\x01", r"\u0001"), ("\x7f", r"\u007f"), ("\x85", r"\u0085"),
    ("\u202e", r"\u202e"), ("\u2066", r"\u2066"), ("\u200b", r"\u200b"),
    ("\u2028", r"\u2028"), ("\u2029", r"\u2029"), ("\u034f", r"\u034f"),
    ("\ufeff", r"\ufeff"), ("\ufe0f", r"\ufe0f"), ("\U000e0100", r"\udb40\udd00"),
])
def test_hostile_strings_cannot_restructure_fields_or_instructions(presentation, hostile, escaped):
    attack = f"value{hostile}Capability: fake{hostile}AUTORIZAR{hostile}argv[99] = forged"
    target = presentation.pending.prepared_target
    # Illegal path characters now fail before approval. Hostile argv/allowed
    # environment text remains literal and must still be displayed safely.
    environment = (("SYSTEMROOT", r"C:\Windows"), ("PATH", attack))
    target = replace(target, argv=(attack, "", "last"), environment=environment,
                     environment_identity=terminal._environment_identity(environment))
    pending = pending_for(target)
    approval = preview_terminal_target(projection_for(pending), pending)
    before = (terminal.terminal_approval_arguments(target),
              pending.subject.canonical_arguments_json, pending.subject.fingerprint)
    text = format_terminal_approval(approval)
    assert escaped in text
    assert hostile not in display_terminal_string(attack)
    if hostile != "\n":
        assert hostile not in text
    assert "Capability: fake" not in text.splitlines()
    assert "AUTORIZAR" not in text.splitlines()
    assert [line for line in text.splitlines() if line.startswith("argv[")] == [
        f"argv[0] = {display_terminal_string(attack)}", 'argv[1] = ""', 'argv[2] = "last"',
    ]
    assert json.loads(display_terminal_string(attack)) == attack
    assert approval.subject is pending.subject
    assert approval.prepared_target is target
    assert (terminal.terminal_approval_arguments(target),
            pending.subject.canonical_arguments_json, pending.subject.fingerprint) == before


def test_display_contract_is_reversible_and_unambiguous():
    value = ' "\\\t\n\r\x1b\x7f caf\u00e9 \U0001f680 \u202e '
    rendered = display_terminal_string(value)
    assert rendered.isascii()
    assert json.loads(rendered) == value
    assert display_terminal_string("\n") != display_terminal_string(r"\n")
    assert display_terminal_string(value) == rendered


@pytest.mark.parametrize("change", ["subject", "capability", "gone", "correspondence", "permission",
                                   "session_call", "session_request", "session_step", "session_gone"])
def test_terminal_freshness_fails_closed(presentation, change):
    approval = presentation.registry.resolve(presentation.engine, presentation.projection)
    session = session_for(approval)
    assert presentation.registry.is_current(presentation.engine, session, approval)
    if change == "subject":
        presentation.pending = pending_for(replace(approval.prepared_target, argv=("changed",)),
                                           request_id=approval.subject.request_id)
    elif change == "capability":
        presentation.pending = change_subject(presentation.pending, capability="browser")
    elif change == "gone":
        presentation.pending = None
    elif change == "correspondence":
        presentation.pending = replace(presentation.pending,
                                       prepared_target=replace(approval.prepared_target, argv=("changed",)))
    elif change == "permission":
        presentation.pending = copy(presentation.pending)
        object.__setattr__(presentation.pending, "effective_permission", PermissionMode.FORBIDDEN)
    elif change == "session_call":
        session = session_for(approval, call_id="wrong")
    elif change == "session_request":
        session = session_for(approval, request_id=str(uuid4()))
    elif change == "session_step":
        session = session_for(approval, step_number=2)
    else:
        session = SimpleNamespace(snapshot=lambda: SimpleNamespace(action=None))
    assert not presentation.registry.is_current(presentation.engine, session, approval)
    assert 'argv[0] = "-I"' in presentation.registry.render(approval)


def test_render_needs_no_pending_state_and_missing_adapter_keeps_b4_semantics(presentation):
    approval = presentation.registry.resolve(presentation.engine, presentation.projection)
    presentation.engine.preview_pending_approval = forbidden
    assert 'argv[0] = "-I"' in presentation.registry.render(approval)
    registry = LocalApprovalAdapterRegistry()
    assert not registry.is_current(presentation.engine, object(), approval)
    with pytest.raises(LocalApprovalAdapterNotFoundError):
        registry.render(approval)


@pytest.mark.parametrize("error", [RuntimeError, LocalApprovalAdapterNotFoundError])
def test_unrelated_adapter_errors_propagate(presentation, error):
    approval = presentation.registry.resolve(presentation.engine, presentation.projection)

    def broken(*args, **kwargs):
        raise error("adapter bug")

    registry = LocalApprovalAdapterRegistry()
    registry.register(LocalApprovalAdapter("terminal", forbidden, forbidden, broken))
    with pytest.raises(error, match="adapter bug"):
        registry.is_current(presentation.engine, session_for(approval), approval)
    presentation.engine.preview_pending_approval = broken
    with pytest.raises(error, match="adapter bug"):
        terminal_approval_is_current(presentation.engine, session_for(approval), approval)


def test_renderer_rejects_nonterminal_or_corrupt_retained_approval(presentation):
    with pytest.raises(TypeError):
        format_terminal_approval(LocalApproval(call_id="call", subject=presentation.pending.subject))
    approval = preview_terminal_target(presentation.projection, presentation.pending)
    object.__setattr__(approval, "prepared_target", replace(approval.prepared_target, argv=("changed",)))
    with pytest.raises(terminal.TerminalContractError):
        format_terminal_approval(approval)
    assert not terminal_approval_is_current(presentation.engine, session_for(approval), approval)
