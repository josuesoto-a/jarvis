"""Offline integration tests for Responses adapter + real ActionWorker."""

from __future__ import annotations

from threading import Event, Lock
from time import monotonic, sleep
from uuid import UUID

import pytest

from core.action_worker import ActionWorker
from core.contracts import ActionStatus
from core.orchestrator import OrchestrationResult
from integrations.openai_live import (
    PendingPermissionUpdate,
    TerminalFunctionOutput,
)
from integrations.openai_live_coordinator import (
    LiveActionCoordinator,
)
from integrations.openai_live_events import (
    FakeLiveConnection,
    ResponsesEventAdapter,
    ResponsesOutputWriter,
    fake_function_call_event,
)


class DeterministicBoundEngine:
    """Small deterministic engine used only to exercise the real ActionWorker.

    It deliberately mimics the bound-method surface expected by ActionWorker:

    run()
    resume()
    validate_confirmation()

    No OpenAI, browser, network, Planner or runtime capability is used here.
    """

    def __init__(
        self,
        *,
        wait_for_permission: bool = False,
        slow_run: Event | None = None,
    ) -> None:
        self.wait_for_permission = (
            wait_for_permission
        )

        self.slow_run = slow_run

        self.run_calls = 0
        self.resume_calls = 0
        self.validation_calls = 0

        self.run_thread_ids: list[int] = []
        self.execution_order: list[str] = []

        self._waiting_request_ids: set[
            UUID
        ] = set()

        self._lock = Lock()

    def run(
        self,
        request,
    ) -> OrchestrationResult:
        from threading import get_ident

        with self._lock:
            self.run_calls += 1
            self.run_thread_ids.append(
                get_ident()
            )
            self.execution_order.append(
                request.goal
            )

        if self.slow_run is not None:
            self.slow_run.wait(
                timeout=2
            )

        if self.wait_for_permission:
            with self._lock:
                self._waiting_request_ids.add(
                    request.request_id
                )

            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.WAITING_FOR_PERMISSION,
                pending_confirmation_steps=(
                    1,
                ),
            )

        return OrchestrationResult(
            request_id=request.request_id,
            status=ActionStatus.COMPLETED,
        )

    def validate_confirmation(
        self,
        request_id: UUID,
        *,
        confirmed_steps: frozenset[int],
    ) -> None:
        with self._lock:
            self.validation_calls += 1

            if (
                request_id
                not in self._waiting_request_ids
            ):
                raise ValueError(
                    "request is not waiting"
                )

            if (
                confirmed_steps
                != frozenset(
                    {
                        1,
                    }
                )
            ):
                raise ValueError(
                    "only step 1 may be confirmed"
                )

    def resume(
        self,
        request_id: UUID,
        *,
        confirmed_steps: frozenset[int],
    ) -> OrchestrationResult:
        self.validate_confirmation(
            request_id,
            confirmed_steps=confirmed_steps,
        )

        with self._lock:
            self.resume_calls += 1
            self._waiting_request_ids.remove(
                request_id
            )

        return OrchestrationResult(
            request_id=request_id,
            status=ActionStatus.COMPLETED,
        )


def build_stack(
    engine: DeterministicBoundEngine,
):
    connection = (
        FakeLiveConnection()
    )

    adapter = (
        ResponsesEventAdapter()
    )

    writer = (
        ResponsesOutputWriter(
            connection
        )
    )

    worker = ActionWorker(
        engine.run,
        capacity=8,
    )

    worker.start()

    coordinator = (
        LiveActionCoordinator(
            worker=worker,
            event_adapter=adapter,
            output_writer=writer,
        )
    )

    return (
        connection,
        adapter,
        writer,
        worker,
        coordinator,
    )


def poll_until_projection(
    coordinator,
    call_id,
    *,
    timeout=2.0,
):
    deadline = (
        monotonic()
        + timeout
    )

    while (
        monotonic()
        < deadline
    ):
        projection = (
            coordinator.poll(
                call_id
            )
        )

        if projection is not None:
            return projection

        sleep(
            0.005
        )

    raise AssertionError(
        "projection did not become ready"
    )


