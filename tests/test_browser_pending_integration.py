"""D1F-B3: production browser identity must remain coupled to opener input."""

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import core.executor as executor_module
from capabilities.browser import (
    BrowserCapabilityError, create_browser_handler, prepare_browser_approval,
)
from core.action_worker import ActionWorker
from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import (
    ActionRequest, ActionStatus, ArgumentSource, CapabilitySpec,
    ExecutionArgument, ExecutionPlan, ExecutionStep, PermissionMode, RiskLevel,
)
from core.executor import Executor
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from integrations.local_browser_approval import (
    approval_is_current, format_browser_approval, preview_browser_approval_v2,
)
from integrations.openai_live import PendingPermissionUpdate


URL = "https://EXAMPLE.com:443/a%2Fb/?q=A%20B#Section"
OTHER = "https://example.com/other"


def system(*, dependency=False, url=URL, arguments=None, output=None,
           permission=PermissionMode.AUTOMATIC, runtime=True, capability=True):
    request = ActionRequest("Open exact target", "Open exact target")
    steps = []
    if dependency:
        steps.append(ExecutionStep(1, "Search", "web_search", arguments={
            "query": ExecutionArgument.literal("example"),
        }))
    number = 2 if dependency else 1
    if arguments is None:
        arguments = {"url": (ExecutionArgument.step_output(1, "best_result_url")
                             if dependency else ExecutionArgument(
                                 ArgumentSource.LITERAL, value=url))}
    steps.append(ExecutionStep(number, "Open", "browser", arguments=arguments,
                               permission=permission))
    plan = ExecutionPlan(request.request_id, tuple(steps), RiskLevel.LOW)
    registry, runtimes = CapabilityRegistry(), CapabilityRuntimeRegistry()
    if capability:
        registry.register(CapabilitySpec("browser", "Browser"))
    registry.register(CapabilitySpec("web_search", "Search"))
    opened, dispatched, searched, planned = [], [], [], []
    handler = create_browser_handler(opener=lambda value: opened.append(value) or True)

    def browser(arguments):
        dispatched.append(dict(arguments))
        return handler(arguments)

    def search(arguments):
        searched.append(dict(arguments))
        return {"best_result_url": url} if output is None else output

    if runtime:
        runtimes.register("browser", browser)
    runtimes.register("web_search", search)
    permissions = PermissionEngine()
    executor = Executor(validator=PlanValidator(CapabilityResolver(registry)),
                        permission_engine=permissions, runtime_registry=runtimes)

    def plan_once(incoming):
        planned.append(incoming)
        return plan

    engine = Orchestrator(planner=SimpleNamespace(plan=plan_once), executor=executor)
    return SimpleNamespace(request=request, plan=plan, number=number, engine=engine,
                           executor=executor, permissions=permissions, opened=opened,
                           dispatched=dispatched, searched=searched, planned=planned)


def projection(setup, *, call_id="call-current"):
    return PendingPermissionUpdate(
        delegation_id="delegation-current", call_id=call_id,
        request_id=str(setup.request.request_id), confirmation_steps=(setup.number,),
        pending_confirmation_steps=(setup.number,), message="Confirm locally",
    )


def session(setup, *, call_id="call-current", request_id=None, step=None):
    pending = projection(setup, call_id=call_id)
    if step is not None:
        pending = replace(pending, pending_confirmation_steps=(step,))
    return SimpleNamespace(snapshot=lambda: SimpleNamespace(action=SimpleNamespace(
        request_id=request_id or str(setup.request.request_id), call_id=call_id,
        pending_permission=pending,
    )))


def target(setup):
    return setup.engine.preview_pending_approval(setup.request.request_id,
                                               step_number=setup.number)


def claim(setup, subject):
    return setup.engine.resume(setup.request.request_id,
                              confirmed_steps=frozenset({setup.number}),
                              expected_subject=subject)


