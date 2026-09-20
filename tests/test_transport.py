"""Offline transport contract tests, including real engine composition."""
import json
from collections import UserDict
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from uuid import UUID

import pytest

from core.contracts import (
    ActionRequest, ActionResult, ActionStatus, CapabilitySpec,
    ExecutionPlan, ExecutionStep, PermissionMode, RiskLevel,
)
from core.executor import ExecutionReport, Executor, StepExecutionResult
from core.orchestrator import OrchestrationResult, Orchestrator
from core.permissions import PermissionDecision, PermissionEngine, PermissionReport
from core.plan_validator import PlanValidationReport, PlanValidationStatus, PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from core.transport import to_action_request, to_json_safe, to_transport_response


REQUEST_ID = UUID("c2ec0b09-df60-44df-bc9d-e8eae901bb70")


def payload(**changes):
    return {"goal": "Process input", "raw_input": "Please process input.", **changes}


def outcome(status=ActionStatus.COMPLETED, *, steps=(), permissions=None):
    return OrchestrationResult(
        request_id=REQUEST_ID,
        status=status,
        execution_report=ExecutionReport(
            request_id=REQUEST_ID, status=status, step_results=steps,
            message="Engine message", permission_report=permissions,
        ),
    )


def permission_report():
    return PermissionReport(decisions=(
        PermissionDecision(
            step_number=1, capability="sample",
            planner_permission=PermissionMode.AUTOMATIC,
            effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            reasons=("Local policy requires confirmation.",),
        ),
    ))


def assert_plain_json(value):
    if isinstance(value, dict):
        assert type(value) is dict
        for key, child in value.items():
            assert type(key) is str
            assert_plain_json(child)
    elif isinstance(value, list):
        assert type(value) is list
        for child in value:
            assert_plain_json(child)
    else:
        assert value is None or type(value) in (bool, int, float, str)
    assert json.loads(json.dumps(value, allow_nan=False)) == value


def test_valid_request_preserves_text_and_normalizes_goal():
    request = to_action_request(payload(goal="  Process input\n", raw_input="  Original\n"))
    assert isinstance(request, ActionRequest)
    assert request.goal == "Process input"
    assert request.raw_input == "  Original\n"
    assert request.context == {}
    assert isinstance(request.request_id, UUID)


def test_request_id_default_is_generated_by_domain():
    first = to_action_request(payload())
    second = to_action_request(payload())
    assert first.request_id != second.request_id
    assert first.request_id.version == 4


def test_supplied_request_id_is_deterministic():
    incoming = payload(request_id=str(REQUEST_ID).upper())
    first = to_action_request(incoming)
    assert first == to_action_request(incoming)
    assert first.request_id == REQUEST_ID
    assert incoming["request_id"] == str(REQUEST_ID).upper()


@pytest.mark.parametrize("value", ["", " ", "\n\t"])
def test_blank_goal_rejected(value):
    with pytest.raises(ValueError, match="goal"):
        to_action_request(payload(goal=value))


@pytest.mark.parametrize("value", [None, 1, True, [], {}, ActionStatus.COMPLETED])
def test_non_text_goal_rejected(value):
    with pytest.raises(TypeError, match="goal"):
        to_action_request(payload(goal=value))


@pytest.mark.parametrize("name", ["goal", "raw_input"])
def test_missing_required_field(name):
    incoming = payload()
    del incoming[name]
    with pytest.raises(ValueError, match=name):
        to_action_request(incoming)


@pytest.mark.parametrize("value", [None, False, 7, [], {}])
def test_invalid_raw_input(value):
    with pytest.raises(TypeError, match="raw_input"):
        to_action_request(payload(raw_input=value))


def test_empty_raw_input_is_preserved():
    assert to_action_request(payload(raw_input="")).raw_input == ""


@pytest.mark.parametrize("value", [None, [], "text", 42])
def test_non_mapping_payload(value):
    with pytest.raises(TypeError, match="payload"):
        to_action_request(value)


