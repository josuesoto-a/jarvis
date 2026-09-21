"""Offline tests for fake Live/Responses event handling."""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from core.transport import TransportResponse
from integrations.openai_live import (
    CallConflictError,
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    project_transport_response,
)
from integrations.openai_live_events import (
    DeliveryUncertainError,
    FakeLiveConnection,
    ResponseEventConflictError,
    ResponsesEventAdapter,
    ResponsesOutputWriter,
    canonical_fake_event_json,
    fake_function_call_event,
)


def transport_response(
    request_id,
    *,
    status="completed",
    message=None,
    error=None,
    step_results=None,
    confirmation_steps=None,
    pending_confirmation_steps=None,
):
    return cast(
        TransportResponse,
        {
            "request_id": request_id,
            "status": status,
            "message": message,
            "error": error,
            "step_results": step_results or [],
            "confirmation_steps": confirmation_steps or [],
            "pending_confirmation_steps": pending_confirmation_steps or [],
            "metadata": {},
        },
    )


def admitted():
    adapter = ResponsesEventAdapter()

    admission = adapter.ingest(
        fake_function_call_event()
    )

    assert admission is not None

    binding = adapter.binding_for_call(
        admission.call_id
    )

    return adapter, admission, binding


def terminal_output(
    *,
    status="completed",
):
    _, admission, binding = admitted()

    projection = project_transport_response(
        binding,
        transport_response(
            admission.request_id,
            status=status,
            message="done",
        ),
    )

    assert isinstance(
        projection,
        TerminalFunctionOutput,
    )

    return projection


def test_non_response_event_is_ignored():
    adapter = ResponsesEventAdapter()

    result = adapter.ingest(
        {
            "type": "session.output_audio.delta",
            "event_id": "audio-1",
        }
    )

    assert result is None


def test_irrelevant_inner_response_event_is_ignored():
    adapter = ResponsesEventAdapter()

    result = adapter.ingest(
        {
            "type": "response.event",
            "event_id": "event-1",
            "delegation_id": "delegation-1",
            "event": {
                "type": "response.output_text.delta",
            },
        }
    )

    assert result is None


def test_non_function_output_item_is_ignored():
    adapter = ResponsesEventAdapter()

    result = adapter.ingest(
        {
            "type": "response.event",
            "event_id": "event-1",
            "delegation_id": "delegation-1",
            "event": {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                },
            },
        }
    )

    assert result is None


def test_valid_function_call_creates_action_admission():
    adapter = ResponsesEventAdapter()

    admission = adapter.ingest(
        fake_function_call_event()
    )

    assert admission is not None
    assert admission.is_new is True
    assert admission.delegation_id == "delegation-1"
    assert admission.call_id == "call-1"

    UUID(admission.request_id)

    assert admission.transport_request is not None
    assert (
        admission.transport_request["request_id"]
        == admission.request_id
    )


def test_admitted_transport_request_contains_goal():
    _, admission, _ = admitted()

    assert admission.transport_request is not None
    assert (
        admission.transport_request["goal"]
        == "Open the page"
    )


def test_missing_event_id_rejected():
    adapter = ResponsesEventAdapter()

    event = fake_function_call_event()
    del event["event_id"]

    with pytest.raises(
        TypeError,
        match="event_id",
    ):
        adapter.ingest(event)


def test_empty_event_id_rejected():
    adapter = ResponsesEventAdapter()

    with pytest.raises(
        ValueError,
        match="event_id",
    ):
        adapter.ingest(
            fake_function_call_event(
                event_id=""
            )
        )


def test_missing_delegation_id_rejected():
    adapter = ResponsesEventAdapter()

    event = fake_function_call_event()
    del event["delegation_id"]

    with pytest.raises(
        TypeError,
        match="delegation_id",
    ):
        adapter.ingest(event)


@pytest.mark.parametrize(
    "field",
    [
        "call_id",
        "name",
        "arguments",
    ],
)
def test_missing_function_call_field_rejected(
    field,
):
    adapter = ResponsesEventAdapter()

    event = fake_function_call_event()

    del event["event"]["item"][field]

    with pytest.raises(
        TypeError,
        match=field,
    ):
        adapter.ingest(event)


def test_wrong_function_name_rejected():
    adapter = ResponsesEventAdapter()

    with pytest.raises(
        ValueError,
        match="Unsupported function",
    ):
        adapter.ingest(
            fake_function_call_event(
                name="weather_tool"
            )
        )