@pytest.mark.parametrize("dependency", [False, True])
def test_prepared_once_stored_previewed_claimed_and_dispatched_exactly(monkeypatch, dependency):
    prepared = []
    original = executor_module.prepare_browser_approval

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(executor_module, "prepare_browser_approval", prepare)
    setup = system(dependency=dependency)
    waiting = setup.engine.run(setup.request)
    stored = target(setup)
    assert waiting.status == ActionStatus.WAITING_FOR_PERMISSION
    assert stored is waiting.execution_report.pending_approval_target is prepared[0]
    assert type(stored.prepared_target) is str
    assert stored.prepared_target == URL
    assert stored.subject.arguments == {"url": URL}
    assert setup.opened == setup.dispatched == []
    if dependency:
        assert waiting.execution_report.checkpoint.resolved_arguments == {"url": URL}
        assert len(setup.searched) == 1
    else:
        assert waiting.execution_report.checkpoint is None
    approval = preview_browser_approval_v2(setup.engine, projection(setup))
    assert approval.subject is stored.subject
    assert URL in format_browser_approval(approval)
    assert approval_is_current(setup.engine, session(setup), approval)
    received = []
    execute = setup.executor.execute

    def continue_exact(plan, **kwargs):
        received.append(kwargs["pending_approval_target"])
        return execute(plan, **kwargs)

    monkeypatch.setattr(setup.executor, "execute", continue_exact)
    if not dependency:
        monkeypatch.setattr(setup.executor, "_resolve_arguments",
                            lambda *a, **k: pytest.fail("Literal resume selected a new target"))
    result = claim(setup, approval.subject)
    assert received == [stored]
    assert received[0] is stored
    assert result.completed
    assert len(prepared) == len(setup.planned) == 1
    assert len(setup.searched) == int(dependency)
    assert setup.dispatched == [{"url": stored.prepared_target}]
    assert setup.opened == [stored.prepared_target]
    assert target(setup) is None


BAD_URLS = [
    "file:///C:/secret.txt", "javascript:alert(1)", "https://user:pass@example.com/",
    "https://example.com:bad/", "https://example.com:99999/", "https:///missing",
    "https://[broken/", "https://example.com/a b", " https://example.com/",
    "https://example.com/\t", "https://example.com/\n", "https://example.com/\x1b",
    "https://example.com/\u200b", "https://example.com/\\bad",
    "https://example.com/" + "a" * 2048,
]


@pytest.mark.parametrize("dependency", [False, True])
@pytest.mark.parametrize("url", BAD_URLS)
def test_invalid_concrete_target_fails_before_waiting(dependency, url):
    setup = system(dependency=dependency, url=url)
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.FAILED
    assert result.execution_report.pending_approval_target is None
    assert target(setup) is None
    assert result.pending_confirmation_steps == ()
    assert setup.opened == setup.dispatched == []
    assert len(setup.searched) == int(dependency)


@pytest.mark.parametrize("arguments", [
    {}, {"url": ExecutionArgument.literal_string_sequence(["https://example.com/"])},
    {"url": ExecutionArgument.literal(URL), "other": ExecutionArgument.literal("extra")},
])
def test_missing_or_wrong_literal_browser_arguments_fail(arguments):
    setup = system(arguments=arguments)
    assert setup.engine.run(setup.request).status == ActionStatus.FAILED
    assert target(setup) is None
    assert setup.opened == []


@pytest.mark.parametrize("value", [None, 123, [], {}, True])
def test_non_string_preparation_rejected(value):
    with pytest.raises(BrowserCapabilityError):
        prepare_browser_approval({"url": value}, request_id=uuid4(), step_number=1,
                                 risk=RiskLevel.LOW,
                                 effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION)


@pytest.mark.parametrize("output", [{}, {"best_result_url": 123}, {"best_result_url": []}, []])
def test_bad_dependency_output_never_requests_approval(output):
    setup = system(dependency=True, output=output)
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.FAILED
    assert target(setup) is None
    assert setup.opened == []
    assert len(setup.searched) == 1


@pytest.mark.parametrize("dependency", [False, True])
def test_returned_mutation_cannot_change_private_execution(dependency):
    setup = system(dependency=dependency)
    waiting = setup.engine.run(setup.request)
    stored = target(setup)
    stored.subject.arguments["url"] = OTHER
    with pytest.raises(FrozenInstanceError):
        stored.prepared_target = OTHER
    if dependency:
        exposed = waiting.execution_report.checkpoint
        exposed.resolved_arguments["url"] = OTHER
        exposed.outputs[1]["best_result_url"] = OTHER
        exposed.step_results[0].data["best_result_url"] = OTHER
    assert target(setup) is stored
    assert stored.subject.arguments == {"url": URL}
    assert claim(setup, stored.subject).completed
    assert setup.opened == [URL]