@pytest.mark.parametrize("value", [None, [], "text", False])
def test_non_mapping_context(value):
    with pytest.raises(TypeError, match="context"):
        to_action_request(payload(context=value))


@pytest.mark.parametrize("value", ["", "not-a-uuid"])
def test_invalid_uuid_text(value):
    with pytest.raises(ValueError, match="request_id"):
        to_action_request(payload(request_id=value))


@pytest.mark.parametrize("value", [None, REQUEST_ID, 1, []])
def test_request_id_must_be_text(value):
    with pytest.raises(TypeError, match="request_id"):
        to_action_request(payload(request_id=value))


@pytest.mark.parametrize("name", ["confirmed_steps", "permission", "unexpected"])
def test_unknown_request_fields_rejected(name):
    with pytest.raises(ValueError, match=name):
        to_action_request(payload(**{name: True}))


def test_non_string_payload_key_rejected():
    with pytest.raises(TypeError, match="keys"):
        to_action_request({**payload(), 1: "bad"})


def test_nested_context_is_normalized_and_detached():
    nested = {"items": [{"id": REQUEST_ID, "status": ActionStatus.PENDING}]}
    incoming = payload(context=MappingProxyType({"nested": nested, "tuple": (1, None)}))
    request = to_action_request(incoming)
    assert request.context == {
        "nested": {"items": [{"id": str(REQUEST_ID), "status": "pending"}]},
        "tuple": [1, None],
    }
    nested["items"][0]["id"] = "changed"
    assert request.context["nested"]["items"][0]["id"] == str(REQUEST_ID)
    request.context["nested"]["items"].append("local")
    assert len(nested["items"]) == 1
    assert_plain_json(request.context)


def test_default_context_is_not_shared():
    first, second = to_action_request(payload()), to_action_request(payload())
    first.context["local"] = True
    assert second.context == {}


@pytest.mark.parametrize("context", [{"x": object()}, {"x": {1: "bad"}}])
def test_invalid_nested_context(context):
    with pytest.raises(TypeError):
        to_action_request(payload(context=context))


@pytest.mark.parametrize("status", list(ActionStatus))
def test_outer_status_and_identifier_are_preserved(status):
    response = to_transport_response(outcome(status))
    assert response["request_id"] == str(REQUEST_ID)
    assert response["status"] == status.value
    assert response["message"] == "Engine message"
    assert response["error"] is None
    assert response["step_results"] == []
    assert_plain_json(response)


@pytest.mark.parametrize("status", list(ActionStatus))
def test_no_execution_report(status):
    response = to_transport_response(OrchestrationResult(
        request_id=REQUEST_ID, status=status, error="Planning failed",
    ))
    assert response == {
        "request_id": str(REQUEST_ID), "status": status.value,
        "message": None, "error": "Planning failed",
        "step_results": [], "confirmation_steps": [], "pending_confirmation_steps": [], "metadata": {},
    }


def test_completed_multiple_steps_and_nested_domain_data():
    response = to_transport_response(outcome(steps=(
        StepExecutionResult(1, "sample", ActionStatus.COMPLETED, {"value": 3}),
        StepExecutionResult(2, "other", ActionStatus.COMPLETED, {
            "result": ActionResult(
                REQUEST_ID, ActionStatus.COMPLETED, "Done", {"values": (1, 2)},
            ),
        }),
    )))
    assert [step["step_number"] for step in response["step_results"]] == [1, 2]
    assert response["step_results"][0] == {
        "step_number": 1, "capability": "sample", "status": "completed",
        "data": {"value": 3}, "error": None,
    }
    assert response["step_results"][1]["data"]["result"] == {
        "request_id": str(REQUEST_ID), "status": "completed", "summary": "Done",
        "data": {"values": [1, 2]}, "error": None,
    }
    assert_plain_json(response)


