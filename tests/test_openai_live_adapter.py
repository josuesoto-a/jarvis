"""Offline tests for the pure OpenAI Live/Responses action adapter."""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from core.transport import TransportResponse
from integrations.openai_live import (
    ActionCallRegistry,
    CallConflictError,
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    parse_perform_action_call,
    perform_action_argument_schema,
    project_transport_response,
)


def arguments(**overrides):
    payload = {
        "goal": "Open the relevant page",
        "raw_input": "Find it and open it",
        "context": {
            "language": "en",
        },
    }

    payload.update(
        overrides
    )

    return json.dumps(
        payload
    )


def parsed(
    *,
    delegation_id="delegation-1",
    call_id="call-1",
    name="perform_action",
    args=None,
):
    return parse_perform_action_call(
        delegation_id=delegation_id,
        call_id=call_id,
        name=name,
        arguments=(
            args
            if args is not None
            else arguments()
        ),
    )


def registered():
    registry = ActionCallRegistry()

    registration = registry.register(
        parsed()
    )

    return (
        registry,
        registration.binding,
    )


def response(
    request_id,
    *,
    status="completed",
    message=None,
    error=None,
    step_results=None,
    confirmation_steps=None,
    pending_confirmation_steps=None,
    metadata=None,
):
    return cast(
        TransportResponse,
        {
            "request_id": request_id,
            "status": status,
            "message": message,
            "error": error,
            "step_results": (
                step_results
                or []
            ),
            "confirmation_steps": (
                confirmation_steps
                or []
            ),
            "pending_confirmation_steps": (
                pending_confirmation_steps
                or []
            ),
            "metadata": (
                metadata
                or {}
            ),
        },
    )


def test_schema_has_only_action_fields():
    schema = (
        perform_action_argument_schema()
    )

    assert set(
        schema["properties"]
    ) == {
        "goal",
        "raw_input",
        "context",
    }

    assert (
        schema[
            "additionalProperties"
        ]
        is False
    )


def test_schema_returns_fresh_copy():
    first = (
        perform_action_argument_schema()
    )

    second = (
        perform_action_argument_schema()
    )

    first["required"].append(
        "fake"
    )

    assert second[
        "required"
    ] == [
        "goal",
        "raw_input",
    ]


def test_valid_call_parses():
    call = parsed()

    values = call.arguments()

    assert (
        values["goal"]
        == "Open the relevant page"
    )

    assert (
        values["raw_input"]
        == "Find it and open it"
    )

    assert values[
        "context"
    ] == {
        "language": "en",
    }


def test_goal_is_trimmed():
    call = parsed(
        args=arguments(
            goal="  Open page  "
        )
    )

    assert (
        call.arguments()[
            "goal"
        ]
        == "Open page"
    )


def test_raw_input_is_preserved():
    call = parsed(
        args=arguments(
            raw_input="  exact text  "
        )
    )

    assert (
        call.arguments()[
            "raw_input"
        ]
        == "  exact text  "
    )


def test_context_is_optional():
    call = parsed(
        args=json.dumps(
            {
                "goal": "Do something",
                "raw_input": "Do something",
            }
        )
    )

    assert (
        call.arguments()[
            "context"
        ]
        == {}
    )


def test_wrong_function_name_rejected():
    with pytest.raises(
        ValueError,
        match="Unsupported function",
    ):
        parsed(
            name="weather_tool"
        )


def test_invalid_json_rejected():
    with pytest.raises(
        ValueError,
        match="valid JSON",
    ):
        parsed(
            args="{bad json"
        )


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '"hello"',
        "42",
        "null",
    ],
)
def test_arguments_must_be_object(
    value,
):
    with pytest.raises(
        TypeError,
        match="JSON object",
    ):
        parsed(
            args=value
        )


def test_goal_required():
    with pytest.raises(
        ValueError,
        match="goal",
    ):
        parsed(
            args=json.dumps(
                {
                    "raw_input": "hello",
                }
            )
        )


def test_raw_input_required():
    with pytest.raises(
        ValueError,
        match="raw_input",
    ):
        parsed(
            args=json.dumps(
                {
                    "goal": "hello",
                }
            )
        )


@pytest.mark.parametrize(
    "goal",
    [
        "",
        " ",
        "\n\t",
    ],
)
def test_goal_cannot_be_blank(
    goal,
):
    with pytest.raises(
        ValueError,
        match="blank",
    ):
        parsed(
            args=arguments(
                goal=goal
            )
        )