def test_invalid_arguments_rejected():
    adapter = ResponsesEventAdapter()

    with pytest.raises(
        ValueError,
        match="valid JSON",
    ):
        adapter.ingest(
            fake_function_call_event(
                arguments="{bad"
            )
        )


def test_identical_duplicate_event_is_idempotent():
    adapter = ResponsesEventAdapter()

    event = fake_function_call_event()

    first = adapter.ingest(event)
    second = adapter.ingest(event)

    assert first is not None
    assert second is not None

    assert first.is_new is True
    assert second.is_new is False
    assert second.request_id == first.request_id
    assert second.transport_request is None


def test_same_call_new_event_id_is_idempotent():
    adapter = ResponsesEventAdapter()

    first = adapter.ingest(
        fake_function_call_event(
            event_id="event-1"
        )
    )

    second = adapter.ingest(
        fake_function_call_event(
            event_id="event-2"
        )
    )

    assert first is not None
    assert second is not None

    assert first.is_new is True
    assert second.is_new is False
    assert second.request_id == first.request_id
    assert second.transport_request is None


def test_same_event_id_different_call_conflicts():
    adapter = ResponsesEventAdapter()

    adapter.ingest(
        fake_function_call_event(
            event_id="event-1",
            call_id="call-a",
        )
    )

    with pytest.raises(
        ResponseEventConflictError,
        match="event_id",
    ):
        adapter.ingest(
            fake_function_call_event(
                event_id="event-1",
                call_id="call-b",
            )
        )


def test_same_call_different_arguments_conflicts():
    adapter = ResponsesEventAdapter()

    adapter.ingest(
        fake_function_call_event(
            event_id="event-1",
            arguments=(
                '{"goal":"one",'
                '"raw_input":"one",'
                '"context":{}}'
            ),
        )
    )

    with pytest.raises(
        CallConflictError,
        match="different arguments",
    ):
        adapter.ingest(
            fake_function_call_event(
                event_id="event-2",
                arguments=(
                    '{"goal":"two",'
                    '"raw_input":"two",'
                    '"context":{}}'
                ),
            )
        )


def test_same_call_other_delegation_conflicts():
    adapter = ResponsesEventAdapter()

    adapter.ingest(
        fake_function_call_event(
            event_id="event-1",
            delegation_id="delegation-a",
        )
    )

    with pytest.raises(
        CallConflictError,
        match="another delegation",
    ):
        adapter.ingest(
            fake_function_call_event(
                event_id="event-2",
                delegation_id="delegation-b",
            )
        )


def test_semantically_identical_arguments_are_idempotent():
    adapter = ResponsesEventAdapter()

    first = adapter.ingest(
        fake_function_call_event(
            event_id="event-1",
            arguments=(
                '{"goal":"x",'
                '"raw_input":"y",'
                '"context":{"b":2,"a":1}}'
            ),
        )
    )

    second = adapter.ingest(
        fake_function_call_event(
            event_id="event-2",
            arguments=(
                '{"context":{"a":1,"b":2},'
                '"raw_input":"y",'
                '"goal":"x"}'
            ),
        )
    )

    assert first is not None
    assert second is not None

    assert second.is_new is False
    assert second.request_id == first.request_id


def test_fake_event_json_is_deterministic():
    event = fake_function_call_event()

    first = canonical_fake_event_json(event)
    second = canonical_fake_event_json(event)

    assert first == second

    json.loads(first)


def test_output_writer_delivers_terminal_result():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    delivered = writer.deliver(
        output
    )

    assert delivered is True

    assert (
        writer.delivery_state(
            output.call_id
        )
        == "delivered"
    )

    assert connection.response.create_calls == 1

    assert (
        len(
            connection.response.item.created_items
        )
        == 1
    )


def test_written_item_is_function_call_output():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    writer.deliver(output)

    item = (
        connection.response.item.created_items[0]
    )

    assert (
        item["type"]
        == "function_call_output"
    )

    assert item["call_id"] == output.call_id
    assert item["output"] == output.output_json


def test_identical_terminal_delivery_is_idempotent():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    assert writer.deliver(output) is True
    assert writer.deliver(output) is False

    assert (
        len(
            connection.response.item.created_items
        )
        == 1
    )

    assert connection.response.create_calls == 1


def test_different_terminal_output_same_call_conflicts():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    first = terminal_output(
        status="completed"
    )

    writer.deliver(first)

    conflicting = TerminalFunctionOutput(
        delegation_id=first.delegation_id,
        call_id=first.call_id,
        request_id=first.request_id,
        status="failed",
        output_json='{"status":"failed"}',
    )

    with pytest.raises(
        CallConflictError,
        match="different terminal output",
    ):
        writer.deliver(conflicting)


