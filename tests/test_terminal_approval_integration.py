"""D1G-A: approval/claim/continuation to inert recording handlers only."""

import ast
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import fields, replace
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

import capabilities.terminal as terminal
import core.executor as executor_module
from core.action_worker import ActionWorker
from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import (
    ActionRequest, ActionStatus, ArgumentSource, CapabilitySpec, ExecutionArgument,
    ExecutionPlan, ExecutionStep, PermissionMode, RiskLevel,
)
from core.executor import Executor, ExecutionCheckpoint, StepExecutionResult
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator, PlanValidationStatus
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from core.transport import to_transport_response
from integrations.local_approval import LocalApproval
from integrations.local_approval_defaults import build_default_local_approval_registry
from integrations.local_approval_registry import LocalApprovalAdapter, LocalApprovalAdapterRegistry
from integrations.openai_live import PendingPermissionUpdate


@pytest.fixture
def system(tmp_path, monkeypatch):
    # The bytes need not be executable. Preparation reads them; nothing launches.
    executable = tmp_path / "recording-input.exe"
    executable.write_bytes(b"inert identity bytes; never a runnable fixture")
    request = ActionRequest("Record exact inputs", "Record exact inputs")
    arguments = {
        "executable": ExecutionArgument.literal(str(executable)),
        "argv": ExecutionArgument.literal_string_sequence(["first", "", "  white space  ", "script.py"]),
        "cwd": ExecutionArgument.literal(str(tmp_path)),
    }
    plan = ExecutionPlan(request.request_id, (
        ExecutionStep(1, "Record", "terminal", arguments=arguments,
                      permission=PermissionMode.AUTOMATIC),
    ), RiskLevel.LOW)
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("terminal", "Inert recording test"))
    registry.register(CapabilitySpec("web_search", "Inert search test"))
    runtimes = CapabilityRuntimeRegistry()
    calls, prepared, validations, evaluations = [], [], [], []
    result = SimpleNamespace(value={"recorded": True})

    def record(inputs):
        assert set(inputs) == {"target"}  # Never browser URL dispatch.
        calls.append(inputs["target"])
        return result.value

    runtimes.register("terminal", record)
    runtimes.register("web_search", lambda _: {"value": "ignored"})
    validator = PlanValidator(CapabilityResolver(registry))
    permissions = PermissionEngine()
    validate, evaluate = validator.validate, permissions.evaluate
    monkeypatch.setattr(validator, "validate", lambda p: validations.append(p) or validate(p))
    monkeypatch.setattr(permissions, "evaluate", lambda p: evaluations.append(p) or evaluate(p))
    prepare = executor_module.prepare_terminal_execution

    def prepare_once(value):
        target = prepare(value)
        prepared.append(target)
        return target

    monkeypatch.setattr(executor_module, "prepare_terminal_execution", prepare_once)
    executor = Executor(validator=validator, permission_engine=permissions, runtime_registry=runtimes)
    engine = Orchestrator(planner=SimpleNamespace(plan=lambda _: plan), executor=executor)
    return SimpleNamespace(request=request, plan=plan, engine=engine, executor=executor,
                           calls=calls, prepared=prepared, validations=validations,
                           evaluations=evaluations, result=result, permissions=permissions,
                           validator=validator, runtimes=runtimes, tmp_path=tmp_path)


def waiting(system):
    result = system.engine.run(system.request)
    assert result.status is ActionStatus.WAITING_FOR_PERMISSION
    target = system.engine.preview_pending_approval(system.request.request_id, step_number=1)
    assert type(target) is PendingApprovalTarget
    assert target is result.execution_report.pending_approval_target
    return target


def claim(system, subject):
    return system.engine.resume(system.request.request_id,
                                confirmed_steps=frozenset({1}), expected_subject=subject)


def subject_for(system, target):
    return ApprovalSubject(request_id=system.request.request_id, step_number=1,
                           capability="terminal", risk=RiskLevel.LOW,
                           arguments=terminal.terminal_approval_arguments(target))