@pytest.mark.parametrize(
    "context",
    [
        [],
        "hello",
        123,
        None,
    ],
)
def test_context_must_be_object(
    context,
):
    with pytest.raises(
        TypeError,
        match="context",
    ):
        parsed(
            args=arguments(
                context=context
            )
        )


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "confirmed_steps",
            [1],
        ),
        (
            "permission",
            "automatic",
        ),
        (
            "permission_mode",
            "automatic",
        ),
        (
            "request_id",
            "00000000-0000-0000-0000-000000000000",
        ),
        (
            "unexpected",
            True,
        ),
    ],
)
def test_unknown_and_authority_fields_rejected(
    field,
    value,
):
    with pytest.raises(
        ValueError,
        match="Unknown",
    ):
        parsed(
            args=arguments(
                **{
                    field: value
                }
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "confirmed_steps",
        "permission",
        "permission_mode",
        "request_id",
    ],
)
def test_authority_fields_cannot_hide_in_context(
    field,
):
    with pytest.raises(
        ValueError,
        match="authority field",
    ):
        parsed(
            args=arguments(
                context={
                    field: "model-controlled",
                }
            )
        )


def test_authority_fields_cannot_hide_deeply():
    with pytest.raises(
        ValueError,
        match="authority field",
    ):
        parsed(
            args=arguments(
                context={
                    "nested": {
                        "items": [
                            {
                                "confirmed_steps": [
                                    1
                                ]
                            }
                        ]
                    }
                }
            )
        )


@pytest.mark.parametrize(
    "delegation_id,call_id",
    [
        (
            "",
            "call-1",
        ),
        (
            " ",
            "call-1",
        ),
        (
            "delegation-1",
            "",
        ),
        (
            "delegation-1",
            " ",
        ),
    ],
)
def test_empty_identifiers_rejected(
    delegation_id,
    call_id,
):
    with pytest.raises(
        ValueError
    ):
        parsed(
            delegation_id=delegation_id,
            call_id=call_id,
        )


def test_duplicate_json_keys_rejected():
    raw = (
        '{"goal":"one",'
        '"goal":"two",'
        '"raw_input":"hello",'
        '"context":{}}'
    )

    with pytest.raises(
        ValueError,
        match="duplicate key",
    ):
        parsed(
            args=raw
        )


@pytest.mark.parametrize(
    "constant",
    [
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_non_standard_json_constants_rejected(
    constant,
):
    raw = (
        '{"goal":"hello",'
        '"raw_input":"hello",'
        f'"context":{{"value":{constant}}}}}'
    )

    with pytest.raises(
        ValueError
    ):
        parsed(
            args=raw
        )


def test_non_finite_float_rejected():
    raw = (
        '{"goal":"hello",'
        '"raw_input":"hello",'
        '"context":{"value":1e999}}'
    )

    with pytest.raises(
        ValueError,
        match="non-finite",
    ):
        parsed(
            args=raw
        )


def test_registry_generates_local_uuid():
    registry = (
        ActionCallRegistry()
    )

    registration = (
        registry.register(
            parsed()
        )
    )

    assert (
        registration.is_new
        is True
    )

    UUID(
        registration.binding.request_id
    )


def test_transport_request_uses_local_request_id():
    _, binding = registered()

    request = (
        binding.to_transport_request()
    )

    assert (
        request["request_id"]
        == binding.request_id
    )

    assert (
        request["goal"]
        == "Open the relevant page"
    )

    assert (
        "confirmed_steps"
        not in request
    )

    assert (
        "permission"
        not in request
    )


def test_transport_requests_do_not_share_context():
    _, binding = registered()

    first = (
        binding.to_transport_request()
    )

    second = (
        binding.to_transport_request()
    )

    first[
        "context"
    ][
        "language"
    ] = "changed"

    assert second[
        "context"
    ] == {
        "language": "en",
    }


def test_identical_duplicate_is_idempotent():
    registry = (
        ActionCallRegistry()
    )

    first = registry.register(
        parsed()
    )

    second = registry.register(
        parsed()
    )

    assert (
        first.is_new
        is True
    )

    assert (
        second.is_new
        is False
    )

    assert (
        second.binding
        == first.binding
    )


def test_semantically_identical_json_is_idempotent():
    registry = (
        ActionCallRegistry()
    )

    first = registry.register(
        parsed(
            args=(
                '{"goal":"x",'
                '"raw_input":"y",'
                '"context":{"b":2,"a":1}}'
            )
        )
    )

    second = registry.register(
        parsed(
            args=(
                '{"context":{"a":1,"b":2},'
                '"raw_input":"y",'
                '"goal":"x"}'
            )
        )
    )

    assert (
        second.is_new
        is False
    )

    assert (
        second.binding.request_id
        == first.binding.request_id
    )


def test_same_call_with_different_arguments_conflicts():
    registry = (
        ActionCallRegistry()
    )

    registry.register(
        parsed(
            args=arguments(
                goal="first"
            )
        )
    )

    with pytest.raises(
        CallConflictError,
        match="different arguments",
    ):
        registry.register(
            parsed(
                args=arguments(
                    goal="second"
                )
            )
        )


def test_same_call_with_other_delegation_conflicts():
    registry = (
        ActionCallRegistry()
    )

    registry.register(
        parsed(
            delegation_id="delegation-a"
        )
    )

    with pytest.raises(
        CallConflictError,
        match="another delegation",
    ):
        registry.register(
            parsed(
                delegation_id="delegation-b"
            )
        )


@pytest.mark.parametrize(
    "status",
    [
        "completed",
        "failed",
        "blocked",
    ],
)
def test_terminal_states_create_terminal_output(
    status,
):
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status=status,
                message="message",
                error=(
                    "error"
                    if status == "failed"
                    else None
                ),
            ),
        )
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    assert (
        projection.status
        == status
    )

    body = json.loads(
        projection.output_json
    )

    assert (
        body["status"]
        == status
    )


