"""Pure transport contracts and conversions; never plan or execute actions.

Usage: to_transport_response(app.orchestrator.run(to_action_request(payload))).
Only the caller invokes the orchestrator. Returned dicts/lists are independent,
mutable snapshots, with no references to the input's mutable containers.
"""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from math import isfinite
from typing import TYPE_CHECKING, Literal, TypedDict, Union, cast
from uuid import UUID

from core.contracts import ActionRequest

if TYPE_CHECKING:
    from core.orchestrator import OrchestrationResult


JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]
TransportStatus = Literal[
    "pending", "planning", "waiting_for_permission", "running",
    "completed", "failed", "blocked",
]


class _OptionalRequestFields(TypedDict, total=False):
    context: dict[str, JSONValue]
    request_id: str


class TransportRequest(_OptionalRequestFields):
    """Wire input. request_id is an optional UUID string, never a permission."""

    goal: str
    raw_input: str


class TransportStepResult(TypedDict):
    step_number: int
    capability: str
    status: TransportStatus
    data: dict[str, JSONValue]
    error: str | None


class TransportResponse(TypedDict):
    """Wire output; status is authoritative, including when no report exists.

    confirmation_steps lists steps requiring confirmation BY POLICY, even after
    completion or blocking. pending_confirmation_steps lists outstanding steps
    for this pending plan, and is empty for every non-waiting status.
    Read status == 'waiting_for_permission' to determine active waiting.

    message is the execution report's message (None without a report); error is
    the orchestration error. Step errors remain on their individual results.
    metadata contains validation diagnostics and permission decisions, without
    exposing the plan or internal report objects.
    """

    request_id: str
    status: TransportStatus
    message: str | None
    error: str | None
    step_results: list[TransportStepResult]
    confirmation_steps: list[int]
    pending_confirmation_steps: list[int]
    metadata: dict[str, JSONValue]


def to_json_safe(value: object) -> JSONValue:
    """Copy into strict JSON values, raising TypeError/ValueError on failure.

    UUID -> canonical string; Enum -> recursively converted value; dataclass
    instance -> all declared fields (not properties); tuple -> list. Mapping
    keys must be plain strings. Primitive subclasses other than Enum are not
    supported. Non-finite floats and cycles are rejected. Shared, non-cyclic
    values are copied separately. No repr/str fallback or type tags are used.
    Error paths locate unsupported data without rendering unknown objects.
    """
    active: set[int] = set()

    def convert(item: object, path: str) -> JSONValue:
        # Enum must precede primitives, including str/IntEnum subclasses.
        if not isinstance(item, Enum):
            if item is None or type(item) in (bool, int, str):
                return cast(JSONValue, item)
            if type(item) is float:
                if not isfinite(item):
                    raise ValueError(f"{path}: non-finite float is not JSON-safe")
                return item
            if isinstance(item, UUID):
                return str(item)

        identity = id(item)
        if identity in active:
            raise ValueError(f"{path}: cyclic value is not JSON-safe")
        active.add(identity)
        try:
            if isinstance(item, Enum):
                return convert(item.value, f"{path}.value")
            if is_dataclass(item) and not isinstance(item, type):
                return {
                    field.name: convert(getattr(item, field.name), f"{path}.{field.name}")
                    for field in fields(item)
                }
            if isinstance(item, Mapping):
                result: dict[str, JSONValue] = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise TypeError(f"{path}: mapping keys must be plain strings")
                    result[key] = convert(child, f"{path}[{key!r}]")
                return result
            if isinstance(item, (list, tuple)):
                return [convert(child, f"{path}[{index}]") for index, child in enumerate(item)]
            raise TypeError(f"{path}: unsupported JSON type {type(item).__name__}")
        finally:
            active.remove(identity)

    return convert(value, "$")


def to_action_request(payload: Mapping[str, object]) -> ActionRequest:
    """Validate wire input and construct a domain request without executing it.

    goal is trimmed and must be nonblank; raw_input is a required string kept
    verbatim (including empty text). context defaults to {} and is recursively
    normalized using to_json_safe. Unknown fields are rejected. A supplied UUID
    string gives deterministic identity; otherwise ActionRequest generates it.
    Reusing an ID does not deduplicate or authorize execution.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    for key in payload:
        if type(key) is not str:
            raise TypeError("payload keys must be plain strings")
        if key not in {"goal", "raw_input", "context", "request_id"}:
            raise ValueError(f"Unknown request field: {key}")
    for key in ("goal", "raw_input"):
        if key not in payload:
            raise ValueError(f"Missing required field: {key}")
        if type(payload[key]) is not str:
            raise TypeError(f"{key} must be a string")
    goal = cast(str, payload["goal"]).strip()
    if not goal:
        raise ValueError("goal must not be blank")
    context = payload.get("context", {})
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")
    normalized_context = cast(dict[str, JSONValue], to_json_safe(context))
    raw_input = cast(str, payload["raw_input"])
    if "request_id" in payload:
        request_id = payload["request_id"]
        if type(request_id) is not str:
            raise TypeError("request_id must be a UUID string")
        try:
            parsed_id = UUID(request_id)
        except ValueError as error:
            raise ValueError("request_id must be a valid UUID string") from error
        return ActionRequest(goal, raw_input, normalized_context, parsed_id)
    return ActionRequest(goal, raw_input, normalized_context)


def to_transport_response(result: "OrchestrationResult") -> TransportResponse:
    """Project a domain outcome into the transport schema, then copy/serialize.

    Preserve partial results in report order; never synthesize unexecuted steps.
    No permission evaluation, execution, confirmation, or resumption occurs.
    """
    report = result.execution_report
    metadata: dict[str, object] = {}
    confirmation_steps: list[int] = []
    if report is not None:
        if report.validation_report is not None:
            validation = report.validation_report
            metadata["validation"] = {
                "status": validation.status,
                "errors": validation.errors,
                "missing_capabilities": validation.missing_capabilities,
            }
        if report.permission_report is not None:
            permissions = report.permission_report
            metadata["permission_decisions"] = [
                {
                    "step_number": decision.step_number,
                    "capability": decision.capability,
                    "planner_permission": decision.planner_permission,
                    "effective_permission": decision.effective_permission,
                    "reasons": decision.reasons,
                }
                for decision in permissions.decisions
            ]
            confirmation_steps = [
                decision.step_number for decision in permissions.confirmation_steps
            ]

    response = {
        "request_id": result.request_id,
        "status": result.status,
        "message": report.message if report is not None else None,
        "error": result.error,
        "step_results": [
            {
                "step_number": step.step_number,
                "capability": step.capability,
                "status": step.status,
                "data": step.data,
                "error": step.error,
            }
            for step in report.step_results
        ] if report is not None else [],
        "confirmation_steps": confirmation_steps,
        "pending_confirmation_steps": (
            result.pending_confirmation_steps if result.waiting_for_permission else ()
        ),
        "metadata": metadata,
    }
    return cast(TransportResponse, to_json_safe(response))