def test_prepared_stored_previewed_claimed_and_recorded_same_object_once(system, monkeypatch):
    stored = waiting(system)
    assert stored.prepared_target is system.prepared[0]
    assert stored.effective_permission is PermissionMode.CONFIRM_BEFORE_EXECUTION
    assert system.calls == []
    assert len(system.prepared) == 1
    monkeypatch.setattr(executor_module, "prepare_terminal_execution",
                        lambda _: pytest.fail("resume re-prepared terminal"))
    monkeypatch.setattr(executor_module, "prepare_browser_approval",
                        lambda *a, **k: pytest.fail("terminal entered browser preparation"))
    monkeypatch.setattr(system.executor, "_validate_browser_continuation",
                        lambda *a, **k: pytest.fail("terminal entered browser continuation"))
    assert claim(system, stored.subject).completed
    assert system.calls == [stored.prepared_target]
    assert system.calls[0] is stored.prepared_target
    assert system.validations == system.evaluations == [system.plan, system.plan]
    assert claim(system, stored.subject).status is ActionStatus.FAILED
    assert len(system.calls) == 1


def test_local_test_adapter_routes_exact_stored_subject_and_claim(system):
    stored = waiting(system)
    projection = PendingPermissionUpdate(
        delegation_id="test", call_id="test-call", request_id=str(system.request.request_id),
        confirmation_steps=(1,), pending_confirmation_steps=(1,), message="Test confirmation",
    )
    default = build_default_local_approval_registry()
    assert default.names() == ("browser",)
    assert default.resolve(system.engine, projection) is None
    seen = []
    registry = LocalApprovalAdapterRegistry()

    def preview(projection, target):
        seen.append(target)
        return LocalApproval(call_id=projection.call_id, subject=target.subject)

    def current(engine, session, approval):
        target = engine.preview_pending_approval(system.request.request_id, step_number=1)
        return (target is stored and target.subject is approval.subject
                and session.call_id == approval.call_id)

    registry.register(LocalApprovalAdapter("terminal", preview, lambda _: "inert test prompt", current))
    approval = registry.resolve(system.engine, projection)
    assert seen == [stored] and seen[0] is stored
    assert approval.subject is stored.subject
    assert registry.is_current(system.engine, SimpleNamespace(call_id="test-call"), approval)
    assert claim(system, approval.subject).completed
    assert system.calls[0] is stored.prepared_target
    assert not registry.is_current(system.engine, SimpleNamespace(call_id="test-call"), approval)


def test_real_worker_confirmation_forwards_terminal_subject_and_exact_target(system):
    worker = ActionWorker(system.engine.run, capacity=2)
    worker.start()
    try:
        request_id = worker.submit({
            "goal": system.request.goal, "raw_input": system.request.raw_input,
            "request_id": str(system.request.request_id),
        })
        assert worker.result(request_id, timeout=3)["status"] == "waiting_for_permission"
        stored = system.engine.preview_pending_approval(system.request.request_id, step_number=1)
        assert system.calls == []
        wrong = subject_for(system, replace(stored.prepared_target, argv=("wrong",)))
        with pytest.raises(ValueError):
            worker.confirm(request_id, confirmed_steps=frozenset({1}), expected_subject=wrong)
        assert system.calls == []
        assert worker.confirm(request_id, confirmed_steps=frozenset({1}), expected_subject=stored.subject) == request_id
        assert worker.result(request_id, timeout=3)["status"] == "completed"
        assert system.calls[0] is stored.prepared_target is system.prepared[0]
        assert len(system.calls) == len(system.prepared) == 1
    finally:
        worker.shutdown(wait=True, timeout=3)


MATERIAL_CHANGES = {
    "contract_version": "terminal-execution-target/v2",
    "platform_contract": "another-platform/v1",
    "executable_requested": "other.exe",
    "executable_resolved": r"C:\other.exe",
    "executable_identity": "sha256:" + "a" * 64,
    "argv": ("second", "first", ""),
    "cwd": r"C:\other",
    "environment_policy_id": "another-policy/v1",
    "environment": (("PATH", "different"),),
    "environment_identity": "sha256:" + "b" * 64,
    "timeout_seconds": 31.0,
    "stdin_mode": "interactive",
    "shell": True,
}


@pytest.mark.parametrize("field", list(MATERIAL_CHANGES))
def test_projection_binds_every_material_field(system, field):
    target = waiting(system).prepared_target
    # Fixed fields cannot normally change. Simulate future/corrupt inputs to
    # prove projection binds them, independently of stricter v1 validation.
    changed = copy(target)
    object.__setattr__(changed, field, MATERIAL_CHANGES[field])
    assert subject_for(system, changed).fingerprint != subject_for(system, target).fingerprint