def test_preview_does_not_read_plan_resolve_or_prepare(monkeypatch):
    setup = system(dependency=True)
    setup.engine.run(setup.request)
    stored = target(setup)

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must only read the stored identity")

    monkeypatch.setattr(executor_module, "prepare_browser_approval", forbidden)
    monkeypatch.setattr(setup.executor, "_resolve_arguments", forbidden)
    monkeypatch.setattr(setup.engine, "preview_single_browser", forbidden)
    monkeypatch.setattr(setup.engine, "preview_checkpoint_browser", forbidden)
    assert target(setup) is stored
    approval = preview_browser_approval_v2(setup.engine, projection(setup))
    assert approval.subject is stored.subject
    assert approval_is_current(setup.engine, session(setup), approval)


def replace_pending_target(setup, replacement):
    with setup.engine._lock:
        pending = setup.engine._pending[setup.request.request_id]
        changed = replace(pending, pending_approval_target=replacement)
        setup.engine._pending[setup.request.request_id] = changed
    return changed


def replacement(setup):
    return prepare_browser_approval({"url": OTHER}, request_id=setup.request.request_id,
                                   step_number=setup.number, risk=RiskLevel.LOW,
                                   effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION)


def test_changed_identity_rejected_by_freshness_and_atomic_claim():
    setup = system()
    setup.engine.run(setup.request)
    approval = preview_browser_approval_v2(setup.engine, projection(setup))
    assert approval_is_current(setup.engine, session(setup), approval)
    changed = replace_pending_target(setup, replacement(setup))
    assert not approval_is_current(setup.engine, session(setup), approval)
    with pytest.raises(ValueError, match="current pending target"):
        claim(setup, approval.subject)
    assert setup.engine._pending[setup.request.request_id] is changed
    assert setup.opened == []


def test_missing_expected_subject_fails_without_consuming():
    setup = system()
    setup.engine.run(setup.request)
    stored = target(setup)
    with pytest.raises(ValueError, match="expected_subject is required"):
        setup.engine.resume(setup.request.request_id, confirmed_steps=frozenset({1}))
    assert target(setup) is stored
    assert claim(setup, stored.subject).completed


@pytest.mark.parametrize("mismatch", ["request", "step", "capability", "risk", "url", "target_type"])
@pytest.mark.parametrize("dependency", [False, True])
def test_forged_envelope_cannot_reach_opener(mismatch, dependency):
    setup = system(dependency=dependency)
    waiting = setup.engine.run(setup.request)
    stored = target(setup)
    metadata = dict(request_id=setup.request.request_id, step_number=setup.number,
                    capability="browser", risk=RiskLevel.LOW, arguments={"url": URL})
    if mismatch == "request":
        metadata["request_id"] = uuid4()
    elif mismatch == "step":
        metadata["step_number"] = 99
    elif mismatch == "capability":
        metadata["capability"] = "sample"
    elif mismatch == "risk":
        metadata["risk"] = RiskLevel.HIGH
    elif mismatch == "url":
        metadata["arguments"] = {"url": OTHER}
    forged = replace(stored, subject=ApprovalSubject(**metadata),
                     prepared_target=[URL] if mismatch == "target_type" else URL)
    report = setup.executor.execute(
        setup.plan, confirmed_steps=frozenset({setup.number}),
        checkpoint=waiting.execution_report.checkpoint, pending_approval_target=forged,
    )
    assert report.status == ActionStatus.FAILED
    assert setup.opened == []
    assert len(setup.searched) == int(dependency)


def test_checkpoint_a_and_consistent_envelope_b_rejected_after_claim():
    setup = system(dependency=True)
    setup.engine.run(setup.request)
    changed = replacement(setup)
    replace_pending_target(setup, changed)
    result = claim(setup, changed.subject)
    assert result.status == ActionStatus.FAILED
    assert setup.opened == []
    assert len(setup.searched) == 1


