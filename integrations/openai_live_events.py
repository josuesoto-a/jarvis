"""Offline event adaptation and fake Responses transport for Jarvis.

This module models only the small subset of the OpenAI Live/Responses
protocol that Jarvis currently needs for testing.

It performs no network I/O and imports no OpenAI SDK types.

Responsibilities:

- recognize completed Responses function-call items;
- convert perform_action calls through the validated 15.4A boundary;
- deduplicate repeated events/calls in memory;
- never execute actions;
- model final function_call_output delivery;
- provide a fake connection for deterministic offline tests.

Permission authority remains outside this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Protocol, cast

from core.transport import (
    JSONValue,
    TransportRequest,
    to_json_safe,
)
from integrations.openai_live import (
    ActionBinding,
    ActionCallRegistry,
    CallConflictError,
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    parse_perform_action_call,
)


class ResponseEventConflictError(ValueError):
    """One server event ID was reused for incompatible action content."""


class DeliveryUncertainError(RuntimeError):
    """A network-like send failed after delivery may already have begun."""


def _require_string(
    value: object,
    *,
    field: str,
) -> str:
    if type(value) is not str:
        raise TypeError(
            f"{field} must be a string"
        )

    if not value:
        raise ValueError(
            f"{field} must not be empty"
        )

    if value.strip() != value:
        raise ValueError(
            f"{field} must not contain leading or trailing whitespace"
        )

    return value


def _require_mapping(
    value: object,
    *,
    field: str,
) -> Mapping[str, object]:
    if not isinstance(
        value,
        Mapping,
    ):
        raise TypeError(
            f"{field} must be an object"
        )

    for key in value:
        if type(key) is not str:
            raise TypeError(
                f"{field} keys must be strings"
            )

    return cast(
        Mapping[str, object],
        value,
    )


@dataclass(frozen=True, slots=True)
class ActionAdmission:
    """Result of observing one perform_action function call.

    transport_request is present only for the first accepted observation.
    Duplicate compatible observations never request a second execution.
    """

    event_id: str
    delegation_id: str
    call_id: str
    request_id: str
    is_new: bool
    transport_request: TransportRequest | None


class ResponsesEventAdapter:
    """Recognize and deduplicate perform_action calls from response.event.

    This adapter has no worker, no connection, and no execution capability.
    """

    def __init__(
        self,
        *,
        registry: ActionCallRegistry | None = None,
    ) -> None:
        self._registry = (
            registry
            if registry is not None
            else ActionCallRegistry()
        )

        self._event_fingerprints: dict[
            str,
            tuple[
                str,
                str,
                str,
            ],
        ] = {}

    def ingest(
        self,
        event: Mapping[str, object],
    ) -> ActionAdmission | None:
        """Process one server-event snapshot.

        Returns None for events that are valid but irrelevant to action
        admission.

        Malformed function-call events fail explicitly.
        """

        if not isinstance(
            event,
            Mapping,
        ):
            raise TypeError(
                "event must be an object"
            )

        for key in event:
            if type(key) is not str:
                raise TypeError(
                    "event keys must be strings"
                )

        if (
            event.get("type")
            != "response.event"
        ):
            return None

        event_id = _require_string(
            event.get("event_id"),
            field="event_id",
        )

        inner = _require_mapping(
            event.get("event"),
            field="event.event",
        )

        if (
            inner.get("type")
            != "response.output_item.done"
        ):
            return None

        item = _require_mapping(
            inner.get("item"),
            field="event.event.item",
        )

        if (
            item.get("type")
            != "function_call"
        ):
            return None

        delegation_id = _require_string(
            event.get("delegation_id"),
            field="delegation_id",
        )

        call_id = _require_string(
            item.get("call_id"),
            field="call_id",
        )

        name = _require_string(
            item.get("name"),
            field="name",
        )

        arguments = _require_string(
            item.get("arguments"),
            field="arguments",
        )

        parsed = parse_perform_action_call(
            delegation_id=delegation_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        )

        fingerprint = (
            parsed.delegation_id,
            parsed.call_id,
            parsed.arguments_json,
        )

        previous_fingerprint = (
            self._event_fingerprints.get(
                event_id
            )
        )

        if previous_fingerprint is not None:
            if (
                previous_fingerprint
                != fingerprint
            ):
                raise ResponseEventConflictError(
                    "event_id was repeated with incompatible action content"
                )

            registration = (
                self._registry.register(
                    parsed
                )
            )

            return ActionAdmission(
                event_id=event_id,
                delegation_id=(
                    registration.binding.delegation_id
                ),
                call_id=(
                    registration.binding.call_id
                ),
                request_id=(
                    registration.binding.request_id
                ),
                is_new=False,
                transport_request=None,
            )

        registration = (
            self._registry.register(
                parsed
            )
        )

        self._event_fingerprints[
            event_id
        ] = fingerprint

        transport_request = None

        if registration.is_new:
            transport_request = (
                registration.binding.to_transport_request()
            )

        return ActionAdmission(
            event_id=event_id,
            delegation_id=(
                registration.binding.delegation_id
            ),
            call_id=(
                registration.binding.call_id
            ),
            request_id=(
                registration.binding.request_id
            ),
            is_new=registration.is_new,
            transport_request=transport_request,
        )

    def binding_for_call(
        self,
        call_id: str,
    ) -> ActionBinding:
        """Return the stable binding for a previously admitted call."""

        return self._registry.get(
            call_id
        )


class ResponseItemResourceLike(
    Protocol
):
    def create(
        self,
        *,
        item: Mapping[str, object],
    ) -> None:
        ...


class ResponseResourceLike(
    Protocol
):
    item: ResponseItemResourceLike

    def create(
        self,
    ) -> None:
        ...


class ResponsesConnectionLike(
    Protocol
):
    response: ResponseResourceLike


@dataclass(slots=True)
class _DeliveryRecord:
    fingerprint: tuple[
        str,
        str,
        str,
        str,
    ]
    state: str


class ResponsesOutputWriter:
    """Deliver terminal outputs exactly once per call within this process.

    This class deliberately treats send failures as uncertain rather than
    blindly retrying. The real SDK may queue a write before raising, so an
    automatic retry could duplicate a function_call_output.

    Reconciliation after uncertain delivery belongs to a later task.
    """

    def __init__(
        self,
        connection: ResponsesConnectionLike,
    ) -> None:
        self._connection = (
            connection
        )

        self._deliveries: dict[
            str,
            _DeliveryRecord,
        ] = {}

    def deliver(
        self,
        output: TerminalFunctionOutput,
    ) -> bool:
        """Deliver one final function output.

        Returns:
            True when this call was delivered now.
            False when the identical call was already delivered.

        Raises:
            CallConflictError for an incompatible duplicate.
            DeliveryUncertainError after a prior failed send attempt.
            TypeError for non-terminal permission updates.
        """

        if not isinstance(
            output,
            TerminalFunctionOutput,
        ):
            if isinstance(
                output,
                PendingPermissionUpdate,
            ):
                raise TypeError(
                    "WAITING_FOR_PERMISSION cannot be delivered as a final function output"
                )

            raise TypeError(
                "output must be TerminalFunctionOutput"
            )

        fingerprint = (
            output.delegation_id,
            output.call_id,
            output.request_id,
            output.output_json,
        )

        record = (
            self._deliveries.get(
                output.call_id
            )
        )

        if record is not None:
            if (
                record.fingerprint
                != fingerprint
            ):
                raise CallConflictError(
                    "call_id already has a different terminal output"
                )

            if (
                record.state
                == "delivered"
            ):
                return False

            if (
                record.state
                == "uncertain"
            ):
                raise DeliveryUncertainError(
                    "previous delivery attempt is uncertain; automatic retry is disabled"
                )

            raise RuntimeError(
                f"Unknown delivery state: {record.state}"
            )

        record = _DeliveryRecord(
            fingerprint=fingerprint,
            state="attempting",
        )

        self._deliveries[
            output.call_id
        ] = record

        try:
            self._connection.response.item.create(
                item=(
                    output.function_call_output_item()
                )
            )

            self._connection.response.create()

        except Exception as error:
            record.state = "uncertain"

            raise DeliveryUncertainError(
                "terminal output delivery became uncertain"
            ) from error

        record.state = "delivered"

        return True

    def delivery_state(
        self,
        call_id: str,
    ) -> str | None:
        record = (
            self._deliveries.get(
                call_id
            )
        )

        if record is None:
            return None

        return record.state


class _FakeResponseItemResource:
    def __init__(
        self,
        owner: "FakeLiveConnection",
    ) -> None:
        self._owner = owner

        self.created_items: list[
            dict[str, JSONValue]
        ] = []

    def create(
        self,
        *,
        item: Mapping[str, object],
    ) -> None:
        if self._owner.closed:
            raise RuntimeError(
                "fake connection is closed"
            )

        failure = (
            self._owner._next_item_failure
        )

        if failure is not None:
            self._owner._next_item_failure = None

            raise failure

        safe = to_json_safe(
            item
        )

        if type(safe) is not dict:
            raise TypeError(
                "function output item must serialize to an object"
            )

        self.created_items.append(
            cast(
                dict[str, JSONValue],
                safe,
            )
        )


class _FakeResponseResource:
    def __init__(
        self,
        owner: "FakeLiveConnection",
    ) -> None:
        self._owner = owner

        self.item = (
            _FakeResponseItemResource(
                owner
            )
        )

        self.create_calls = 0

    def create(
        self,
    ) -> None:
        if self._owner.closed:
            raise RuntimeError(
                "fake connection is closed"
            )

        failure = (
            self._owner._next_response_failure
        )

        if failure is not None:
            self._owner._next_response_failure = None

            raise failure

        self.create_calls += 1


class FakeLiveConnection:
    """Deterministic fake for the Responses output methods Jarvis needs."""

    def __init__(
        self,
    ) -> None:
        self.closed = False

        self._next_item_failure: (
            Exception
            | None
        ) = None

        self._next_response_failure: (
            Exception
            | None
        ) = None

        self.response = (
            _FakeResponseResource(
                self
            )
        )

    def close(
        self,
    ) -> None:
        self.closed = True

    def fail_next_item_create(
        self,
        error: Exception | None = None,
    ) -> None:
        self._next_item_failure = (
            error
            if error is not None
            else RuntimeError(
                "simulated item.create failure"
            )
        )

    def fail_next_response_create(
        self,
        error: Exception | None = None,
    ) -> None:
        self._next_response_failure = (
            error
            if error is not None
            else RuntimeError(
                "simulated response.create failure"
            )
        )


def fake_function_call_event(
    *,
    event_id: str = "event-1",
    delegation_id: str = "delegation-1",
    call_id: str = "call-1",
    name: str = "perform_action",
    arguments: str = (
        '{"goal":"Open the page",'
        '"raw_input":"Open the page",'
        '"context":{}}'
    ),
) -> dict[str, object]:
    """Build one fake SDK-shaped Responses function-call event."""

    return {
        "type": "response.event",
        "event_id": event_id,
        "delegation_id": delegation_id,
        "client_event_id": None,
        "event": {
            "type": (
                "response.output_item.done"
            ),
            "item": {
                "type": (
                    "function_call"
                ),
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            },
        },
    }


def canonical_fake_event_json(
    event: Mapping[str, object],
) -> str:
    """Useful diagnostic representation for deterministic tests."""

    safe = to_json_safe(
        event
    )

    return json.dumps(
        safe,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