def test_projection_covers_all_target_fields_with_explicit_json_lists(system):
    target = waiting(system).prepared_target
    projection = terminal.terminal_approval_arguments(target)
    assert set(projection) == {field.name for field in fields(target)}
    assert type(projection["argv"]) is list
    assert type(projection["environment"]) is list
    assert all(type(pair) is list for pair in projection["environment"])
    assert json.loads(json.dumps(projection)) == projection
    assert projection["argv"] == ["first", "", "  white space  ", "script.py"]


@pytest.mark.parametrize("argv", [
    ("", "first", "  white space  ", "script.py"),
    ("first", "  white space  ", "script.py"),
    ("first", "", "white space", "script.py"),
    ("first", "", "  white space  ", "script.py", ""),
])
def test_argv_order_empty_values_and_whitespace_are_identity(system, argv):
    original = waiting(system).prepared_target
    assert subject_for(system, replace(original, argv=argv)).fingerprint != subject_for(system, original).fingerprint


def test_caller_containers_and_preview_copies_cannot_mutate_identity(system):
    original = waiting(system).prepared_target
    argv, environment = list(original.argv), [list(pair) for pair in original.environment]
    detached = replace(original, argv=argv, environment=environment)
    data = terminal.terminal_approval_arguments(detached)
    subject = ApprovalSubject(request_id=system.request.request_id, step_number=1,
                              capability="terminal", risk=RiskLevel.LOW, arguments=data)
    before = subject.fingerprint
    argv.append("mutated")
    environment.append(["PATH", "mutated"])
    data["argv"].append("mutated")
    subject.arguments["argv"].append("mutated")
    assert detached == original
    assert subject.fingerprint == before == subject_for(system, original).fingerprint


def test_environment_source_order_is_canonical_values_and_frozen_order_bind(system):
    target = waiting(system).prepared_target
    first = terminal._freeze_windows_environment({"PATH": "A", "TEMP": "B"})
    second = terminal._freeze_windows_environment({"TEMP": "B", "PATH": "A"})
    assert first == second == (("TEMP", "B"), ("PATH", "A"))
    original = replace(target, environment=first, environment_identity=terminal._environment_identity(first))
    changed_env = (("TEMP", "C"), ("PATH", "A"))
    changed = replace(target, environment=changed_env,
                      environment_identity=terminal._environment_identity(changed_env))
    assert subject_for(system, original).fingerprint != subject_for(system, changed).fingerprint
    reversed_env = tuple(reversed(first))
    with pytest.raises(terminal.TerminalContractError, match="ordered"):
        replace(target, environment=reversed_env, environment_identity=terminal._environment_identity(reversed_env))
    corrupt = copy(original)
    object.__setattr__(corrupt, "environment", reversed_env)
    assert subject_for(system, corrupt).fingerprint != subject_for(system, original).fingerprint


@pytest.mark.parametrize("field", ["executable", "argv", "cwd"])
def test_step_output_terminal_fields_fail_before_any_handler_or_preparation(system, field):
    prefix = ExecutionStep(1, "Search", "web_search")
    arguments = dict(system.plan.steps[0].arguments)
    arguments[field] = ExecutionArgument.step_output(1, "value")
    step = replace(system.plan.steps[0], step_number=2, arguments=arguments)
    report = system.executor.execute(replace(system.plan, steps=(prefix, step)))
    assert report.status is ActionStatus.FAILED
    assert "exactly one" in report.message
    assert system.calls == system.prepared == []
    # Capability-level check isolates literal-only validation from multi-step.
    with pytest.raises(terminal.TerminalContractError, match="literal-only"):
        terminal.terminal_request_from_plan(replace(system.plan, steps=(replace(step, step_number=1),)))


def test_multiple_terminal_steps_and_terminal_checkpoint_are_unsupported(system):
    report = system.executor.execute(replace(system.plan, steps=(system.plan.steps[0], replace(system.plan.steps[0], step_number=2))))
    assert report.status is ActionStatus.FAILED
    stored = waiting(system)
    checkpoint = ExecutionCheckpoint(1, {}, (), {})
    report = system.executor.execute(system.plan, confirmed_steps=frozenset({1}),
                                     checkpoint=checkpoint, pending_approval_target=stored)
    assert report.status is ActionStatus.FAILED
    assert system.calls == []


