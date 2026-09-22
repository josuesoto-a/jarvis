from __future__ import annotations

import json
import time

import pytest

from core.action_worker import ActionWorker
from core.contracts import (
    CapabilitySpec,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.executor import Executor
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry

from integrations.openai_live import (
    PendingPermissionUpdate,
    TerminalFunctionOutput,
)
from integrations.openai_live_voice import (
    VoiceActionBridge,
)


# ============================================================
# FAKE LIVE CONNECTION
# ============================================================

class _FakeSession:
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


class _FakeResponseItem:
    def __init__(self):
        self.created = []

    def create(
        self,
        *,
        item,
        event_id=None,
    ):
        self.created.append(
            {
                "event_id": event_id,
                "item": item,
            }
        )


class _FakeResponse:
    def __init__(self):
        self.item = _FakeResponseItem()
        self.created = []

    def create(
        self,
        *,
        event_id=None,
    ):
        self.created.append(
            {
                "event_id": event_id,
            }
        )


class FakeLiveConnection:
    def __init__(self):
        self.session = _FakeSession()
        self.response = _FakeResponse()


# ============================================================
# TEST PLANNER
# ============================================================

class DeterministicPlanner:
    def __init__(
        self,
        *,
        capability: str,
        permission: PermissionMode,
    ):
        self.capability = capability
        self.permission = permission
        self.calls = []

    def plan(
        self,
        request,
    ):
        self.calls.append(
            request
        )

        return ExecutionPlan(
            request_id=request.request_id,
            steps=(
                ExecutionStep(
                    step_number=1,
                    description=(
                        "Voice action bridge test"
                    ),
                    capability=(
                        self.capability
                    ),
                    arguments={
                        "query": (
                            ExecutionArgument.literal(
                                request.goal
                            )
                        ),
                    },
                    risk=RiskLevel.LOW,
                    permission=(
                        self.permission
                    ),
                ),
            ),
            overall_risk=RiskLevel.LOW,
        )


# ============================================================
# ENGINE FACTORY
# ============================================================

def build_worker(
    *,
    capability="web_search",
    permission=PermissionMode.AUTOMATIC,
):
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            capability,
            "Offline voice bridge test",
        )
    )

    runtime_calls = []

    runtimes = (
        CapabilityRuntimeRegistry()
    )

    def handler(
        arguments,
    ):
        runtime_calls.append(
            dict(arguments)
        )

        return {
            "ok": True,
            "arguments": (
                dict(arguments)
            ),
        }

    runtimes.register(
        capability,
        handler,
    )

    planner = (
        DeterministicPlanner(
            capability=capability,
            permission=permission,
        )
    )

    executor = Executor(
        validator=(
            PlanValidator(
                CapabilityResolver(
                    registry
                )
            )
        ),
        permission_engine=(
            PermissionEngine()
        ),
        runtime_registry=runtimes,
    )

    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )

    worker = ActionWorker(
        orchestrator.run,
        capacity=8,
    )

    worker.start()

    return (
        worker,
        planner,
        runtime_calls,
    )


# ============================================================
# EVENTS
# ============================================================

def perform_action_event(
    *,
    delegation_id="delegation-1",
    call_id="call-1",
    event_id="event-tool-1",
):
    return {
        "type": "response.event",
        "event_id": event_id,
        "delegation_id": (
            delegation_id
        ),
        "event": {
            "type": (
                "response.output_item.done"
            ),
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": "perform_action",
                "arguments": json.dumps(
                    {
                        "goal": (
                            "Run voice bridge test"
                        ),
                        "raw_input": (
                            "Run voice bridge test"
                        ),
                        "context": {},
                    }
                ),
                "status": "completed",
            },
        },
    }


def delegated_response_created(
    *,
    delegation_id="delegation-1",
    response_id="response-2",
):
    return {
        "type": "response.event",
        "event_id": (
            "event-response-created"
        ),
        "delegation_id": (
            delegation_id
        ),
        "event": {
            "type": "response.created",
            "response": {
                "id": response_id,
            },
        },
    }


def delegated_response_completed(
    *,
    delegation_id="delegation-1",
):
    return {
        "type": "response.event",
        "event_id": (
            "event-response-completed"
        ),
        "delegation_id": (
            delegation_id
        ),
        "event": {
            "type": (
                "response.completed"
            ),
            "response": {
                "id": "response-1",
            },
        },
    }


def session_updated_event():
    return {
        "type": "session.updated",
        "event_id": (
            "event-session-updated"
        ),
    }


# ============================================================
# POLLING HELPERS
# ============================================================

def poll_until(
    bridge,
    predicate,
    *,
    attempts=1000,
):
    last_projection = None

    for _ in range(
        attempts
    ):
        last_projection = (
            bridge.poll_once()
        )

        snapshot = (
            bridge.snapshot()
        )

        if predicate(
            snapshot,
            last_projection,
        ):
            return (
                snapshot,
                last_projection,
            )

        time.sleep(
            0.001
        )

    raise AssertionError(
        "bridge did not reach expected state"
    )


# ============================================================
# TESTS
# ============================================================

