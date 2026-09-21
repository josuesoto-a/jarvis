"""Pure OpenAI Live/Responses adaptation for the Jarvis Action Engine.

This module performs no network I/O and imports no OpenAI SDK types.

It validates the single general action function that a future Responses
delegation may emit and adapts Jarvis TransportResponse objects into local
function-call result contracts.

Authority always remains local:

- delegation_id is correlation only.
- call_id is correlation only.
- request_id is correlation only.
- model arguments cannot grant permissions.
- WAITING_FOR_PERMISSION is not a completed function call.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, cast
from uuid import UUID, uuid4

from core.transport import (
    JSONValue,
    TransportRequest,
    TransportResponse,
    to_json_safe,
)


PERFORM_ACTION_NAME = "perform_action"

FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {
        "confirmed_steps",
        "permission",
        "permission_mode",
        "request_id",
    }
)

TerminalStatus = Literal[
    "completed",
    "failed",
    "blocked",
]


class CallConflictError(ValueError):
    """A previously observed function call was reused incompatibly."""


def perform_action_argument_schema() -> dict[str, JSONValue]:
    """Return a fresh JSON schema for perform_action arguments."""

    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The action Jarvis should accomplish.",
            },
            "raw_input": {
                "type": "string",
                "description": "The user's original relevant request.",
            },
            "context": {
                "type": "object",
                "description": "Optional non-authoritative context.",
                "additionalProperties": True,
            },
        },
        "required": [
            "goal",
            "raw_input",
        ],
        "additionalProperties": False,
    }


def _reject_nonstandard_constant(value: str) -> object:
    raise ValueError(
        f"arguments contains non-standard JSON constant: {value}"
    )


def _strict_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"arguments contains duplicate key: {key}"
            )

        result[key] = value

    return result


def _canonical_json(value: JSONValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_identifier(
    value: object,
    *,
    name: str,
) -> str:
    if type(value) is not str:
        raise TypeError(
            f"{name} must be a string"
        )

    if not value:
        raise ValueError(
            f"{name} must not be empty"
        )

    if value.strip() != value:
        raise ValueError(
            f"{name} must not contain leading or trailing whitespace"
        )

    return value


def _reject_authority_fields(
    value: JSONValue,
    *,
    path: str = "context",
) -> None:
    """Reject model-controlled authority-looking keys recursively."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_AUTHORITY_FIELDS:
                raise ValueError(
                    f"{path} cannot contain authority field: {key}"
                )

            _reject_authority_fields(
                child,
                path=f"{path}.{key}",
            )

    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_authority_fields(
                child,
                path=f"{path}[{index}]",
            )


@dataclass(frozen=True, slots=True)
class ParsedActionCall:
    """Validated perform_action call independent of OpenAI SDK objects."""

    delegation_id: str
    call_id: str
    arguments_json: str

    def arguments(self) -> dict[str, JSONValue]:
        """Return a new mutable snapshot of validated arguments."""

        value = json.loads(
            self.arguments_json
        )

        assert type(value) is dict

        return cast(
            dict[str, JSONValue],
            value,
        )


def parse_perform_action_call(
    *,
    delegation_id: str,
    call_id: str,
    name: str,
    arguments: str,
) -> ParsedActionCall:
    """Validate a Responses perform_action call without executing it."""

    delegation_id = _validate_identifier(
        delegation_id,
        name="delegation_id",
    )

    call_id = _validate_identifier(
        call_id,
        name="call_id",
    )

    if type(name) is not str:
        raise TypeError(
            "name must be a string"
        )

    if name != PERFORM_ACTION_NAME:
        raise ValueError(
            f"Unsupported function name: {name!r}"
        )

    if type(arguments) is not str:
        raise TypeError(
            "arguments must be a JSON string"
        )

    try:
        decoded = json.loads(
            arguments,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_nonstandard_constant,
        )

    except json.JSONDecodeError as error:
        raise ValueError(
            "arguments must contain valid JSON"
        ) from error

    safe = to_json_safe(
        decoded
    )

    if type(safe) is not dict:
        raise TypeError(
            "arguments must decode to a JSON object"
        )

    allowed_fields = {
        "goal",
        "raw_input",
        "context",
    }

    unknown_fields = (
        set(safe)
        - allowed_fields
    )

    if unknown_fields:
        names = ", ".join(
            sorted(unknown_fields)
        )

        raise ValueError(
            f"Unknown perform_action field(s): {names}"
        )

    if "goal" not in safe:
        raise ValueError(
            "Missing required field: goal"
        )

    if "raw_input" not in safe:
        raise ValueError(
            "Missing required field: raw_input"
        )

    goal = safe["goal"]

    if type(goal) is not str:
        raise TypeError(
            "goal must be a string"
        )

    goal = goal.strip()

    if not goal:
        raise ValueError(
            "goal must not be blank"
        )

    raw_input = safe["raw_input"]

    if type(raw_input) is not str:
        raise TypeError(
            "raw_input must be a string"
        )

    context = safe.get(
        "context",
        {},
    )

    if type(context) is not dict:
        raise TypeError(
            "context must be a JSON object"
        )

    _reject_authority_fields(
        context
    )

    normalized: dict[str, JSONValue] = {
        "goal": goal,
        "raw_input": raw_input,
        "context": context,
    }

    return ParsedActionCall(
        delegation_id=delegation_id,
        call_id=call_id,
        arguments_json=_canonical_json(
            normalized
        ),
    )