def test_literal_a_and_consistent_envelope_b_rejected_after_claim():
    setup = system()
    setup.engine.run(setup.request)
    changed = replacement(setup)
    replace_pending_target(setup, changed)
    assert claim(setup, changed.subject).status == ActionStatus.FAILED
    assert setup.opened == []


@pytest.mark.parametrize("stale", ["call", "request", "step", "action"])
def test_matching_subject_cannot_rescue_stale_session(stale):
    setup = system()
    setup.engine.run(setup.request)
    approval = preview_browser_approval_v2(setup.engine, projection(setup))
    if stale == "call":
        current = session(setup, call_id="stale-call")
    elif stale == "request":
        current = session(setup, request_id=str(uuid4()))
    elif stale == "step":
        current = session(setup, step=2)
    else:
        current = SimpleNamespace(snapshot=lambda: SimpleNamespace(action=None))
    assert not approval_is_current(setup.engine, current, approval)
    assert target(setup).subject is approval.subject
    assert setup.opened == []


@pytest.mark.parametrize("dependency", [False, True])
def test_forbidden_blocks_before_preparation_or_search(monkeypatch, dependency):
    setup = system(dependency=dependency, permission=PermissionMode.FORBIDDEN)
    monkeypatch.setattr(executor_module, "prepare_browser_approval",
                        lambda *a, **k: pytest.fail("Forbidden action was prepared"))
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.BLOCKED
    assert result.execution_report.pending_approval_target is None
    assert setup.searched == setup.opened == []


def test_effective_automatic_creates_no_approval_envelope(monkeypatch):
    setup = system()
    evaluate = setup.permissions.evaluate
    monkeypatch.setattr(setup.permissions, "evaluate", lambda plan: replace(
        evaluate(plan), decisions=tuple(replace(d, effective_permission=PermissionMode.AUTOMATIC)
                                        for d in evaluate(plan).decisions)))
    result = setup.engine.run(setup.request)
    assert result.completed
    assert result.execution_report.pending_approval_target is None
    assert target(setup) is None
    assert setup.opened == [URL]


@pytest.mark.parametrize("missing", ["capability", "runtime"])
@pytest.mark.parametrize("dependency", [False, True])
def test_unavailable_browser_blocks_without_approval(missing, dependency):
    setup = system(dependency=dependency, capability=missing != "capability",
                   runtime=missing != "runtime")
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.BLOCKED
    assert result.execution_report.pending_approval_target is None
    assert setup.opened == setup.searched == []


def test_stricter_policy_after_claim_still_blocks(monkeypatch):
    setup = system(dependency=True)
    setup.engine.run(setup.request)
    stored = target(setup)
    evaluate = setup.permissions.evaluate
    monkeypatch.setattr(setup.permissions, "evaluate", lambda plan: replace(
        evaluate(plan), decisions=tuple(replace(d, effective_permission=PermissionMode.FORBIDDEN)
                                        for d in evaluate(plan).decisions)))
    assert claim(setup, stored.subject).status == ActionStatus.BLOCKED
    assert setup.opened == []
    assert len(setup.searched) == 1


def test_worker_rejects_stale_subject_before_queue():
    setup = system()
    worker = ActionWorker(setup.engine.run, capacity=2)
    worker.start()
    try:
        request_id = worker.submit({"goal": "Open", "raw_input": "Open",
                                    "request_id": str(setup.request.request_id)})
        assert worker.result(request_id, timeout=3)["status"] == "waiting_for_permission"
        approval = preview_browser_approval_v2(setup.engine, projection(setup))
        replace_pending_target(setup, replacement(setup))
        with pytest.raises(ValueError):
            worker.confirm(request_id, confirmed_steps=frozenset({1}),
                           expected_subject=approval.subject)
        assert setup.opened == []
    finally:
        worker.shutdown(wait=True, timeout=3)