def test_failed_with_report_preserves_partial_results_and_all_errors():
    result = outcome(ActionStatus.FAILED, steps=(
        StepExecutionResult(1, "sample", ActionStatus.COMPLETED, {"value": 3}),
        StepExecutionResult(2, "other", ActionStatus.FAILED, {"partial": True}, "Step failed"),
    ))
    result = OrchestrationResult(
        REQUEST_ID, ActionStatus.FAILED,
        execution_report=result.execution_report, error="Outer error",
    )
    response = to_transport_response(result)
    assert response["status"] == "failed"
    assert response["message"] == "Engine message"
    assert response["error"] == "Outer error"
    assert len(response["step_results"]) == 2
    assert response["step_results"][0]["data"] == {"value": 3}
    assert response["step_results"][1]["error"] == "Step failed"
    assert response["step_results"][1]["data"] == {"partial": True}
    assert response["step_results"][1]["status"] == "failed"


@pytest.mark.parametrize("status", [
    ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.BLOCKED,
    ActionStatus.WAITING_FOR_PERMISSION,
])
def test_confirmation_policy_does_not_determine_active_state(status):
    response = to_transport_response(outcome(status, permissions=permission_report()))
    assert response["status"] == status.value
    assert response["confirmation_steps"] == [1]
    assert response["metadata"]["permission_decisions"] == [{
        "step_number": 1, "capability": "sample",
        "planner_permission": "automatic",
        "effective_permission": "confirm_before_execution",
        "reasons": ["Local policy requires confirmation."],
    }]


def test_outer_status_is_authoritative_over_report_status():
    result = OrchestrationResult(
        REQUEST_ID, ActionStatus.BLOCKED,
        execution_report=outcome(
            ActionStatus.WAITING_FOR_PERMISSION, permissions=permission_report(),
        ).execution_report,
    )
    assert to_transport_response(result)["status"] == "blocked"


def test_response_is_detached_in_both_directions():
    data = {"nested": [{"value": 1}]}
    result = outcome(steps=(StepExecutionResult(1, "sample", ActionStatus.COMPLETED, data),))
    first, second = to_transport_response(result), to_transport_response(result)
    first["step_results"][0]["data"]["nested"][0]["value"] = 2
    assert data["nested"][0]["value"] == 1
    data["nested"].append("changed")
    assert second["step_results"][0]["data"] == {"nested": [{"value": 1}]}


class Number(IntEnum):
    ONE = 1


class Structured(Enum):
    VALUE = ("text", {"id": REQUEST_ID})


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), (True, True), (False, False), (0, 0), (2**80, 2**80),
    (-1.25, -1.25), ("texto ñ", "texto ñ"), (REQUEST_ID, str(REQUEST_ID)),
    (ActionStatus.COMPLETED, "completed"), (Number.ONE, 1),
    (Structured.VALUE, ["text", {"id": str(REQUEST_ID)}]),
    (UserDict({"a": (1, [None, {"b": True}])}), {"a": [1, [None, {"b": True}]]}),
])
def test_json_serialization(value, expected):
    result = to_json_safe(value)
    assert result == expected
    assert_plain_json(result)


@dataclass
class Record:
    value: object
    hidden: str = field(default="retained", repr=False)


def test_dataclass_fields_are_preserved_without_properties():
    assert to_json_safe(Record({"id": REQUEST_ID})) == {
        "value": {"id": str(REQUEST_ID)}, "hidden": "retained",
    }


class NoStringFallback:
    def __str__(self):
        raise AssertionError("Must never stringify unknown objects")


@pytest.mark.parametrize("value", [object(), NoStringFallback(), b"bytes", {1, 2}, Record])
def test_unsupported_values_fail_explicitly(value):
    with pytest.raises(TypeError, match="unsupported JSON type"):
        to_json_safe({"nested": [value]})