def test_waiting_cannot_be_delivered_as_final():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    pending = PendingPermissionUpdate(
        delegation_id="delegation-1",
        call_id="call-1",
        request_id=(
            "00000000-0000-0000-0000-000000000001"
        ),
        confirmation_steps=(1,),
        pending_confirmation_steps=(1,),
        message="approval required",
    )

    with pytest.raises(
        TypeError,
        match="WAITING_FOR_PERMISSION",
    ):
        writer.deliver(pending)

    assert connection.response.create_calls == 0
    assert (
        connection.response.item.created_items
        == []
    )


def test_item_create_failure_becomes_uncertain():
    connection = FakeLiveConnection()

    connection.fail_next_item_create()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    with pytest.raises(
        DeliveryUncertainError
    ):
        writer.deliver(output)

    assert (
        writer.delivery_state(
            output.call_id
        )
        == "uncertain"
    )

    assert (
        connection.response.item.created_items
        == []
    )

    assert connection.response.create_calls == 0


def test_uncertain_item_failure_not_blindly_retried():
    connection = FakeLiveConnection()

    connection.fail_next_item_create()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    with pytest.raises(
        DeliveryUncertainError
    ):
        writer.deliver(output)

    with pytest.raises(
        DeliveryUncertainError,
        match="automatic retry",
    ):
        writer.deliver(output)


def test_response_create_failure_becomes_uncertain():
    connection = FakeLiveConnection()

    connection.fail_next_response_create()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    with pytest.raises(
        DeliveryUncertainError
    ):
        writer.deliver(output)

    assert (
        len(
            connection.response.item.created_items
        )
        == 1
    )

    assert connection.response.create_calls == 0

    with pytest.raises(
        DeliveryUncertainError,
        match="automatic retry",
    ):
        writer.deliver(output)

    assert (
        len(
            connection.response.item.created_items
        )
        == 1
    )


def test_closed_connection_produces_uncertain_delivery():
    connection = FakeLiveConnection()

    connection.close()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output()

    with pytest.raises(
        DeliveryUncertainError
    ):
        writer.deliver(output)

    assert (
        writer.delivery_state(
            output.call_id
        )
        == "uncertain"
    )


@pytest.mark.parametrize(
    "status",
    [
        "completed",
        "failed",
        "blocked",
    ],
)
def test_all_terminal_statuses_can_be_written(
    status,
):
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    output = terminal_output(
        status=status
    )

    assert writer.deliver(output) is True


def test_fake_connection_records_two_calls():
    connection = FakeLiveConnection()

    writer = ResponsesOutputWriter(
        connection
    )

    adapter = ResponsesEventAdapter()

    first = adapter.ingest(
        fake_function_call_event(
            event_id="event-1",
            call_id="call-1",
        )
    )

    second = adapter.ingest(
        fake_function_call_event(
            event_id="event-2",
            call_id="call-2",
        )
    )

    assert first is not None
    assert second is not None

    first_binding = adapter.binding_for_call(
        "call-1"
    )

    second_binding = adapter.binding_for_call(
        "call-2"
    )

    first_output = project_transport_response(
        first_binding,
        transport_response(
            first.request_id
        ),
    )

    second_output = project_transport_response(
        second_binding,
        transport_response(
            second.request_id
        ),
    )

    assert isinstance(
        first_output,
        TerminalFunctionOutput,
    )

    assert isinstance(
        second_output,
        TerminalFunctionOutput,
    )

    writer.deliver(first_output)
    writer.deliver(second_output)

    assert (
        len(
            connection.response.item.created_items
        )
        == 2
    )

    assert connection.response.create_calls == 2


def test_authority_smuggling_is_rejected():
    adapter = ResponsesEventAdapter()

    arguments = json.dumps(
        {
            "goal": "Do it",
            "raw_input": "Do it",
            "context": {
                "confirmed_steps": [1],
            },
        }
    )

    with pytest.raises(
        ValueError,
        match="authority field",
    ):
        adapter.ingest(
            fake_function_call_event(
                arguments=arguments
            )
        )


def test_module_does_not_import_openai_sdk():
    source = Path(
        "integrations/openai_live_events.py"
    ).read_text(
        encoding="utf-8"
    )

    assert "import openai" not in source
    assert "from openai" not in source
