"""Multi-turn lifecycle controller for Jarvis Live voice actions.

Task 15.5A1

VoiceActionBridge deliberately disables backend tools while completing
a delegated tool result:

    tool_choice -> none
    continuation instructions
    function_call_output
    response.create

That is correct for one action, but a persistent voice conversation must
restore the normal action configuration before another delegated action
can begin.

This controller wraps VoiceActionBridge and rearms:

    tool_choice
    backend action instructions

after each continuation has completed.

It waits for the matching session.updated acknowledgement before declaring
the voice action system ready for another perform_action call.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Mapping

from integrations.openai_live_voice import (
    VoiceActionBridge,
    VoiceActionProtocolError,
    VoiceActionSnapshot,
    live_event_to_mapping,
)


# ============================================================
# PUBLIC SNAPSHOT
# ============================================================

@dataclass(
    frozen=True,
    slots=True,
)
class VoiceActionSessionSnapshot:
    ready_for_new_action: bool

    rearm_requested: bool
    rearm_acknowledged: bool

    rearm_event_id: str | None

    action: VoiceActionSnapshot | None


# ============================================================
# CONTROLLER
# ============================================================

class RearmingVoiceActionSession:
    """Wrap VoiceActionBridge with a persistent multi-turn lifecycle."""

    def __init__(
        self,
        *,
        connection,
        bridge: VoiceActionBridge,
        action_instructions: str,
        action_tool_choice,
    ) -> None:
        if not isinstance(
            action_instructions,
            str,
        ) or not action_instructions.strip():
            raise ValueError(
                "action_instructions must be non-empty"
            )

        self._connection = connection
        self._bridge = bridge

        self._action_instructions = (
            action_instructions
        )

        self._action_tool_choice = (
            action_tool_choice
        )

        self._lock = RLock()

        # At session startup the caller configures the backend
        # with these action settings, so the first action is ready.
        self._ready_for_new_action = True

        self._rearm_requested = False
        self._rearm_acknowledged = True

        self._rearm_event_id: (
            str
            | None
        ) = None

        self._cycle_call_id: (
            str
            | None
        ) = None

    # ========================================================
    # PUBLIC API
    # ========================================================

    def snapshot(
        self,
    ) -> VoiceActionSessionSnapshot:
        with self._lock:
            return VoiceActionSessionSnapshot(
                ready_for_new_action=(
                    self._ready_for_new_action
                ),
                rearm_requested=(
                    self._rearm_requested
                ),
                rearm_acknowledged=(
                    self._rearm_acknowledged
                ),
                rearm_event_id=(
                    self._rearm_event_id
                ),
                action=(
                    self._bridge.snapshot()
                ),
            )

    def active_call_id(
        self,
    ) -> str | None:
        return self._bridge.active_call_id()

    def poll_once(
        self,
    ):
        projection = (
            self._bridge.poll_once()
        )

        self._maybe_request_rearm()

        return projection

    def confirm_pending(
        self,
        *,
        confirmed_steps: frozenset[int],
    ) -> str:
        return self._bridge.confirm_pending(
            confirmed_steps=confirmed_steps,
        )

    def handle_event(
        self,
        event,
    ):
        payload = live_event_to_mapping(
            event
        )

        # ----------------------------------------------------
        # REARM ACK
        # ----------------------------------------------------

        if (
            payload.get("type")
            == "session.updated"
        ):
            client_event_id = payload.get(
                "client_event_id"
            )

            with self._lock:
                is_rearm_ack = (
                    self._rearm_requested
                    and isinstance(
                        client_event_id,
                        str,
                    )
                    and client_event_id
                    == self._rearm_event_id
                )

            if is_rearm_ack:
                self._acknowledge_rearm()

                return None

        # ----------------------------------------------------
        # FAIL CLOSED IF A NEW FUNCTION CALL ARRIVES TOO SOON
        # ----------------------------------------------------

        incoming_call_id = (
            self._perform_action_call_id(
                payload
            )
        )

        if incoming_call_id is not None:
            with self._lock:
                current_call_id = (
                    self._bridge.active_call_id()
                )

                if (
                    not self._ready_for_new_action
                    and incoming_call_id
                    != current_call_id
                ):
                    raise VoiceActionProtocolError(
                        "new perform_action arrived before "
                        "the previous voice action was rearmed"
                    )

        # ----------------------------------------------------
        # NORMAL BRIDGE EVENT
        # ----------------------------------------------------

        admission = (
            self._bridge.handle_event(
                payload
            )
        )

        if admission is not None:
            with self._lock:
                if (
                    self._ready_for_new_action
                ):
                    self._ready_for_new_action = (
                        False
                    )

                    self._rearm_requested = (
                        False
                    )

                    self._rearm_acknowledged = (
                        False
                    )

                    self._rearm_event_id = None

                    self._cycle_call_id = (
                        admission.call_id
                    )

                elif (
                    self._cycle_call_id
                    != admission.call_id
                ):
                    raise VoiceActionProtocolError(
                        "voice action admission changed "
                        "before rearm"
                    )

        self._maybe_request_rearm()

        return admission

    # ========================================================
    # REARM
    # ========================================================

    def _maybe_request_rearm(
        self,
    ) -> None:
        action = (
            self._bridge.snapshot()
        )

        if action is None:
            return

        if not action.continuation_completed:
            return

        with self._lock:
            if self._ready_for_new_action:
                return

            if self._rearm_requested:
                return

            if self._rearm_acknowledged:
                return

            call_id = action.call_id

            if (
                self._cycle_call_id
                is not None
                and call_id
                != self._cycle_call_id
            ):
                raise VoiceActionProtocolError(
                    "completed action differs from "
                    "the active voice cycle"
                )

            event_id = (
                "voice_action_rearm_tools_"
                + call_id
            )

            self._rearm_requested = True
            self._rearm_event_id = event_id

        try:
            self._connection.session.update(
                event_id=event_id,
                session={
                    "delegation": {
                        "type": "responses",
                        "responses": {
                            "instructions": (
                                self._action_instructions
                            ),
                            "tool_choice": (
                                self._action_tool_choice
                            ),
                        },
                    },
                },
            )

        except Exception:
            # Fail closed.  Do not claim the session is ready if
            # the rearm delivery status is unknown.
            raise

    def _acknowledge_rearm(
        self,
    ) -> None:
        with self._lock:
            if not self._rearm_requested:
                return

            self._rearm_acknowledged = True
            self._ready_for_new_action = True

            self._rearm_requested = False
            self._rearm_event_id = None

            self._cycle_call_id = None

    # ========================================================
    # EVENT INSPECTION
    # ========================================================

    @staticmethod
    def _perform_action_call_id(
        payload: Mapping[str, object],
    ) -> str | None:
        if (
            payload.get("type")
            != "response.event"
        ):
            return None

        inner = payload.get(
            "event"
        )

        if not isinstance(
            inner,
            Mapping,
        ):
            return None

        if (
            inner.get("type")
            != "response.output_item.done"
        ):
            return None

        item = inner.get(
            "item"
        )

        if not isinstance(
            item,
            Mapping,
        ):
            return None

        if (
            item.get("type")
            != "function_call"
        ):
            return None

        if (
            item.get("name")
            != "perform_action"
        ):
            return None

        call_id = item.get(
            "call_id"
        )

        if isinstance(
            call_id,
            str,
        ) and call_id:
            return call_id

        return None