def test_function_call_output_item():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id
            ),
        )
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    assert (
        projection.function_call_output_item()
        == {
            "type": (
                "function_call_output"
            ),
            "call_id": (
                binding.call_id
            ),
            "output": (
                projection.output_json
            ),
        }
    )


def test_terminal_output_is_compact_json():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                message="done",
            ),
        )
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    assert (
        ": "
        not in projection.output_json
    )

    assert (
        ", "
        not in projection.output_json
    )

    json.loads(
        projection.output_json
    )


def test_terminal_output_preserves_step_results():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status="completed",
                step_results=[
                    {
                        "step_number": 1,
                        "capability": (
                            "web_search"
                        ),
                        "status": (
                            "completed"
                        ),
                        "data": {
                            "answer": "42",
                        },
                        "error": None,
                    }
                ],
            ),
        )
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    body = json.loads(
        projection.output_json
    )

    assert (
        body["step_results"][0][
            "data"
        ][
            "answer"
        ]
        == "42"
    )


def test_internal_metadata_not_leaked():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                metadata={
                    "permission_decisions": [
                        "internal"
                    ],
                    "validation": {
                        "internal": True,
                    },
                },
            ),
        )
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    body = json.loads(
        projection.output_json
    )

    assert (
        "metadata"
        not in body
    )


def test_waiting_is_not_terminal():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status=(
                    "waiting_for_permission"
                ),
                message=(
                    "Human approval required."
                ),
                confirmation_steps=[
                    2,
                    3,
                ],
                pending_confirmation_steps=[
                    2,
                    3,
                ],
            ),
        )
    )

    assert isinstance(
        projection,
        PendingPermissionUpdate,
    )

    assert not isinstance(
        projection,
        TerminalFunctionOutput,
    )


def test_waiting_preserves_confirmation_sets():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status=(
                    "waiting_for_permission"
                ),
                confirmation_steps=[
                    2,
                    3,
                ],
                pending_confirmation_steps=[
                    3,
                ],
            ),
        )
    )

    assert isinstance(
        projection,
        PendingPermissionUpdate,
    )

    assert (
        projection.confirmation_steps
        == (
            2,
            3,
        )
    )

    assert (
        projection.pending_confirmation_steps
        == (
            3,
        )
    )


def test_waiting_status_is_authority():
    _, binding = registered()

    projection = (
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status=(
                    "waiting_for_permission"
                ),
                confirmation_steps=[
                    1
                ],
                pending_confirmation_steps=[],
            ),
        )
    )

    assert isinstance(
        projection,
        PendingPermissionUpdate,
    )


def test_terminal_cannot_claim_pending_permissions():
    _, binding = registered()

    with pytest.raises(
        ValueError,
        match="pending confirmations",
    ):
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status="completed",
                pending_confirmation_steps=[
                    1
                ],
            ),
        )


def test_request_id_mismatch_rejected():
    _, binding = registered()

    with pytest.raises(
        ValueError,
        match="request_id",
    ):
        project_transport_response(
            binding,
            response(
                "00000000-0000-0000-0000-000000000000"
            ),
        )


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "planning",
        "running",
    ],
)
def test_intermediate_states_not_deliverable(
    status,
):
    _, binding = registered()

    with pytest.raises(
        ValueError,
        match="not deliverable",
    ):
        project_transport_response(
            binding,
            response(
                binding.request_id,
                status=status,
            ),
        )


def test_unknown_step_object_fails_explicitly():
    _, binding = registered()

    with pytest.raises(
        TypeError,
        match="unsupported JSON type",
    ):
        project_transport_response(
            binding,
            response(
                binding.request_id,
                step_results=[
                    {
                        "step_number": 1,
                        "capability": "test",
                        "status": "completed",
                        "data": {
                            "bad": object(),
                        },
                        "error": None,
                    }
                ],
            ),
        )


def test_adapter_does_not_import_openai_sdk():
    source = Path(
        "integrations/openai_live.py"
    ).read_text(
        encoding="utf-8"
    )

    assert (
        "import openai"
        not in source
    )

    assert (
        "from openai"
        not in source
    )