@pytest.mark.parametrize("extra", ["shell", "command", "stdin", "detached"])
def test_unsupported_terminal_modes_fail_closed(system, extra):
    arguments = dict(system.plan.steps[0].arguments)
    arguments[extra] = ExecutionArgument.literal("true")
    report = system.executor.execute(replace(system.plan, steps=(replace(system.plan.steps[0], arguments=arguments),)))
    assert report.status is ActionStatus.FAILED
    assert system.calls == system.prepared == []


@pytest.mark.parametrize("name", ["cmd", "cmd.exe", "powershell.exe", "pwsh", "bash.exe", "sh.exe", "wsl.exe", "git-bash.exe", "wrapper.bat", "wrapper.cmd"])
def test_shell_and_batch_requests_rejected_without_launch(system, name):
    with pytest.raises(terminal.TerminalContractError):
        terminal.TerminalExecutionRequest(executable=name, argv=(), cwd=str(system.tmp_path))
    assert system.calls == []


@pytest.mark.parametrize("resolved_name", ["cmd.exe", "powershell.exe", "pwsh.exe", "wrapper.bat", "wrapper.cmd"])
def test_final_resolved_alias_target_is_checked(system, monkeypatch, resolved_name):
    alias = system.tmp_path / "alias.exe"
    alias.write_bytes(b"inert alias")
    resolved = system.tmp_path / resolved_name
    resolved.write_bytes(b"inert resolved bytes")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return resolved if path == alias else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(terminal.TerminalContractError):
        terminal.prepare_terminal_execution(terminal.TerminalExecutionRequest(
            executable=str(alias), argv=(), cwd=str(system.tmp_path)))
    assert system.calls == []


@pytest.mark.parametrize("cwd", ["relative", "C:drive-relative", "/c/git-bash"])
def test_nonabsolute_cwd_rejected(system, cwd):
    arguments = dict(system.plan.steps[0].arguments)
    arguments["cwd"] = ExecutionArgument.literal(cwd)
    report = system.executor.execute(replace(system.plan, steps=(replace(system.plan.steps[0], arguments=arguments),)))
    assert report.status is ActionStatus.FAILED
    assert report.pending_approval_target is None
    assert system.calls == system.prepared == []


def test_missing_directory_preparation_failure_is_not_human_approval(system):
    arguments = dict(system.plan.steps[0].arguments)
    arguments["cwd"] = ExecutionArgument.literal(str(system.tmp_path / "missing"))
    report = system.executor.execute(replace(system.plan, steps=(replace(system.plan.steps[0], arguments=arguments),)))
    assert report.status is ActionStatus.FAILED
    assert report.pending_approval_target is None
    assert report.pending_confirmation_steps == ()
    assert system.calls == system.prepared == []


def test_unsupported_platform_is_rejected_without_global_os_mutation(system, monkeypatch):
    monkeypatch.setattr(terminal, "os", SimpleNamespace(name="posix"))
    report = system.executor.execute(system.plan)
    assert report.status is ActionStatus.FAILED
    assert "Windows only" in report.message
    assert report.pending_approval_target is None
    assert system.calls == []


@pytest.mark.parametrize("digest", ["sha256:" + "z" * 64, "sha256:" + "A" * 64, "sha256:" + "0" * 63, "md5:" + "0" * 64])
def test_malformed_executable_digest_rejected(system, digest):
    target = waiting(system).prepared_target
    with pytest.raises(terminal.TerminalContractError, match="SHA-256"):
        replace(target, executable_identity=digest)


@pytest.mark.parametrize("kind", ["missing", "wrong", "stale"])
def test_missing_wrong_stale_subject_has_zero_dispatch_and_retains_pending(system, kind):
    stored = waiting(system)
    subject = None if kind == "missing" else subject_for(system, replace(stored.prepared_target, argv=("different",)))
    if kind == "stale":
        new_target = replace(stored.prepared_target, argv=("replacement",))
        replacement = terminal.build_terminal_approval_target(
            new_target, request_id=system.request.request_id, step_number=1,
            risk=RiskLevel.LOW, effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION)
        pending = system.engine._pending[system.request.request_id]
        system.engine._pending[system.request.request_id] = replace(pending, pending_approval_target=replacement)
        subject = stored.subject
    with pytest.raises(ValueError):
        claim(system, subject)
    assert system.calls == []
    assert system.request.request_id in system.engine._pending


