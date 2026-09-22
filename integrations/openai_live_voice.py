"""Non-blocking action bridge for Jarvis Live voice sessions.

Task 15.5A

This module connects:

    OpenAI Live / Responses events
        -> LiveActionCoordinator
        -> ActionWorker
        -> Jarvis Action Engine

while preserving the real Live delegated-response lifecycle discovered
during Tasks 15.4D1-D3.

Important invariant:

    A terminal Jarvis result MUST NOT be sent to Live immediately.

The bridge waits for:

    perform_action
        -> Jarvis execution
        -> initial delegated response.completed
        -> session.update(tool_choice="none")
        -> session.updated
        -> function_call_output
        -> response.create
        -> continuation response.completed

WAITING_FOR_PERMISSION is never converted into a function_call_output.

The bridge itself does not own a polling thread.  poll_once() is deliberately
non-blocking so a voice runtime can call it from a small dedicated poller
without blocking the receiver that handles audio and barge-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Mapping

from core.action_worker import ActionWorker

from integrations.openai_live import (
    PendingPermissionUpdate,
    TerminalFunctionOutput,
)
from integrations.openai_live_coordinator import (
    LiveActionCoordinator,
)
from integrations.openai_live_events import (
    ResponsesEventAdapter,
)


# ============================================================
# EXCEPTIONS
# ============================================================

class VoiceActionBridgeError(RuntimeError):
    """Base error for the Live voice action bridge."""


class ConcurrentVoiceActionError(
    VoiceActionBridgeError
):
    """Raised when a second action overlaps an unfinished action."""


class VoiceActionProtocolError(
    VoiceActionBridgeError
):
    """Raised when Live/Jarvis action lifecycle invariants are violated."""


class VoiceActionDeliveryUncertainError(
    VoiceActionBridgeError
):
    """Raised when a network write may have partially succeeded."""


# ============================================================
# PUBLIC SNAPSHOT
# ============================================================

@dataclass(
    frozen=True,
    slots=True,
)
class VoiceActionSnapshot:
    delegation_id: str
    call_id: str
    request_id: str

    initial_response_completed: bool

    pending_permission: (
        PendingPermissionUpdate
        | None
    )

    terminal_output: (
        TerminalFunctionOutput
        | None
    )

    session_update_requested: bool
    session_update_acknowledged: bool
    session_update_uncertain: bool

    output_sent: bool
    output_delivery_uncertain: bool

    continuation_requested: bool
    continuation_started: bool
    continuation_completed: bool


# ============================================================
# INTERNAL STATE
# ============================================================

@dataclass(
    slots=True,
)
class _VoiceActionState:
    delegation_id: str
    call_id: str
    request_id: str

    initial_response_completed: bool = False

    pending_permission: (
        PendingPermissionUpdate
        | None
    ) = None

    terminal_output: (
        TerminalFunctionOutput
        | None
    ) = None

    session_update_requested: bool = False
    session_update_acknowledged: bool = False
    session_update_uncertain: bool = False

    output_sent: bool = False
    output_delivery_uncertain: bool = False

    continuation_requested: bool = False
    continuation_started: bool = False
    continuation_completed: bool = False


# ============================================================
# DEFERRED COORDINATOR WRITER
# ============================================================

class _DeferredTerminalWriter:
    """Capture terminal coordinator output without touching Live.

    LiveActionCoordinator always calls its writer before returning a
    TerminalFunctionOutput.  For voice integration that write must be
    deferred until the Live protocol gate is open.

    This object therefore behaves like the writer expected by the
    coordinator but stores the terminal result only.
    """

    def __init__(self) -> None:
        self._lock = RLock()

        self._outputs: dict[
            str,
            TerminalFunctionOutput,
        ] = {}

    def deliver(
        self,
        output: TerminalFunctionOutput,
    ) -> bool:
        if not isinstance(
            output,
            TerminalFunctionOutput,
        ):
            raise TypeError(
                "_DeferredTerminalWriter requires "
                "TerminalFunctionOutput"
            )

        with self._lock:
            existing = self._outputs.get(
                output.call_id
            )

            if existing is None:
                self._outputs[
                    output.call_id
                ] = output

                return True

            if existing == output:
                return False

            raise VoiceActionProtocolError(
                "call_id produced conflicting terminal outputs"
            )

    def get(
        self,
        call_id: str,
    ) -> TerminalFunctionOutput | None:
        with self._lock:
            return self._outputs.get(
                call_id
            )


# ============================================================
# EVENT CONVERSION
# ============================================================

def live_event_to_mapping(
    event,
) -> Mapping[str, object]:
    """Convert an SDK event or mapping into a plain mapping."""

    if isinstance(
        event,
        Mapping,
    ):
        return event

    model_dump = getattr(
        event,
        "model_dump",
        None,
    )

    if callable(
        model_dump
    ):
        payload = model_dump(
            mode="json"
        )

        if isinstance(
            payload,
            Mapping,
        ):
            return payload

    to_dict = getattr(
        event,
        "to_dict",
        None,
    )

    if callable(
        to_dict
    ):
        payload = to_dict()

        if isinstance(
            payload,
            Mapping,
        ):
            return payload

    raise TypeError(
        "Live event cannot be converted to a mapping"
    )


# ============================================================
# BRIDGE
# ============================================================

class VoiceActionBridge:
    """State machine joining Live voice and the Jarvis Action Engine.

    handle_event() consumes Live server events.

    poll_once() checks Jarvis exactly once with timeout=0 through
    LiveActionCoordinator and therefore never blocks waiting for an action.

    The caller may run poll_once() from a dedicated short-sleep thread while
    the existing voice receiver remains free to consume audio events.
    """

    def __init__(
        self,
        *,
        connection,
        worker: ActionWorker,
        event_adapter: (
            ResponsesEventAdapter
            | None
        ) = None,
        continuation_instructions: str = (
            "The local Jarvis Action Engine has completed the requested "
            "action. Do not call tools. Briefly communicate the action "
            "result and continue the conversation naturally."
        ),
    ) -> None:
        self._connection = connection

        self._event_adapter = (
            event_adapter
            if event_adapter is not None
            else ResponsesEventAdapter()
        )

        self._deferred_writer = (
            _DeferredTerminalWriter()
        )

        self._coordinator = (
            LiveActionCoordinator(
                worker=worker,
                event_adapter=(
                    self._event_adapter
                ),
                output_writer=(
                    self._deferred_writer
                ),
            )
        )

        self._continuation_instructions = (
            continuation_instructions
        )

        self._lock = RLock()

        self._state: (
            _VoiceActionState
            | None
        ) = None

    # ========================================================
    # PUBLIC STATE
    # ========================================================

    def snapshot(
        self,
    ) -> VoiceActionSnapshot | None:
        with self._lock:
            state = self._state

            if state is None:
                return None

            return VoiceActionSnapshot(
                delegation_id=(
                    state.delegation_id
                ),
                call_id=(
                    state.call_id
                ),
                request_id=(
                    state.request_id
                ),
                initial_response_completed=(
                    state.initial_response_completed
                ),
                pending_permission=(
                    state.pending_permission
                ),
                terminal_output=(
                    state.terminal_output
                ),
                session_update_requested=(
                    state.session_update_requested
                ),
                session_update_acknowledged=(
                    state.session_update_acknowledged
                ),
                session_update_uncertain=(
                    state.session_update_uncertain
                ),
                output_sent=(
                    state.output_sent
                ),
                output_delivery_uncertain=(
                    state.output_delivery_uncertain
                ),
                continuation_requested=(
                    state.continuation_requested
                ),
                continuation_started=(
                    state.continuation_started
                ),
                continuation_completed=(
                    state.continuation_completed
                ),
            )

    def active_call_id(
        self,
    ) -> str | None:
        with self._lock:
            if self._state is None:
                return None

            return self._state.call_id

    # ========================================================
    # LIVE EVENTS
    # ========================================================

    def handle_event(
        self,
        event,
    ):
        """Consume one Live server event without waiting for Jarvis."""

        payload = live_event_to_mapping(
            event
        )

        event_type = payload.get(
            "type"
        )

        # ----------------------------------------------------
        # RESPONSES EVENTS
        # ----------------------------------------------------

        if event_type == "response.event":
            admission = (
                self._coordinator.ingest(
                    payload
                )
            )

            if admission is not None:
                self._handle_admission(
                    admission
                )

            inner = payload.get(
                "event"
            )

            if isinstance(
                inner,
                Mapping,
            ):
                self._handle_response_event(
                    inner
                )

            return admission

        # ----------------------------------------------------
        # SESSION UPDATE ACK
        # ----------------------------------------------------

        if event_type == "session.updated":
            self._handle_session_updated()

        return None

    # ========================================================
    # NON-BLOCKING JARVIS POLL
    # ========================================================

    def poll_once(
        self,
    ):
        """Poll the active Jarvis action exactly once without blocking."""

        with self._lock:
            state = self._state

            if state is None:
                return None

            if state.terminal_output is not None:
                return state.terminal_output

            call_id = state.call_id

        projection = (
            self._coordinator.poll(
                call_id
            )
        )

        if projection is None:
            return None

        if isinstance(
            projection,
            PendingPermissionUpdate,
        ):
            with self._lock:
                state = self._require_same_state(
                    call_id
                )

                state.pending_permission = (
                    projection
                )

            return projection

        if isinstance(
            projection,
            TerminalFunctionOutput,
        ):
            with self._lock:
                state = self._require_same_state(
                    call_id
                )

                captured = (
                    self._deferred_writer.get(
                        call_id
                    )
                )

                if captured != projection:
                    raise VoiceActionProtocolError(
                        "coordinator terminal output was not "
                        "captured by deferred writer"
                    )

                state.terminal_output = (
                    projection
                )

                state.pending_permission = None

            self._maybe_request_session_update()

            return projection

        raise VoiceActionProtocolError(
            "coordinator returned an unknown projection"
        )

    # ========================================================
    # TRUSTED LOCAL CONFIRMATION
    # ========================================================

    def confirm_pending(
        self,
        *,
        confirmed_steps: frozenset[int],
    ) -> str:
        """Resume the active action through the trusted local path.

        This method is intentionally not connected to any model event.
        """

        with self._lock:
            state = self._state

            if state is None:
                raise VoiceActionProtocolError(
                    "there is no active action"
                )

            if state.pending_permission is None:
                raise VoiceActionProtocolError(
                    "active action is not waiting for permission"
                )

            call_id = state.call_id

        return self._coordinator.confirm(
            call_id,
            confirmed_steps=confirmed_steps,
        )

    # ========================================================
    # ADMISSION
    # ========================================================

    def _handle_admission(
        self,
        admission,
    ) -> None:
        with self._lock:
            state = self._state

            if state is None:
                self._state = (
                    _VoiceActionState(
                        delegation_id=(
                            admission.delegation_id
                        ),
                        call_id=(
                            admission.call_id
                        ),
                        request_id=(
                            admission.request_id
                        ),
                    )
                )

                return

            if (
                state.call_id
                == admission.call_id
            ):
                return

            if state.continuation_completed:
                self._state = (
                    _VoiceActionState(
                        delegation_id=(
                            admission.delegation_id
                        ),
                        call_id=(
                            admission.call_id
                        ),
                        request_id=(
                            admission.request_id
                        ),
                    )
                )

                return

            raise ConcurrentVoiceActionError(
                "a second perform_action call arrived while "
                "another voice action is still active"
            )

    # ========================================================
    # RESPONSES LIFECYCLE
    # ========================================================

    def _handle_response_event(
        self,
        inner: Mapping[str, object],
    ) -> None:
        inner_type = inner.get(
            "type"
        )

        if inner_type == "response.created":
            with self._lock:
                state = self._state

                if (
                    state is not None
                    and state.continuation_requested
                    and not state.continuation_started
                ):
                    state.continuation_started = (
                        True
                    )

            return

        if inner_type != "response.completed":
            return

        should_request_update = False

        with self._lock:
            state = self._state

            if state is None:
                return

            if not state.initial_response_completed:
                state.initial_response_completed = (
                    True
                )

                should_request_update = True

            elif (
                state.continuation_requested
                and state.continuation_started
                and not state.continuation_completed
            ):
                state.continuation_completed = (
                    True
                )

        if should_request_update:
            self._maybe_request_session_update()

    # ========================================================
    # SESSION UPDATE GATE
    # ========================================================

    def _maybe_request_session_update(
        self,
    ) -> None:
        with self._lock:
            state = self._state

            if state is None:
                return

            if (
                not state.initial_response_completed
                or state.terminal_output is None
            ):
                return

            if (
                state.session_update_requested
                or state.session_update_uncertain
            ):
                return

            state.session_update_requested = (
                True
            )

            call_id = state.call_id

        event_id = (
            "voice_action_disable_tools_"
            + call_id
        )

        try:
            self._connection.session.update(
                event_id=event_id,
                session={
                    "delegation": {
                        "type": "responses",
                        "responses": {
                            "tool_choice": "none",
                            "instructions": (
                                self._continuation_instructions
                            ),
                        },
                    },
                },
            )

        except Exception as error:
            with self._lock:
                state = self._require_same_state(
                    call_id
                )

                state.session_update_uncertain = (
                    True
                )

            raise VoiceActionDeliveryUncertainError(
                "session.update delivery became uncertain"
            ) from error

    def _handle_session_updated(
        self,
    ) -> None:
        with self._lock:
            state = self._state

            if state is None:
                return

            if not state.session_update_requested:
                return

            if state.session_update_acknowledged:
                return

            if state.session_update_uncertain:
                raise VoiceActionDeliveryUncertainError(
                    "cannot continue after uncertain session.update"
                )

            if state.terminal_output is None:
                raise VoiceActionProtocolError(
                    "session.updated arrived before terminal output"
                )

            state.session_update_acknowledged = (
                True
            )

            call_id = state.call_id

            output = state.terminal_output

        self._deliver_terminal_and_continue(
            call_id=call_id,
            output=output,
        )

    # ========================================================
    # TERMINAL DELIVERY
    # ========================================================

    def _deliver_terminal_and_continue(
        self,
        *,
        call_id: str,
        output: TerminalFunctionOutput,
    ) -> None:
        with self._lock:
            state = self._require_same_state(
                call_id
            )

            if state.output_sent:
                return

            if state.output_delivery_uncertain:
                raise VoiceActionDeliveryUncertainError(
                    "previous terminal delivery is uncertain"
                )

        event_id = (
            "voice_action_tool_result_"
            + call_id
        )

        continue_event_id = (
            "voice_action_continue_"
            + call_id
        )

        try:
            self._connection.response.item.create(
                event_id=event_id,
                item=(
                    output.function_call_output_item()
                ),
            )

            self._connection.response.create(
                event_id=continue_event_id
            )

        except Exception as error:
            with self._lock:
                state = self._require_same_state(
                    call_id
                )

                state.output_delivery_uncertain = (
                    True
                )

            raise VoiceActionDeliveryUncertainError(
                "terminal output delivery became uncertain"
            ) from error

        with self._lock:
            state = self._require_same_state(
                call_id
            )

            state.output_sent = True
            state.continuation_requested = (
                True
            )

    # ========================================================
    # INTERNAL CORRELATION
    # ========================================================

    def _require_same_state(
        self,
        call_id: str,
    ) -> _VoiceActionState:
        state = self._state

        if state is None:
            raise VoiceActionProtocolError(
                "voice action state disappeared"
            )

        if state.call_id != call_id:
            raise VoiceActionProtocolError(
                "voice action call_id changed unexpectedly"
            )

        return state