def test_terminal_output_waits_for_initial_response_completed():
    (
        worker,
        planner,
        runtime_calls,
    ) = build_worker()

    connection = (
        FakeLiveConnection()
    )

    bridge = VoiceActionBridge(
        connection=connection,
        worker=worker,
    )

    try:
        admission = bridge.handle_event(
            perform_action_event()
        )

        assert admission is not None
        assert admission.is_new is True

        (
            snapshot,
            projection,
        ) = poll_until(
            bridge,
            lambda snapshot, projection: (
                snapshot is not None
                and snapshot.terminal_output
                is not None
            ),
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert snapshot is not None

        assert (
            snapshot.initial_response_completed
            is False
        )

        assert (
            connection.session.updates
            == []
        )

        assert (
            connection.response.item.created
            == []
        )

        assert (
            connection.response.created
            == []
        )

        bridge.handle_event(
            delegated_response_completed()
        )

        snapshot = bridge.snapshot()

        assert snapshot is not None

        assert (
            snapshot.initial_response_completed
            is True
        )

        assert (
            snapshot.session_update_requested
            is True
        )

        assert len(
            connection.session.updates
        ) == 1

        assert (
            connection.response.item.created
            == []
        )

        # session.updated is the real protocol gate.
        bridge.handle_event(
            session_updated_event()
        )

        snapshot = bridge.snapshot()

        assert snapshot is not None

        assert (
            snapshot.session_update_acknowledged
            is True
        )

        assert (
            snapshot.output_sent
            is True
        )

        assert (
            snapshot.continuation_requested
            is True
        )

        assert len(
            connection.response.item.created
        ) == 1

        assert len(
            connection.response.created
        ) == 1

        assert len(
            planner.calls
        ) == 1

        assert len(
            runtime_calls
        ) == 1

    finally:
        worker.shutdown(
            wait=True,
            timeout=3,
        )


def test_response_completed_before_worker_finishes_is_also_safe():
    (
        worker,
        _planner,
        runtime_calls,
    ) = build_worker()

    connection = (
        FakeLiveConnection()
    )

    bridge = VoiceActionBridge(
        connection=connection,
        worker=worker,
    )

    try:
        bridge.handle_event(
            perform_action_event()
        )

        # The Live response may finish before the worker thread is
        # observed as terminal by the voice poller.
        bridge.handle_event(
            delegated_response_completed()
        )

        snapshot = bridge.snapshot()

        assert snapshot is not None

        assert (
            snapshot.initial_response_completed
            is True
        )

        assert (
            connection.session.updates
            == []
        )

        (
            snapshot,
            projection,
        ) = poll_until(
            bridge,
            lambda snapshot, projection: (
                snapshot is not None
                and snapshot.session_update_requested
            ),
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert snapshot is not None

        assert len(
            connection.session.updates
        ) == 1

        assert (
            connection.response.item.created
            == []
        )

        bridge.handle_event(
            session_updated_event()
        )

        assert len(
            connection.response.item.created
        ) == 1

        assert len(
            connection.response.created
        ) == 1

        assert len(
            runtime_calls
        ) == 1

    finally:
        worker.shutdown(
            wait=True,
            timeout=3,
        )


def test_waiting_for_permission_never_completes_live_tool_call():
    (
        worker,
        planner,
        runtime_calls,
    ) = build_worker(
        capability="terminal",
        permission=(
            PermissionMode.CONFIRM_BEFORE_EXECUTION
        ),
    )

    connection = (
        FakeLiveConnection()
    )

    bridge = VoiceActionBridge(
        connection=connection,
        worker=worker,
    )

    try:
        bridge.handle_event(
            perform_action_event()
        )

        bridge.handle_event(
            delegated_response_completed()
        )

        (
            snapshot,
            projection,
        ) = poll_until(
            bridge,
            lambda snapshot, projection: (
                snapshot is not None
                and snapshot.pending_permission
                is not None
            ),
        )

        assert isinstance(
            projection,
            PendingPermissionUpdate,
        )

        assert snapshot is not None

        assert (
            snapshot.terminal_output
            is None
        )

        assert (
            snapshot.output_sent
            is False
        )

        assert (
            snapshot.session_update_requested
            is False
        )

        assert (
            connection.session.updates
            == []
        )

        assert (
            connection.response.item.created
            == []
        )

        assert (
            connection.response.created
            == []
        )

        assert len(
            planner.calls
        ) == 1

        # Permission blocked execution.
        assert (
            runtime_calls
            == []
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=3,
        )


def test_continuation_completion_closes_current_voice_action_cycle():
    (
        worker,
        _planner,
        _runtime_calls,
    ) = build_worker()

    connection = (
        FakeLiveConnection()
    )

    bridge = VoiceActionBridge(
        connection=connection,
        worker=worker,
    )

    try:
        bridge.handle_event(
            perform_action_event()
        )

        bridge.handle_event(
            delegated_response_completed()
        )

        poll_until(
            bridge,
            lambda snapshot, projection: (
                snapshot is not None
                and snapshot.session_update_requested
            ),
        )

        bridge.handle_event(
            session_updated_event()
        )

        bridge.handle_event(
            delegated_response_created(
                response_id=(
                    "continuation-response"
                )
            )
        )

        snapshot = bridge.snapshot()

        assert snapshot is not None

        assert (
            snapshot.continuation_started
            is True
        )

        bridge.handle_event(
            delegated_response_completed()
        )

        snapshot = bridge.snapshot()

        assert snapshot is not None

        assert (
            snapshot.continuation_completed
            is True
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=3,
        )


def test_duplicate_session_updated_does_not_duplicate_output():
    (
        worker,
        _planner,
        _runtime_calls,
    ) = build_worker()

    connection = (
        FakeLiveConnection()
    )

    bridge = VoiceActionBridge(
        connection=connection,
        worker=worker,
    )

    try:
        bridge.handle_event(
            perform_action_event()
        )

        bridge.handle_event(
            delegated_response_completed()
        )

        poll_until(
            bridge,
            lambda snapshot, projection: (
                snapshot is not None
                and snapshot.session_update_requested
            ),
        )

        bridge.handle_event(
            session_updated_event()
        )

        bridge.handle_event(
            session_updated_event()
        )

        assert len(
            connection.response.item.created
        ) == 1

        assert len(
            connection.response.created
        ) == 1

    finally:
        worker.shutdown(
            wait=True,
            timeout=3,
        )