def test_duplicate_atomic_claim_dispatches_at_most_once(system):
    stored = waiting(system)
    entered, release = Event(), Event()

    def handler(inputs):
        system.calls.append(inputs["target"])
        entered.set()
        assert release.wait(3)
        return {"recorded": True}

    system.runtimes._handlers["terminal"] = handler
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(claim, system, stored.subject)
        try:
            assert entered.wait(3)
            second = pool.submit(claim, system, stored.subject)
            assert second.result(timeout=3).status is ActionStatus.FAILED
        finally:
            release.set()
        assert first.result(timeout=3).completed
    assert system.calls == [stored.prepared_target]
    assert system.calls[0] is stored.prepared_target


def test_policy_tightening_after_claim_blocks_with_no_dispatch(system, monkeypatch):
    stored = waiting(system)
    evaluate = system.permissions.evaluate

    def forbid(plan):
        report = evaluate(plan)
        return replace(report, decisions=tuple(replace(d, effective_permission=PermissionMode.FORBIDDEN) for d in report.decisions))

    monkeypatch.setattr(system.permissions, "evaluate", forbid)
    assert claim(system, stored.subject).status is ActionStatus.BLOCKED
    assert system.calls == []
    assert system.validations == system.evaluations == [system.plan, system.plan]


def test_validation_tightening_after_claim_blocks_with_no_dispatch(system, monkeypatch):
    stored = waiting(system)
    validate = system.validator.validate
    monkeypatch.setattr(system.validator, "validate", lambda p: replace(validate(p), status=PlanValidationStatus.BLOCKED))
    assert claim(system, stored.subject).status is ActionStatus.BLOCKED
    assert system.calls == []
    assert len(system.validations) == 2


@pytest.mark.parametrize("risk,permission", [
    (RiskLevel.CRITICAL, PermissionMode.AUTOMATIC),
    (RiskLevel.LOW, PermissionMode.FORBIDDEN),
])
def test_stricter_risk_or_policy_blocks_before_preparation(system, risk, permission):
    plan = replace(system.plan, steps=(replace(system.plan.steps[0], risk=risk, permission=permission),), overall_risk=risk)
    assert system.executor.execute(plan).status is ActionStatus.BLOCKED
    assert system.calls == system.prepared == []


@pytest.mark.parametrize("risk", [RiskLevel.MEDIUM, RiskLevel.HIGH])
def test_noncritical_risk_remains_bound_and_requires_confirmation(system, risk):
    plan = replace(system.plan, steps=(replace(system.plan.steps[0], risk=risk),), overall_risk=risk)
    report = system.executor.execute(plan)
    assert report.status is ActionStatus.WAITING_FOR_PERMISSION
    assert report.pending_approval_target.subject.risk is risk
    assert system.executor.execute(plan, confirmed_steps=frozenset({1}), pending_approval_target=report.pending_approval_target).completed


def test_confirmed_step_alone_cannot_bypass_stored_target(system):
    assert system.executor.execute(system.plan, confirmed_steps=frozenset({1})).status is ActionStatus.FAILED
    assert system.calls == system.prepared == []


@pytest.mark.parametrize("contradiction", ["request", "step", "capability", "risk", "subject", "target_type", "plan_executable", "plan_argv", "plan_cwd", "consistent_other_argv", "shell", "platform", "mutable_environment"])
def test_contradictory_continuations_fail_before_fake_dispatch(system, contradiction):
    stored = waiting(system)
    plan, forged = system.plan, stored
    metadata = dict(request_id=stored.subject.request_id, step_number=1, capability="terminal",
                    risk=RiskLevel.LOW, arguments=stored.subject.arguments)
    if contradiction in {"request", "step", "capability", "risk", "subject"}:
        if contradiction == "request":
            metadata["request_id"] = uuid4()
        elif contradiction == "step":
            metadata["step_number"] = 2
        elif contradiction == "capability":
            metadata["capability"] = "browser"
        elif contradiction == "risk":
            metadata["risk"] = RiskLevel.HIGH
        else:
            metadata["arguments"]["argv"] = ["wrong"]
        forged = replace(stored, subject=ApprovalSubject(**metadata))
    elif contradiction == "target_type":
        forged = replace(stored, prepared_target="https://example.com")
    elif contradiction.startswith("plan_"):
        key = contradiction[5:]
        arguments = dict(plan.steps[0].arguments)
        arguments[key] = (ExecutionArgument.literal_string_sequence(["wrong"]) if key == "argv"
                          else ExecutionArgument.literal(str(system.tmp_path / "other") if key == "cwd" else "other.exe"))
        plan = replace(plan, steps=(replace(plan.steps[0], arguments=arguments),))
    elif contradiction == "consistent_other_argv":
        changed = replace(stored.prepared_target, argv=("different",))
        forged = replace(stored, subject=subject_for(system, changed), prepared_target=changed)
    else:
        changed = copy(stored.prepared_target)
        if contradiction == "shell":
            object.__setattr__(changed, "shell", True)
        elif contradiction == "platform":
            object.__setattr__(changed, "platform_contract", "unsupported/v1")
        else:
            object.__setattr__(changed, "environment", tuple(list(pair) for pair in changed.environment))
        forged = replace(stored, subject=subject_for(system, changed), prepared_target=changed)
    report = system.executor.execute(plan, confirmed_steps=frozenset({1}), pending_approval_target=forged)
    assert report.status is ActionStatus.FAILED
    assert system.calls == []
    assert len(system.prepared) == 1