@pytest.mark.parametrize("mutation", ["status", "request", "step", "capability", "risk", "permission"])
def test_orchestrator_rejects_misaligned_waiting_report(mutation):
    setup = system()
    report = setup.executor.execute(setup.plan)
    envelope = report.pending_approval_target
    if mutation == "status":
        report = replace(report, status=ActionStatus.COMPLETED)
    elif mutation == "permission":
        decisions = tuple(replace(d, effective_permission=PermissionMode.AUTOMATIC)
                          for d in report.permission_report.decisions)
        report = replace(report, permission_report=replace(report.permission_report, decisions=decisions))
    else:
        metadata = dict(request_id=setup.request.request_id, step_number=1,
                        capability="browser", risk=RiskLevel.LOW, arguments={"url": URL})
        metadata[mutation if mutation != "step" else "step_number"] = {
            "request": uuid4(), "step": 2, "capability": "sample", "risk": RiskLevel.HIGH,
        }[mutation]
        if mutation == "request":
            metadata["request_id"] = metadata.pop("request")
        report = replace(report, pending_approval_target=replace(envelope, subject=ApprovalSubject(**metadata)))
    setup.executor.execute = lambda *a, **k: report
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.FAILED
    assert target(setup) is None
    assert setup.opened == []


@pytest.mark.parametrize("current", [True, False])
def test_real_autorizar_loop_forwards_stored_subject_after_freshness(current):
    setup = system()
    setup.engine.run(setup.request)
    approval = preview_browser_approval_v2(setup.engine, projection(setup))
    tree = ast.parse((Path(__file__).parents[1] / "jarvis_v0_2_6.py").read_text(encoding="utf-8"))
    loop = next(node for node in ast.walk(tree) if isinstance(node, ast.While)
                and any(isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
                        and isinstance(item.value.func, ast.Name) and item.value.func.id == "input"
                        for item in node.body))
    calls, freshness = [], []

    def is_current(orchestrator, action_session, received):
        freshness.append(received)
        return current

    def confirm_pending(**kwargs):
        assert freshness == [approval]
        calls.append(kwargs)
        return str(setup.request.request_id)

    namespace = dict(approval=approval, app=SimpleNamespace(orchestrator=setup.engine),
                     action_session=SimpleNamespace(confirm_pending=confirm_pending),
                     LOCAL_APPROVAL_REGISTRY=SimpleNamespace(is_current=is_current),
                     stop_event=SimpleNamespace(is_set=lambda: False), input=lambda prompt: "AUTORIZAR",
                     console_message=lambda value: None)
    exec(compile(ast.Module(body=[loop], type_ignores=[]), "production_autorizar", "exec"), namespace)
    assert len(calls) == int(current)
    if current:
        assert calls[0]["expected_subject"] is target(setup).subject is approval.subject
        assert calls[0]["confirmed_steps"] == frozenset({1})


@pytest.mark.parametrize("permission", [PermissionMode.AUTOMATIC, PermissionMode.FORBIDDEN])
def test_preparer_cannot_create_envelope_without_confirmation(permission):
    with pytest.raises(BrowserCapabilityError, match="confirmation permission"):
        prepare_browser_approval({"url": URL}, request_id=uuid4(), step_number=1,
                                 risk=RiskLevel.LOW, effective_permission=permission)


def test_multiple_browser_confirmations_do_not_gain_local_approval():
    setup = system()
    second = replace(setup.plan.steps[0], step_number=2)
    plan = replace(setup.plan, steps=setup.plan.steps + (second,))
    setup.engine._planner = SimpleNamespace(plan=lambda request: plan)
    result = setup.engine.run(setup.request)
    assert result.status == ActionStatus.WAITING_FOR_PERMISSION
    assert result.pending_confirmation_steps == (1, 2)
    assert result.execution_report.pending_approval_target is None
    assert preview_browser_approval_v2(setup.engine, projection(setup)) is None
    assert setup.opened == []


def test_legacy_browser_pending_is_not_reconstructed_by_adapter():
    request_id = uuid4()
    pending = PendingPermissionUpdate(
        delegation_id="legacy", call_id="legacy-call", request_id=str(request_id),
        confirmation_steps=(1,), pending_confirmation_steps=(1,), message="Legacy",
    )
    engine = SimpleNamespace(
        preview_pending_approval=lambda *a, **k: None,
        preview_single_browser=lambda *a, **k: pytest.fail("Legacy reconstruction fallback"),
        preview_checkpoint_browser=lambda *a, **k: pytest.fail("Legacy reconstruction fallback"),
    )
    assert preview_browser_approval_v2(engine, pending) is None