@dataclass(frozen=True, slots=True)
class ActionBinding:
    """Stable correlation between Live/Responses and Jarvis."""

    delegation_id: str
    call_id: str
    request_id: str
    arguments_json: str

    def to_transport_request(
        self,
    ) -> TransportRequest:
        """Build a fresh TransportRequest using the local request ID."""

        arguments = json.loads(
            self.arguments_json
        )

        assert type(arguments) is dict

        return cast(
            TransportRequest,
            {
                "goal": arguments["goal"],
                "raw_input": arguments["raw_input"],
                "context": arguments["context"],
                "request_id": self.request_id,
            },
        )


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    binding: ActionBinding
    is_new: bool


class ActionCallRegistry:
    """In-memory function-call correlation and idempotency.

    This class does not execute actions and is not durable storage.
    """

    def __init__(
        self,
    ) -> None:
        self._by_call_id: dict[
            str,
            ActionBinding,
        ] = {}

    def register(
        self,
        call: ParsedActionCall,
    ) -> RegistrationResult:
        existing = self._by_call_id.get(
            call.call_id
        )

        if existing is not None:
            if (
                existing.delegation_id
                != call.delegation_id
            ):
                raise CallConflictError(
                    "call_id is already bound to another delegation_id"
                )

            if (
                existing.arguments_json
                != call.arguments_json
            ):
                raise CallConflictError(
                    "call_id was repeated with different arguments"
                )

            return RegistrationResult(
                binding=existing,
                is_new=False,
            )

        binding = ActionBinding(
            delegation_id=call.delegation_id,
            call_id=call.call_id,
            request_id=str(
                uuid4()
            ),
            arguments_json=call.arguments_json,
        )

        self._by_call_id[
            call.call_id
        ] = binding

        return RegistrationResult(
            binding=binding,
            is_new=True,
        )

    def get(
        self,
        call_id: str,
    ) -> ActionBinding:
        return self._by_call_id[
            call_id
        ]


@dataclass(frozen=True, slots=True)
class TerminalFunctionOutput:
    """Final result for a future Responses function_call_output."""

    delegation_id: str
    call_id: str
    request_id: str
    status: TerminalStatus
    output_json: str

    def function_call_output_item(
        self,
    ) -> dict[str, JSONValue]:
        return {
            "type": "function_call_output",
            "call_id": self.call_id,
            "output": self.output_json,
        }


@dataclass(frozen=True, slots=True)
class PendingPermissionUpdate:
    """Non-terminal permission state for a function call."""

    delegation_id: str
    call_id: str
    request_id: str
    confirmation_steps: tuple[int, ...]
    pending_confirmation_steps: tuple[int, ...]
    message: str | None


ActionProjection = (
    TerminalFunctionOutput
    | PendingPermissionUpdate
)


def _project_step_results(
    response: TransportResponse,
) -> list[JSONValue]:
    projected: list[JSONValue] = []

    for step in response["step_results"]:
        projected.append(
            {
                "step_number": step[
                    "step_number"
                ],
                "capability": step[
                    "capability"
                ],
                "status": step[
                    "status"
                ],
                "data": step[
                    "data"
                ],
                "error": step[
                    "error"
                ],
            }
        )

    return projected


def project_transport_response(
    binding: ActionBinding,
    response: TransportResponse,
) -> ActionProjection:
    """Project a Jarvis result without performing network I/O."""

    if (
        response["request_id"]
        != binding.request_id
    ):
        raise ValueError(
            "TransportResponse request_id does not match action binding"
        )

    status = response[
        "status"
    ]

    if (
        status
        == "waiting_for_permission"
    ):
        return PendingPermissionUpdate(
            delegation_id=(
                binding.delegation_id
            ),
            call_id=(
                binding.call_id
            ),
            request_id=(
                binding.request_id
            ),
            confirmation_steps=tuple(
                response[
                    "confirmation_steps"
                ]
            ),
            pending_confirmation_steps=tuple(
                response[
                    "pending_confirmation_steps"
                ]
            ),
            message=response[
                "message"
            ],
        )

    if status not in {
        "completed",
        "failed",
        "blocked",
    }:
        raise ValueError(
            f"TransportResponse status is not deliverable: {status}"
        )

    if response[
        "pending_confirmation_steps"
    ]:
        raise ValueError(
            "Terminal response cannot contain pending confirmations"
        )

    payload: dict[
        str,
        JSONValue,
    ] = {
        "status": status,
    }

    if (
        response["message"]
        is not None
    ):
        payload["message"] = (
            response["message"]
        )

    if (
        response["error"]
        is not None
    ):
        payload["error"] = (
            response["error"]
        )

    step_results = (
        _project_step_results(
            response
        )
    )

    if step_results:
        payload[
            "step_results"
        ] = step_results

    safe_payload = to_json_safe(
        payload
    )

    if type(
        safe_payload
    ) is not dict:
        raise TypeError(
            "Terminal output projection must be a JSON object"
        )

    return TerminalFunctionOutput(
        delegation_id=(
            binding.delegation_id
        ),
        call_id=(
            binding.call_id
        ),
        request_id=(
            binding.request_id
        ),
        status=cast(
            TerminalStatus,
            status,
        ),
        output_json=_canonical_json(
            safe_payload
        ),
    )


def validate_local_request_id(
    request_id: str,
) -> UUID:
    """Validate a locally generated request UUID."""

    if type(request_id) is not str:
        raise TypeError(
            "request_id must be a string"
        )

    try:
        return UUID(
            request_id
        )

    except ValueError as error:
        raise ValueError(
            "request_id must be a valid UUID"
        ) from error
