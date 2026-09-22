from __future__ import annotations

from dataclasses import dataclass

import pytest

from integrations.openai_live_voice import (
    VoiceActionProtocolError,
)
from integrations.openai_live_voice_session import (
    RearmingVoiceActionSession,
)


# ============================================================
# FAKES
# ============================================================

class _FakeSessionResource:
    def __init__(self):
        self.updates = []

    def update(
        self,
        *,
        event_id=None,
        session,
    ):
        self.updates.append(
            {
                "event_id": event_id,
                "session": session,
            }
        )


class FakeConnection:
    def __init__(self):
        self.session = (
            _FakeSessionResource()
        )


@dataclass
class FakeActionSnapshot:
    call_id: str
    continuation_completed: bool


@dataclass
class FakeAdmission:
    call_id: str


class FakeBridge:
    def __init__(self):
        self.current_call_id = None
        self.current_snapshot = None

    def active_call_id(
        self,
    ):
        return self.current_call_id

    def snapshot(
        self,
    ):
        return self.current_snapshot

    def poll_once(
        self,
    ):
        return None

    def confirm_pending(
        self,
        *,
        confirmed_steps,
    ):
        return "request-1"

    def handle_event(
        self,
        event,
    ):
        if (
            event.get("type")
            != "response.event"
        ):
            return None

        inner = event.get(
            "event"
        )

        if not isinstance(
            inner,
            dict,
        ):
            return None

        if (
            inner.get("type")
            == "response.output_item.done"
        ):
            item = inner.get(
                "item"
            )

            if (
                isinstance(
                    item,
                    dict,
                )
                and item.get("type")
                == "function_call"
                and item.get("name")
                == "perform_action"
            ):
                call_id = (
                    item["call_id"]
                )

                if (
                    self.current_call_id
                    is None
                    or (
                        self.current_snapshot
                        is not None
                        and self.current_snapshot.continuation_completed
                    )
                ):
                    self.current_call_id = (
                        call_id
                    )

                    self.current_snapshot = (
                        FakeActionSnapshot(
                            call_id=call_id,
                            continuation_completed=False,
                        )
                    )

                return FakeAdmission(
                    call_id=call_id
                )

        if (
            inner.get("type")
            == "response.completed"
            and self.current_snapshot
            is not None
        ):
            self.current_snapshot = (
                FakeActionSnapshot(
                    call_id=(
                        self.current_snapshot.call_id
                    ),
                    continuation_completed=True,
                )
            )

        return None


# ============================================================
# EVENTS
# ============================================================

def action_event(
    call_id,
):
    return {
        "type": "response.event",
        "delegation_id": "delegation-1",
        "event": {
            "type": (
                "response.output_item.done"
            ),
            "item": {
                "type": (
                    "function_call"
                ),
                "name": (
                    "perform_action"
                ),
                "call_id": (
                    call_id
                ),
                "arguments": "{}",
            },
        },
    }


def response_completed_event():
    return {
        "type": "response.event",
        "delegation_id": "delegation-1",
        "event": {
            "type": (
                "response.completed"
            ),
        },
    }


def rearm_ack(
    event_id,
):
    return {
        "type": "session.updated",
        "client_event_id": event_id,
    }


# ============================================================
# FACTORY
# ============================================================

ACTION_INSTRUCTIONS = (
    "Translate delegated work into perform_action."
)

ACTION_TOOL_CHOICE = {
    "type": "function",
    "name": "perform_action",
}


def make_controller():
    connection = (
        FakeConnection()
    )

    bridge = (
        FakeBridge()
    )

    controller = (
        RearmingVoiceActionSession(
            connection=connection,
            bridge=bridge,
            action_instructions=(
                ACTION_INSTRUCTIONS
            ),
            action_tool_choice=(
                ACTION_TOOL_CHOICE
            ),
        )
    )

    return (
        connection,
        bridge,
        controller,
    )


# ============================================================
# TESTS
# ============================================================

def test_session_starts_ready_for_first_action():
    (
        _connection,
        _bridge,
        controller,
    ) = make_controller()

    snapshot = (
        controller.snapshot()
    )

    assert (
        snapshot.ready_for_new_action
        is True
    )

    assert (
        snapshot.rearm_acknowledged
        is True
    )


def test_continuation_rearms_tools_and_action_instructions():
    (
        connection,
        _bridge,
        controller,
    ) = make_controller()

    controller.handle_event(
        action_event(
            "call-1"
        )
    )

    snapshot = (
        controller.snapshot()
    )

    assert (
        snapshot.ready_for_new_action
        is False
    )

    controller.handle_event(
        response_completed_event()
    )

    snapshot = (
        controller.snapshot()
    )

    assert (
        snapshot.rearm_requested
        is True
    )

    assert len(
        connection.session.updates
    ) == 1

    update = (
        connection.session.updates[0]
    )

    responses = (
        update["session"]
        ["delegation"]
        ["responses"]
    )

    assert (
        responses["instructions"]
        == ACTION_INSTRUCTIONS
    )

    assert (
        responses["tool_choice"]
        == ACTION_TOOL_CHOICE
    )


def test_rearm_ack_makes_session_ready_again():
    (
        connection,
        _bridge,
        controller,
    ) = make_controller()

    controller.handle_event(
        action_event(
            "call-1"
        )
    )

    controller.handle_event(
        response_completed_event()
    )

    event_id = (
        connection.session.updates[0]
        ["event_id"]
    )

    controller.handle_event(
        rearm_ack(
            event_id
        )
    )

    snapshot = (
        controller.snapshot()
    )

    assert (
        snapshot.ready_for_new_action
        is True
    )

    assert (
        snapshot.rearm_acknowledged
        is True
    )

    assert (
        snapshot.rearm_requested
        is False
    )


def test_duplicate_completion_does_not_duplicate_rearm_update():
    (
        connection,
        _bridge,
        controller,
    ) = make_controller()

    controller.handle_event(
        action_event(
            "call-1"
        )
    )

    controller.handle_event(
        response_completed_event()
    )

    controller.handle_event(
        response_completed_event()
    )

    assert len(
        connection.session.updates
    ) == 1


def test_second_action_is_rejected_before_rearm_ack():
    (
        _connection,
        _bridge,
        controller,
    ) = make_controller()

    controller.handle_event(
        action_event(
            "call-1"
        )
    )

    controller.handle_event(
        response_completed_event()
    )

    with pytest.raises(
        VoiceActionProtocolError
    ):
        controller.handle_event(
            action_event(
                "call-2"
            )
        )


def test_second_action_is_allowed_after_rearm_ack():
    (
        connection,
        _bridge,
        controller,
    ) = make_controller()

    controller.handle_event(
        action_event(
            "call-1"
        )
    )

    controller.handle_event(
        response_completed_event()
    )

    event_id = (
        connection.session.updates[0]
        ["event_id"]
    )

    controller.handle_event(
        rearm_ack(
            event_id
        )
    )

    admission = (
        controller.handle_event(
            action_event(
                "call-2"
            )
        )
    )

    assert admission is not None

    assert (
        admission.call_id
        == "call-2"
    )

    snapshot = (
        controller.snapshot()
    )

    assert (
        snapshot.ready_for_new_action
        is False
    )