def test_unsupported_enum_value_fails():
    class Unsupported(Enum):
        VALUE = object()
    with pytest.raises(TypeError, match="unsupported JSON type"):
        to_json_safe(Unsupported.VALUE)


def test_dataclass_with_unsupported_field_fails_with_path():
    with pytest.raises(TypeError, match=r"\$\.value"):
        to_json_safe(Record(object()))


@pytest.mark.parametrize("key", [1, None, REQUEST_ID, (1,), ActionStatus.COMPLETED])
def test_non_plain_string_mapping_keys_rejected(key):
    with pytest.raises(TypeError, match="mapping keys"):
        to_json_safe({"nested": {key: "value"}})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_rejected(value):
    with pytest.raises(ValueError, match="non-finite"):
        to_json_safe({"nested": [value]})


@pytest.mark.parametrize("kind", ["list", "mapping", "dataclass", "tuple"])
def test_cycles_rejected(kind):
    if kind == "list":
        value = []
        value.append(value)
    elif kind == "mapping":
        value = {}
        value["self"] = value
    elif kind == "dataclass":
        value = Record(None)
        value.value = value
    else:
        child = []
        value = (child,)
        child.append(value)
    with pytest.raises(ValueError, match="cyclic"):
        to_json_safe(value)


def test_shared_values_are_not_cycles_and_are_copied():
    shared = {"items": []}
    result = to_json_safe([shared, shared])
    result[0]["items"].append(1)
    assert result[1] == {"items": []}
    assert shared == {"items": []}


@pytest.mark.parametrize("bad_data", [{"x": object()}, {"x": {1: "value"}}, {"x": float("nan")}])
def test_response_rejects_unserializable_step_data(bad_data):
    result = outcome(steps=(StepExecutionResult(1, "sample", ActionStatus.COMPLETED, bad_data),))
    with pytest.raises((TypeError, ValueError)):
        to_transport_response(result)


def make_engine(*, register=True, risk=RiskLevel.MEDIUM, fail_step=None):
    calls = []
    registry = CapabilityRegistry()
    if register:
        registry.register(CapabilitySpec("sample", "Offline sample"))
    runtimes = CapabilityRuntimeRegistry()

    def handler(arguments):
        calls.append(arguments)
        if len(calls) == fail_step:
            raise RuntimeError("Offline failure")
        return {"ordinal": len(calls)}

    runtimes.register("sample", handler)

    class Planner:
        def plan(self, request):
            return ExecutionPlan(
                request.request_id,
                tuple(ExecutionStep(n, "Process", "sample", risk=risk) for n in (1, 2)),
                risk,
            )

    engine = Orchestrator(
        planner=Planner(),
        executor=Executor(
            validator=PlanValidator(CapabilityResolver(registry)),
            permission_engine=PermissionEngine(), runtime_registry=runtimes,
        ),
    )
    return engine, calls