def test_missing_runtime_blocks_before_preparation(system):
    system.runtimes._handlers.pop("terminal")
    report = system.executor.execute(system.plan)
    assert report.status is ActionStatus.BLOCKED
    assert report.pending_approval_target is None
    assert system.calls == system.prepared == []


FAILURE_DATA = {
    "exit_code": 9, "stdout": "inert stdout", "stderr": "inert stderr",
    "timed_out": False, "stdout_truncated": True, "stderr_truncated": False,
    "execution_metadata": {"encoding": "utf-8", "runtime": "recording-only"},
}


def test_structured_terminal_failure_survives_execution_report_and_transport(system):
    stored = waiting(system)
    system.result.value = StepExecutionResult(1, "terminal", ActionStatus.FAILED,
                                             FAILURE_DATA, "synthetic failure")
    outcome = claim(system, stored.subject)
    assert outcome.status is ActionStatus.FAILED
    step = outcome.execution_report.step_results[0]
    assert step.status is ActionStatus.FAILED and step.data == FAILURE_DATA
    assert step.error == "synthetic failure"
    response = to_transport_response(outcome)
    assert response["status"] == response["step_results"][0]["status"] == "failed"
    assert response["step_results"][0]["data"] == FAILURE_DATA
    response["step_results"][0]["data"]["execution_metadata"]["runtime"] = "changed"
    assert step.data["execution_metadata"]["runtime"] == "recording-only"
    assert system.calls[0] is stored.prepared_target


def test_argv_resource_contents_are_outside_approved_identity(system):
    script = system.tmp_path / "script.py"
    script.write_text("original", encoding="utf-8")
    stored = waiting(system)
    before = stored.subject.fingerprint
    script.write_text("changed after approval", encoding="utf-8")
    assert stored.subject.fingerprint == before
    assert claim(system, stored.subject).completed
    assert system.calls[0].argv[-1] == "script.py"


def test_future_runtime_policy_documents_requirements_without_claiming_runtime():
    text = (Path(__file__).parents[1] / "capabilities" / "terminal_runtime_policy.md").read_text(encoding="utf-8")
    for required in (
        "no launch-time PATH search", "Revalidate executable_identity", "fail closed",
        "Never replace the approved", "Deterministic argv serialization", "shell=False",
        "final resolved filenames", "exact prepared cwd", "exact frozen environment",
        "stdin=DEVNULL", "30-second", "1 MiB", "UTF-8 with replacement",
        "stdout_truncated/stderr_truncated", "5 seconds", "Job Object",
        "no breakaway", "Restrict inherited handles", "Binary-byte continuity remains",
        "script.py contents remain mutable", "mutable repository/config state",
        "none\nof the launch/capture/containment mechanisms", "not a semantic sandbox",
    ):
        assert required in text


def test_changed_production_modules_have_no_launch_api_or_activation():
    root = Path(__file__).parents[1]
    banned = {"subprocess", "Popen", "system", "popen", "create_subprocess_exec",
              "create_subprocess_shell", "CreateProcess"}
    for name in ("capabilities/terminal.py", "core/executor.py", "core/runtime.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "subprocess" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "subprocess"
            elif isinstance(node, ast.Call):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else "")
                assert name not in banned
                assert not any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                               and k.value.value is True for k in node.keywords)
    assert build_default_local_approval_registry().names() == ("browser",)
    bootstrap = (root / "core" / "bootstrap.py").read_text(encoding="utf-8-sig")
    assert "terminal" not in bootstrap
