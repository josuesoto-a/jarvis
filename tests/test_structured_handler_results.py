"""Minimal existing StepExecutionResult handler protocol; no process execution."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.contracts import (
    ActionRequest, ActionStatus, CapabilitySpec, ExecutionArgument,
    ExecutionPlan, ExecutionStep, RiskLevel,
)
from core.executor import Executor, StepExecutionResult
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from core.transport import to_transport_response


DATA = {"exit_code": 7, "stdout": "synthetic", "stderr": "synthetic failure",
        "timed_out": True, "stdout_truncated": False, "stderr_truncated": True,
        "execution_metadata": {"source": "inert handler"}}


def build(capability, handler, *, checkpoint=False, browser_handler=None):
    registry, runtimes = CapabilityRegistry(), CapabilityRuntimeRegistry()
    registry.register(CapabilitySpec("browser", "Test browser"))
    registry.register(CapabilitySpec("web_search", "Test search"))
    runtimes.register(capability, handler)
    if checkpoint:
        runtimes.register("browser", browser_handler or (lambda _: {"opened": True}))
    executor = Executor(validator=PlanValidator(CapabilityResolver(registry)),
                        permission_engine=PermissionEngine(), runtime_registry=runtimes)
    request = ActionRequest("Synthetic result", "Synthetic result")
    step = ExecutionStep(1, "Run synthetic handler", capability,
                         arguments={"url": ExecutionArgument.literal("https://example.com")}
                         if capability == "browser" else {})
    steps = (step,)
    if checkpoint:
        steps += (ExecutionStep(2, "Open output", "browser", arguments={
            "url": ExecutionArgument.step_output(1, "best_result_url")}),)
    plan = ExecutionPlan(request.request_id, steps, RiskLevel.LOW)
    engine = Orchestrator(planner=SimpleNamespace(plan=lambda _: plan), executor=executor)
    return request, plan, executor, engine


@pytest.mark.parametrize("capability", ["browser", "web_search"])
def test_explicit_failed_result_retains_data_and_transport(capability):
    handler = lambda _: StepExecutionResult(1, capability, ActionStatus.FAILED, DATA, "synthetic")
    request, plan, executor, _ = build(capability, handler)
    report = executor.execute(plan, confirmed_steps=frozenset({1}))
    assert report.status is ActionStatus.FAILED
    assert len(report.step_results) == 1
    step = report.step_results[0]
    assert step.status is ActionStatus.FAILED and step.data == DATA and step.error == "synthetic"
    # Project through the existing orchestration and transport path too.
    engine = Orchestrator(planner=SimpleNamespace(plan=lambda _: plan), executor=executor)
    if capability == "browser":
        pending = engine.run(request).execution_report.pending_approval_target
        outcome = engine.resume(request.request_id, confirmed_steps=frozenset({1}), expected_subject=pending.subject)
    else:
        outcome = engine.run(request)
    wire = to_transport_response(outcome)
    assert wire["status"] == "failed"
    assert wire["step_results"][0]["status"] == "failed"
    assert wire["step_results"][0]["data"] == DATA


@pytest.mark.parametrize("capability", ["browser", "web_search"])
@pytest.mark.parametrize("explicit", [False, True])
def test_normal_mapping_and_explicit_success_are_completed(capability, explicit):
    data = {"ok": True, "status": "failed"}  # Mapping contents cannot alter status.
    handler = lambda _: StepExecutionResult(1, capability, ActionStatus.COMPLETED, data) if explicit else data
    _, plan, executor, _ = build(capability, handler)
    report = executor.execute(plan, confirmed_steps=frozenset({1}))
    assert report.completed
    assert report.step_results[0].status is ActionStatus.COMPLETED
    assert report.step_results[0].data == data


@pytest.mark.parametrize("invalid", ["step", "capability", "status", "status_text", "error", "data", "key"])
def test_inconsistent_explicit_result_fails_closed(invalid):
    result = StepExecutionResult(1, "web_search", ActionStatus.FAILED, DATA, "synthetic")
    changes = {
        "step": {"step_number": 2}, "capability": {"capability": "browser"},
        "status": {"status": ActionStatus.WAITING_FOR_PERMISSION},
        "status_text": {"status": "failed"}, "error": {"error": 9},
        "data": {"data": []}, "key": {"data": {7: "bad"}},
    }
    _, plan, executor, _ = build("web_search", lambda _: replace(result, **changes[invalid]))
    report = executor.execute(plan)
    assert report.status is ActionStatus.FAILED
    assert report.step_results[0].status is ActionStatus.FAILED
    assert report.step_results[0].data == {}
    assert "InvalidCapabilityResultError" in report.step_results[0].error


def test_failed_automatic_checkpoint_prefix_retains_data_and_stops_before_browser():
    calls = []
    handler = lambda _: StepExecutionResult(1, "web_search", ActionStatus.FAILED, DATA, "synthetic")
    request, plan, executor, _ = build("web_search", handler, checkpoint=True,
                                      browser_handler=lambda _: calls.append("browser") or {})
    report = executor.execute(plan)
    assert report.status is ActionStatus.FAILED
    assert report.step_results[0].data == DATA
    assert report.pending_approval_target is report.checkpoint is None
    assert calls == []


def test_failed_claimed_browser_checkpoint_keeps_successful_prefix_and_failure_data():
    searches = []

    def search(_):
        searches.append(1)
        return {"best_result_url": "https://example.com"}

    handler = lambda _: StepExecutionResult(2, "browser", ActionStatus.FAILED, DATA, "synthetic")
    request, plan, executor, engine = build("web_search", search, checkpoint=True, browser_handler=handler)
    waiting = engine.run(request)
    assert waiting.status is ActionStatus.WAITING_FOR_PERMISSION
    subject = waiting.execution_report.pending_approval_target.subject
    outcome = engine.resume(request.request_id, confirmed_steps=frozenset({2}), expected_subject=subject)
    assert outcome.status is ActionStatus.FAILED
    assert [s.status for s in outcome.execution_report.step_results] == [ActionStatus.COMPLETED, ActionStatus.FAILED]
    assert outcome.execution_report.step_results[1].data == DATA
    assert to_transport_response(outcome)["step_results"][1]["data"] == DATA
    assert searches == [1]