def test_transport_never_executes_or_evaluates_permissions(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Transport must not invoke engines or capabilities")

    monkeypatch.setattr(Orchestrator, "run", forbidden)
    monkeypatch.setattr(Executor, "execute", forbidden)
    monkeypatch.setattr(PermissionEngine, "evaluate", forbidden)
    monkeypatch.setattr(CapabilityRuntimeRegistry, "get", forbidden)
    request = to_action_request(payload())
    assert request.goal == "Process input"
    assert to_transport_response(outcome())["status"] == "completed"


def test_real_engine_confirmation_policy_survives_completion():
    engine, calls = make_engine()
    request = to_action_request(payload())
    assert calls == []
    initial = engine.run(request)
    assert initial.waiting_for_permission
    waiting = to_transport_response(engine.resume(request.request_id, confirmed_steps=frozenset({1})))
    assert waiting["status"] == "waiting_for_permission"
    # Policy includes 1, although only 2 remains unconfirmed. No text parsing.
    assert waiting["confirmation_steps"] == [1, 2]
    assert calls == []
    result = engine.resume(request.request_id, confirmed_steps=frozenset({2}))
    assert len(calls) == 2
    response = to_transport_response(result)
    assert response["status"] == "completed"
    assert response["confirmation_steps"] == [1, 2]
    assert len(calls) == 2
    assert_plain_json(response)


def test_real_engine_forbidden_steps_are_blocked():
    engine, calls = make_engine(risk=RiskLevel.CRITICAL)
    response = to_transport_response(engine.run(to_action_request(payload())))
    assert response["status"] == "blocked"
    assert response["confirmation_steps"] == []
    assert response["metadata"]["permission_decisions"][0]["effective_permission"] == "forbidden"
    assert calls == []


def test_real_engine_validation_diagnostics_preserved():
    engine, calls = make_engine(register=False)
    response = to_transport_response(engine.run(to_action_request(payload())))
    assert response["status"] == "blocked"
    assert response["metadata"]["validation"] == {
        "status": "blocked", "errors": [], "missing_capabilities": ["sample", "sample"],
    }
    assert "permission_decisions" not in response["metadata"]
    assert calls == []


def test_real_engine_partial_failure():
    engine, calls = make_engine(fail_step=2)
    request = to_action_request(payload())
    assert engine.run(request).waiting_for_permission
    result = engine.resume(request.request_id, confirmed_steps=frozenset({1, 2}))
    response = to_transport_response(result)
    assert response["status"] == "failed"
    assert response["message"] == "Execution failed at step 2."
    assert response["step_results"][0]["data"] == {"ordinal": 1}
    assert response["step_results"][1]["error"] == "RuntimeError: Offline failure"
    assert len(calls) == 2


def test_real_planning_failure_without_report():
    class BrokenPlanner:
        def plan(self, request):
            raise ValueError("Invalid plan")

    result = Orchestrator(planner=BrokenPlanner(), executor=None).run(to_action_request(payload()))
    response = to_transport_response(result)
    assert response["status"] == "failed"
    assert response["error"] == "Planning failed: ValueError: Invalid plan"
    assert response["message"] is None
    assert response["step_results"] == []


def test_invalid_plan_errors_are_preserved():
    plan = ExecutionPlan(REQUEST_ID, (), RiskLevel.LOW)
    validation = PlanValidator(CapabilityResolver(CapabilityRegistry())).validate(plan)
    result = OrchestrationResult(
        REQUEST_ID, ActionStatus.BLOCKED, plan=plan,
        execution_report=ExecutionReport(
            REQUEST_ID, ActionStatus.BLOCKED,
            message="Execution plan failed validation.",
            validation_report=validation,
        ),
    )
    response = to_transport_response(result)
    assert response["metadata"]["validation"] == {
        "status": "invalid",
        "errors": ["Execution plan must contain at least one step."],
        "missing_capabilities": [],
    }
    assert "plan" not in response
    assert_plain_json(response)


def test_serializer_supports_nested_domain_reports():
    plan = ExecutionPlan(
        REQUEST_ID, (ExecutionStep(1, "Process", "sample"),), RiskLevel.LOW,
    )
    result = OrchestrationResult(
        REQUEST_ID, ActionStatus.COMPLETED, plan=plan,
        execution_report=ExecutionReport(
            REQUEST_ID, ActionStatus.COMPLETED,
            permission_report=permission_report(),
            validation_report=PlanValidationReport(
                PlanValidationStatus.EXECUTABLE, plan, None,
            ),
        ),
    )
    serialized = to_json_safe(result)
    assert serialized["plan"]["request_id"] == str(REQUEST_ID)
    assert serialized["execution_report"]["validation_report"]["status"] == "executable"
    assert serialized["execution_report"]["permission_report"]["decisions"][0]["reasons"]
    assert_plain_json(serialized)


def test_cyclic_context_rejected_before_request_creation():
    context = {}
    context["self"] = context
    with pytest.raises(ValueError, match="cyclic"):
        to_action_request(payload(context=context))
