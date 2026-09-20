"""Security regressions for in-memory, plan-bound permission resumption."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.contracts import (
    ActionRequest, ActionStatus, CapabilitySpec, ExecutionArgument,
    ExecutionPlan, ExecutionStep, PermissionMode, RiskLevel,
)
from core.executor import ExecutionReport, Executor, StepExecutionResult
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidationReport, PlanValidationStatus, PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry


@pytest.fixture
def setup():
    request = ActionRequest("Original goal", "Original input")
    borrowed_arguments = {"value": ExecutionArgument.literal("original")}
    borrowed_steps = [
        ExecutionStep(1, "Search", "web_search", arguments=borrowed_arguments),
        ExecutionStep(
            2, "Use search output", "filesystem",
            arguments={"value": ExecutionArgument.step_output(1, "value")},
        ),
        ExecutionStep(
            3, "Run original operation", "terminal",
            arguments={"value": ExecutionArgument.literal("original operation")},
        ),
    ]
    original = ExecutionPlan(request.request_id, borrowed_steps, RiskLevel.MEDIUM)
    planner_calls, executions, handler_calls = [], [], []

    class ChangingPlanner:
        def plan(self, incoming):
            planner_calls.append(incoming)
            if len(planner_calls) == 1:
                return original
            # A replan would authorize different arguments under the same steps.
            return ExecutionPlan(incoming.request_id, (
                ExecutionStep(
                    1, "Replacement", "terminal",
                    arguments={"value": ExecutionArgument.literal("replacement")},
                ),
            ), RiskLevel.HIGH)

    registry = CapabilityRegistry()
    runtimes = CapabilityRuntimeRegistry()
    for capability in ("web_search", "filesystem", "terminal"):
        registry.register(CapabilitySpec(capability, "Offline test"))

        def handler(arguments, name=capability):
            handler_calls.append((name, arguments))
            return {"value": arguments["value"]}

        runtimes.register(capability, handler)

    permissions = PermissionEngine()
    validator = PlanValidator(CapabilityResolver(registry))

    class RecordingExecutor(Executor):
        def execute(self, plan, *, confirmed_steps=frozenset()):
            executions.append((plan, confirmed_steps))
            return super().execute(plan, confirmed_steps=confirmed_steps)

    executor = RecordingExecutor(
        validator=validator, permission_engine=permissions, runtime_registry=runtimes,
    )
    engine = Orchestrator(planner=ChangingPlanner(), executor=executor)
    return SimpleNamespace(
        request=request, plan=original, engine=engine, executor=executor,
        permissions=permissions, validator=validator, planner_calls=planner_calls,
        executions=executions, handler_calls=handler_calls,
        borrowed_arguments=borrowed_arguments, borrowed_steps=borrowed_steps,
    )


def confirm(setup, steps=frozenset({2, 3})):
    return setup.engine.resume(setup.request.request_id, confirmed_steps=steps)


def test_resume_executes_exact_presented_plan_without_replanning(setup):
    waiting = setup.engine.run(setup.request)
    assert waiting.waiting_for_permission
    assert waiting.plan is setup.plan
    assert waiting.execution_report.step_results == ()
    assert setup.handler_calls == []

    result = confirm(setup)
    assert result.completed
    assert result.plan is waiting.plan
    assert setup.planner_calls == [setup.request]
    assert len(setup.executions) == 2
    assert all(plan is waiting.plan for plan, _ in setup.executions)
    assert setup.executions[1][1] == frozenset({2, 3})
    assert setup.handler_calls == [
        ("web_search", {"value": "original"}),
        ("filesystem", {"value": "original"}),
        ("terminal", {"value": "original operation"}),
    ]


def test_partial_confirmations_accumulate_only_for_pending_plan(setup):
    waiting = setup.engine.run(setup.request)
    partial = confirm(setup, frozenset({2}))
    assert partial.waiting_for_permission
    assert partial.plan is waiting.plan
    assert setup.handler_calls == []
    with pytest.raises(ValueError, match="empty"):
        confirm(setup, frozenset())
    assert setup.handler_calls == []

    assert confirm(setup, frozenset({3})).completed
    assert setup.executions[-1][1] == frozenset({2, 3})
    assert len(setup.handler_calls) == 3
    assert setup.planner_calls == [setup.request]


@pytest.mark.parametrize("steps", [
    frozenset({0}), frozenset({-1}), frozenset({True}), frozenset({2.0}),
    frozenset({"2"}), frozenset({None}), frozenset({99}), frozenset({1}),
    frozenset({2, 99}),
])
def test_invalid_confirmations_do_not_execute_consume_or_grant(setup, steps):
    setup.engine.run(setup.request)
    with pytest.raises(ValueError, match="confirmed_steps"):
        confirm(setup, steps)
    assert len(setup.executions) == 1
    assert setup.handler_calls == []
    # Valid steps included in the rejected set were not silently granted.
    assert confirm(setup, frozenset({3})).waiting_for_permission
    assert setup.handler_calls == []
    assert confirm(setup, frozenset({2})).completed


@pytest.mark.parametrize("steps", [None, True, 2, [2, 3], {2, 3}, "2,3", {2: True}])
def test_confirmation_container_must_be_explicit_immutable_set(setup, steps):
    setup.engine.run(setup.request)
    with pytest.raises(TypeError, match="confirmed_steps"):
        confirm(setup, steps)
    assert len(setup.executions) == 1
    assert confirm(setup).completed


def test_unknown_id_and_resume_before_run_never_plan_or_execute(setup):
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.engine.resume(uuid4(), confirmed_steps=frozenset({2})).status == ActionStatus.FAILED
    assert setup.planner_calls == setup.executions == setup.handler_calls == []
    setup.engine.run(setup.request)
    assert confirm(setup).completed


def test_resume_requires_uuid_and_explicit_confirmation_argument(setup):
    setup.engine.run(setup.request)
    with pytest.raises(TypeError, match="request_id"):
        setup.engine.resume(str(setup.request.request_id), confirmed_steps=frozenset({2, 3}))
    with pytest.raises(TypeError, match="confirmed_steps"):
        setup.engine.resume(setup.request.request_id)
    assert len(setup.executions) == 1
    assert confirm(setup).completed


def test_run_cannot_replace_pending_plan_or_preapprove_it(setup):
    waiting = setup.engine.run(setup.request)
    replacement = replace(setup.request, goal="Different goal", context={"confirmed_steps": [2, 3]})
    with pytest.raises(TypeError, match="confirmed_steps"):
        setup.engine.run(replacement, confirmed_steps=frozenset({2, 3}))
    duplicate = setup.engine.run(replacement)
    assert duplicate.status == ActionStatus.FAILED
    assert "already used" in duplicate.error
    assert setup.planner_calls == [setup.request]
    assert len(setup.executions) == 1
    assert confirm(setup).plan is waiting.plan


def test_completed_id_cannot_be_replanned_or_confirmed_again(setup):
    setup.engine.run(setup.request)
    assert confirm(setup).completed
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.engine.run(setup.request).status == ActionStatus.FAILED
    assert len(setup.planner_calls) == 1
    assert len(setup.executions) == 2
    assert len(setup.handler_calls) == 3


def test_confirmation_for_one_request_does_not_authorize_another(setup):
    first = setup.engine.run(setup.request)
    other_request = ActionRequest("Other goal", "Other input")
    other = setup.engine.run(other_request)
    assert other.waiting_for_permission
    assert other.plan is not first.plan
    assert confirm(setup).completed
    with pytest.raises(ValueError, match="empty"):
        setup.engine.resume(other_request.request_id, confirmed_steps=frozenset())
    assert len(setup.handler_calls) == 3
    assert setup.engine.resume(other_request.request_id, confirmed_steps=frozenset({1})).completed
    assert setup.handler_calls[-1] == ("terminal", {"value": "replacement"})
    assert len(setup.planner_calls) == 2


def test_plan_containers_are_detached_and_immutable_while_waiting(setup):
    waiting = setup.engine.run(setup.request)
    setup.borrowed_arguments["value"] = ExecutionArgument.literal("changed target")
    setup.borrowed_steps.clear()
    setup.request.context["confirmed_steps"] = [2, 3]
    with pytest.raises(TypeError):
        waiting.plan.steps[0].arguments["value"] = ExecutionArgument.literal("changed target")
    with pytest.raises(FrozenInstanceError):
        waiting.plan.steps[0].arguments["value"].value = "changed target"
    with pytest.raises(FrozenInstanceError):
        waiting.plan.steps = ()
    assert confirm(setup).completed
    assert setup.handler_calls[0] == ("web_search", {"value": "original"})
    assert len(setup.handler_calls) == 3


def test_policy_is_rechecked_and_forbidden_plan_cannot_resume_again(setup, monkeypatch):
    setup.engine.run(setup.request)
    original_evaluate = setup.permissions.evaluate

    def forbid(plan):
        report = original_evaluate(plan)
        return replace(report, decisions=tuple(
            replace(decision, effective_permission=PermissionMode.FORBIDDEN)
            for decision in report.decisions
        ))

    monkeypatch.setattr(setup.permissions, "evaluate", forbid)
    result = confirm(setup)
    assert result.status == ActionStatus.BLOCKED
    assert result.plan is setup.plan
    assert setup.handler_calls == []
    monkeypatch.setattr(setup.permissions, "evaluate", original_evaluate)
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.engine.run(setup.request).status == ActionStatus.FAILED
    assert len(setup.executions) == 2
    assert len(setup.planner_calls) == 1


def test_validation_is_rechecked_on_resume(setup, monkeypatch):
    setup.engine.run(setup.request)
    monkeypatch.setattr(setup.validator, "validate", lambda plan: PlanValidationReport(
        PlanValidationStatus.BLOCKED, plan, None, errors=("Capability withdrawn",),
    ))
    assert confirm(setup).status == ActionStatus.BLOCKED
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.handler_calls == []
    assert len(setup.planner_calls) == 1


@pytest.mark.parametrize("mode", ["failed", "exception", "wrong_id", "unsafe_waiting"])
def test_failed_resume_is_terminal_even_after_partial_side_effects(setup, monkeypatch, mode):
    setup.engine.run(setup.request)
    calls = []

    def fail(plan, *, confirmed_steps):
        calls.append(plan)
        if mode == "exception":
            raise RuntimeError("Engine failure")
        return ExecutionReport(
            uuid4() if mode == "wrong_id" else plan.request_id,
            ActionStatus.WAITING_FOR_PERMISSION if mode == "unsafe_waiting" else ActionStatus.FAILED,
            step_results=(StepExecutionResult(1, "web_search", ActionStatus.COMPLETED),),
        )

    monkeypatch.setattr(setup.executor, "execute", fail)
    result = confirm(setup)
    assert result.status == ActionStatus.FAILED
    assert result.plan is setup.plan
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.engine.run(setup.request).status == ActionStatus.FAILED
    assert calls == [setup.plan]
    assert len(setup.planner_calls) == 1


@pytest.mark.parametrize("status", [ActionStatus.COMPLETED, ActionStatus.BLOCKED, ActionStatus.FAILED])
def test_initial_terminal_results_are_not_resumable(setup, monkeypatch, status):
    monkeypatch.setattr(setup.executor, "execute", lambda plan, **kwargs: ExecutionReport(
        plan.request_id, status,
    ))
    assert setup.engine.run(setup.request).status == status
    assert confirm(setup).status == ActionStatus.FAILED
    assert setup.engine.run(setup.request).status == ActionStatus.FAILED
    assert len(setup.planner_calls) == 1


def test_resume_cannot_borrow_another_orchestrators_pending_plan(setup):
    setup.engine.run(setup.request)
    other = Orchestrator(planner=None, executor=setup.executor)
    assert other.resume(setup.request.request_id, confirmed_steps=frozenset({2, 3})).status == ActionStatus.FAILED
    assert len(setup.executions) == 1
    assert confirm(setup).completed


def test_concurrent_resumes_consume_plan_once_without_holding_execution_lock(setup, monkeypatch):
    setup.engine.run(setup.request)
    entered, release = Event(), Event()
    execute = setup.executor.execute

    def gated(plan, *, confirmed_steps):
        entered.set()
        assert release.wait(5)
        return execute(plan, confirmed_steps=confirmed_steps)

    monkeypatch.setattr(setup.executor, "execute", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, setup)
        try:
            assert entered.wait(5)
            # Must return while the first executor is still blocked.
            second = pool.submit(confirm, setup)
            assert second.result(timeout=5).status == ActionStatus.FAILED
            duplicate = pool.submit(setup.engine.run, setup.request)
            assert duplicate.result(timeout=5).status == ActionStatus.FAILED
            assert setup.handler_calls == []
        finally:
            release.set()
        assert first.result(timeout=5).completed
    assert len(setup.planner_calls) == 1
    assert len(setup.executions) == 2
    assert len(setup.handler_calls) == 3


def test_duplicate_run_during_planning_cannot_create_replacement(setup, monkeypatch):
    entered, release = Event(), Event()
    plan = setup.engine._planner.plan

    def gated(request):
        entered.set()
        assert release.wait(5)
        return plan(request)

    monkeypatch.setattr(setup.engine._planner, "plan", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(setup.engine.run, setup.request)
        try:
            assert entered.wait(5)
            duplicate = pool.submit(setup.engine.run, setup.request)
            assert duplicate.result(timeout=5).status == ActionStatus.FAILED
            premature = pool.submit(confirm, setup)
            assert premature.result(timeout=5).status == ActionStatus.FAILED
        finally:
            release.set()
        assert first.result(timeout=5).waiting_for_permission
    assert confirm(setup).completed
    assert len(setup.planner_calls) == 1