def test_function_call_runs_through_real_action_worker():
    engine = (
        DeterministicBoundEngine()
    )

    (
        connection,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        admission = coordinator.ingest(
            fake_function_call_event()
        )

        assert admission is not None
        assert admission.is_new is True

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            projection.status
            == "completed"
        )

        assert engine.run_calls == 1

        assert (
            connection.response.create_calls
            == 1
        )

        assert (
            len(
                connection.response.item.created_items
            )
            == 1
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_ingest_returns_before_slow_worker_execution_finishes():
    gate = Event()

    engine = (
        DeterministicBoundEngine(
            slow_run=gate
        )
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        started = monotonic()

        admission = coordinator.ingest(
            fake_function_call_event()
        )

        elapsed = (
            monotonic()
            - started
        )

        assert admission is not None

        assert elapsed < 0.25

        gate.set()

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

    finally:
        gate.set()

        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_duplicate_function_call_does_not_execute_twice():
    engine = (
        DeterministicBoundEngine()
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        event = (
            fake_function_call_event()
        )

        first = coordinator.ingest(
            event
        )

        second = coordinator.ingest(
            event
        )

        assert first is not None
        assert second is not None

        assert first.is_new is True
        assert second.is_new is False

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert engine.run_calls == 1

    finally:
        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_request_id_is_identical_across_all_boundaries():
    engine = (
        DeterministicBoundEngine()
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        admission = coordinator.ingest(
            fake_function_call_event()
        )

        assert admission is not None

        UUID(
            admission.request_id
        )

        assert (
            coordinator.binding_request_id(
                "call-1"
            )
            == admission.request_id
        )

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            projection.request_id
            == admission.request_id
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_waiting_permission_is_not_delivered_as_terminal():
    engine = (
        DeterministicBoundEngine(
            wait_for_permission=True
        )
    )

    (
        connection,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        coordinator.ingest(
            fake_function_call_event()
        )

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            PendingPermissionUpdate,
        )

        assert (
            projection.pending_confirmation_steps
            == (
                1,
            )
        )

        assert (
            connection.response.item.created_items
            == []
        )

        assert (
            connection.response.create_calls
            == 0
        )

    finally:
        worker.shutdown(
            wait=False
        )


def test_local_confirm_resumes_same_worker_request():
    engine = (
        DeterministicBoundEngine(
            wait_for_permission=True
        )
    )

    (
        connection,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        admission = coordinator.ingest(
            fake_function_call_event()
        )

        assert admission is not None

        waiting = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            waiting,
            PendingPermissionUpdate,
        )

        confirmed_request_id = (
            coordinator.confirm(
                "call-1",
                confirmed_steps=frozenset(
                    {
                        1,
                    }
                ),
            )
        )

        assert (
            confirmed_request_id
            == admission.request_id
        )

        terminal = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            terminal,
            TerminalFunctionOutput,
        )

        assert terminal.status == "completed"

        assert (
            terminal.request_id
            == admission.request_id
        )

        assert engine.run_calls == 1
        assert engine.resume_calls == 1

        assert (
            connection.response.create_calls
            == 1
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_invalid_confirmation_is_rejected_without_resume():
    engine = (
        DeterministicBoundEngine(
            wait_for_permission=True
        )
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        coordinator.ingest(
            fake_function_call_event()
        )

        waiting = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            waiting,
            PendingPermissionUpdate,
        )

        with pytest.raises(
            ValueError
        ):
            coordinator.confirm(
                "call-1",
                confirmed_steps=frozenset(
                    {
                        999,
                    }
                ),
            )

        assert engine.resume_calls == 0

    finally:
        worker.shutdown(
            wait=False
        )


def test_two_calls_share_one_worker_fifo():
    gate = Event()

    engine = (
        DeterministicBoundEngine(
            slow_run=gate
        )
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        first = coordinator.ingest(
            fake_function_call_event(
                event_id="event-1",
                call_id="call-1",
                arguments=(
                    '{"goal":"first",'
                    '"raw_input":"first",'
                    '"context":{}}'
                ),
            )
        )

        second = coordinator.ingest(
            fake_function_call_event(
                event_id="event-2",
                call_id="call-2",
                arguments=(
                    '{"goal":"second",'
                    '"raw_input":"second",'
                    '"context":{}}'
                ),
            )
        )

        assert first is not None
        assert second is not None

        gate.set()

        first_result = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        second_result = (
            poll_until_projection(
                coordinator,
                "call-2",
            )
        )

        assert isinstance(
            first_result,
            TerminalFunctionOutput,
        )

        assert isinstance(
            second_result,
            TerminalFunctionOutput,
        )

        assert (
            engine.execution_order
            == [
                "first",
                "second",
            ]
        )

    finally:
        gate.set()

        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_poll_before_result_is_nonblocking():
    gate = Event()

    engine = (
        DeterministicBoundEngine(
            slow_run=gate
        )
    )

    (
        _,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        coordinator.ingest(
            fake_function_call_event()
        )

        started = monotonic()

        projection = coordinator.poll(
            "call-1"
        )

        elapsed = (
            monotonic()
            - started
        )

        assert projection is None
        assert elapsed < 0.25

    finally:
        gate.set()

        worker.shutdown(
            wait=True,
            timeout=2,
        )


def test_terminal_result_is_delivered_only_once():
    engine = (
        DeterministicBoundEngine()
    )

    (
        connection,
        _,
        _,
        worker,
        coordinator,
    ) = build_stack(
        engine
    )

    try:
        coordinator.ingest(
            fake_function_call_event()
        )

        projection = (
            poll_until_projection(
                coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            len(
                connection.response.item.created_items
            )
            == 1
        )

        with pytest.raises(
            KeyError
        ):
            coordinator.poll(
                "call-1"
            )

        assert (
            len(
                connection.response.item.created_items
            )
            == 1
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=2,
        )
