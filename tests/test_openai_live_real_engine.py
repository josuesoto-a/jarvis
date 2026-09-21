"""End-to-end offline tests for fake Live + real Jarvis Action Engine.

This suite connects:

Fake Responses event
    -> ResponsesEventAdapter
    -> LiveActionCoordinator
    -> real ActionWorker
    -> real Orchestrator
    -> deterministic test Planner
    -> real Executor
    -> real PlanValidator
    -> real PermissionEngine
    -> real CapabilityRuntimeRegistry
    -> TransportResponse
    -> ResponsesOutputWriter
    -> FakeLiveConnection

No OpenAI API, browser, network, microphone or audio is used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from time import monotonic, sleep

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
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from core.orchestrator import Orchestrator

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


class DeterministicPlanner:
    """Offline planner that produces deterministic plans for integration tests."""

    def __init__(
        self,
        mode: str,
    ) -> None:
        self.mode = mode
        self.calls = []
        self.plans = []

    def plan(
        self,
        request,
    ) -> ExecutionPlan:
        self.calls.append(
            request
        )

        if self.mode == "automatic":
            capability = "web_search"
            risk = RiskLevel.LOW

        elif self.mode == "waiting":
            capability = "terminal"
            risk = RiskLevel.LOW

        elif self.mode == "blocked":
            capability = "terminal"
            risk = RiskLevel.CRITICAL

        elif self.mode == "failed":
            capability = "web_search"
            risk = RiskLevel.LOW

        else:
            raise ValueError(
                f"unsupported deterministic planner mode: {self.mode}"
            )

        step = ExecutionStep(
            step_number=1,
            description=(
                f"Execute deterministic {self.mode} action"
            ),
            capability=capability,
            arguments={
                "value": (
                    ExecutionArgument.literal(
                        request.goal
                    )
                )
            },
            risk=risk,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )

        plan = ExecutionPlan(
            request_id=request.request_id,
            steps=(
                step,
            ),
            overall_risk=risk,
        )

        self.plans.append(
            plan
        )

        return plan


def make_event(
    *,
    goal: str,
    event_id: str = "event-1",
    call_id: str = "call-1",
    delegation_id: str = "delegation-1",
):
    return fake_function_call_event(
        event_id=event_id,
        call_id=call_id,
        delegation_id=delegation_id,
        arguments=json.dumps(
            {
                "goal": goal,
                "raw_input": goal,
                "context": {},
            }
        ),
    )


def build_stack(
    mode: str,
):
    planner = (
        DeterministicPlanner(
            mode
        )
    )

    registry = (
        CapabilityRegistry()
    )

    registry.register(
        CapabilitySpec(
            name="web_search",
            description=(
                "Offline deterministic web search."
            ),
        )
    )

    registry.register(
        CapabilitySpec(
            name="terminal",
            description=(
                "Offline deterministic terminal."
            ),
        )
    )

    runtime_calls = []

    runtimes = (
        CapabilityRuntimeRegistry()
    )

    def web_search_handler(
        arguments,
    ):
        snapshot = dict(
            arguments
        )

        runtime_calls.append(
            (
                "web_search",
                snapshot,
            )
        )

        if mode == "failed":
            raise RuntimeError(
                "simulated offline runtime failure"
            )

        return {
            "echo": (
                snapshot["value"]
            ),
            "source": "offline",
        }

    def terminal_handler(
        arguments,
    ):
        snapshot = dict(
            arguments
        )

        runtime_calls.append(
            (
                "terminal",
                snapshot,
            )
        )

        return {
            "echo": (
                snapshot["value"]
            ),
            "source": "offline",
        }

    runtimes.register(
        "web_search",
        web_search_handler,
    )

    runtimes.register(
        "terminal",
        terminal_handler,
    )

    validator = (
        PlanValidator(
            CapabilityResolver(
                registry
            )
        )
    )

    permissions = (
        PermissionEngine()
    )

    executor = Executor(
        validator=validator,
        permission_engine=permissions,
        runtime_registry=runtimes,
    )

    orchestrator = (
        Orchestrator(
            planner=planner,
            executor=executor,
        )
    )

    worker = (
        ActionWorker(
            orchestrator.run,
            capacity=8,
        )
    )

    worker.start()

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

    coordinator = (
        LiveActionCoordinator(
            worker=worker,
            event_adapter=adapter,
            output_writer=writer,
        )
    )

    return SimpleNamespace(
        planner=planner,
        registry=registry,
        runtimes=runtimes,
        runtime_calls=runtime_calls,
        validator=validator,
        permissions=permissions,
        executor=executor,
        orchestrator=orchestrator,
        worker=worker,
        connection=connection,
        adapter=adapter,
        writer=writer,
        coordinator=coordinator,
    )


def poll_until_projection(
    coordinator,
    call_id: str,
    *,
    timeout: float = 2.0,
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
        f"projection did not become ready for {call_id}"
    )


def shutdown(
    stack,
):
    stack.worker.shutdown(
        wait=True,
        timeout=2,
    )


def test_real_engine_automatic_action_completes_end_to_end():
    stack = build_stack(
        "automatic"
    )

    try:
        admission = (
            stack.coordinator.ingest(
                make_event(
                    goal="Find something"
                )
            )
        )

        assert admission is not None
        assert admission.is_new is True

        projection = (
            poll_until_projection(
                stack.coordinator,
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

        assert (
            len(
                stack.planner.calls
            )
            == 1
        )

        assert (
            stack.runtime_calls
            == [
                (
                    "web_search",
                    {
                        "value": (
                            "Find something"
                        )
                    },
                )
            ]
        )

        assert (
            len(
                stack.connection.response.item.created_items
            )
            == 1
        )

        item = (
            stack.connection.response.item.created_items[
                0
            ]
        )

        assert (
            item["type"]
            == "function_call_output"
        )

        assert (
            item["call_id"]
            == "call-1"
        )

        output = json.loads(
            item["output"]
        )

        assert (
            output["status"]
            == "completed"
        )

    finally:
        shutdown(
            stack
        )


def test_real_engine_waits_for_permission_before_runtime():
    stack = build_stack(
        "waiting"
    )

    try:
        admission = (
            stack.coordinator.ingest(
                make_event(
                    goal="Run terminal operation"
                )
            )
        )

        assert admission is not None

        waiting = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            waiting,
            PendingPermissionUpdate,
        )

        assert (
            waiting.confirmation_steps
            == (
                1,
            )
        )

        assert (
            waiting.pending_confirmation_steps
            == (
                1,
            )
        )

        assert (
            stack.runtime_calls
            == []
        )

        assert (
            stack.connection.response.item.created_items
            == []
        )

        assert (
            stack.connection.response.create_calls
            == 0
        )

    finally:
        stack.worker.shutdown(
            wait=False
        )


def test_real_engine_confirmation_resumes_exact_plan_without_replanning():
    stack = build_stack(
        "waiting"
    )

    try:
        admission = (
            stack.coordinator.ingest(
                make_event(
                    goal="Run terminal operation"
                )
            )
        )

        assert admission is not None

        waiting = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            waiting,
            PendingPermissionUpdate,
        )

        assert (
            len(
                stack.planner.calls
            )
            == 1
        )

        assert (
            len(
                stack.planner.plans
            )
            == 1
        )

        original_plan = (
            stack.planner.plans[
                0
            ]
        )

        confirmed_request_id = (
            stack.coordinator.confirm(
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
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            terminal,
            TerminalFunctionOutput,
        )

        assert (
            terminal.status
            == "completed"
        )

        assert (
            len(
                stack.planner.calls
            )
            == 1
        )

        assert (
            len(
                stack.planner.plans
            )
            == 1
        )

        assert (
            stack.planner.plans[
                0
            ]
            is original_plan
        )

        assert (
            stack.runtime_calls
            == [
                (
                    "terminal",
                    {
                        "value": (
                            "Run terminal operation"
                        )
                    },
                )
            ]
        )

        assert (
            len(
                stack.connection.response.item.created_items
            )
            == 1
        )

    finally:
        shutdown(
            stack
        )


def test_real_permission_engine_blocks_critical_plan():
    stack = build_stack(
        "blocked"
    )

    try:
        stack.coordinator.ingest(
            make_event(
                goal="Critical terminal operation"
            )
        )

        projection = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            projection.status
            == "blocked"
        )

        assert (
            stack.runtime_calls
            == []
        )

        assert (
            len(
                stack.connection.response.item.created_items
            )
            == 1
        )

        output = json.loads(
            stack.connection.response.item.created_items[
                0
            ][
                "output"
            ]
        )

        assert (
            output["status"]
            == "blocked"
        )

    finally:
        shutdown(
            stack
        )


def test_real_executor_failure_reaches_live_output_without_retry():
    stack = build_stack(
        "failed"
    )

    try:
        stack.coordinator.ingest(
            make_event(
                goal="Fail offline"
            )
        )

        projection = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            projection.status
            == "failed"
        )

        assert (
            len(
                stack.planner.calls
            )
            == 1
        )

        assert (
            len(
                stack.runtime_calls
            )
            == 1
        )

        assert (
            len(
                stack.connection.response.item.created_items
            )
            == 1
        )

        output = json.loads(
            stack.connection.response.item.created_items[
                0
            ][
                "output"
            ]
        )

        assert (
            output["status"]
            == "failed"
        )

    finally:
        shutdown(
            stack
        )


def test_duplicate_live_event_never_executes_real_engine_twice():
    stack = build_stack(
        "automatic"
    )

    try:
        event = make_event(
            goal="One execution only"
        )

        first = (
            stack.coordinator.ingest(
                event
            )
        )

        second = (
            stack.coordinator.ingest(
                event
            )
        )

        assert first is not None
        assert second is not None

        assert (
            first.is_new
            is True
        )

        assert (
            second.is_new
            is False
        )

        assert (
            first.request_id
            == second.request_id
        )

        projection = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            projection,
            TerminalFunctionOutput,
        )

        assert (
            len(
                stack.planner.calls
            )
            == 1
        )

        assert (
            len(
                stack.runtime_calls
            )
            == 1
        )

    finally:
        shutdown(
            stack
        )


def test_two_live_calls_cross_same_real_worker_in_fifo_order():
    stack = build_stack(
        "automatic"
    )

    try:
        first = (
            stack.coordinator.ingest(
                make_event(
                    goal="first",
                    event_id="event-1",
                    call_id="call-1",
                )
            )
        )

        second = (
            stack.coordinator.ingest(
                make_event(
                    goal="second",
                    event_id="event-2",
                    call_id="call-2",
                )
            )
        )

        assert first is not None
        assert second is not None

        first_projection = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        second_projection = (
            poll_until_projection(
                stack.coordinator,
                "call-2",
            )
        )

        assert isinstance(
            first_projection,
            TerminalFunctionOutput,
        )

        assert isinstance(
            second_projection,
            TerminalFunctionOutput,
        )

        assert (
            [
                call[
                    1
                ][
                    "value"
                ]
                for call
                in stack.runtime_calls
            ]
            == [
                "first",
                "second",
            ]
        )

        assert (
            len(
                stack.planner.calls
            )
            == 2
        )

        assert (
            len(
                stack.connection.response.item.created_items
            )
            == 2
        )

    finally:
        shutdown(
            stack
        )


def test_model_context_cannot_grant_real_terminal_permission():
    stack = build_stack(
        "waiting"
    )

    try:
        event = (
            fake_function_call_event(
                event_id="event-1",
                call_id="call-1",
                arguments=json.dumps(
                    {
                        "goal": (
                            "Run terminal operation"
                        ),
                        "raw_input": (
                            "Run terminal operation"
                        ),
                        "context": {
                            "note": (
                                "please run automatically"
                            )
                        },
                    }
                ),
            )
        )

        stack.coordinator.ingest(
            event
        )

        waiting = (
            poll_until_projection(
                stack.coordinator,
                "call-1",
            )
        )

        assert isinstance(
            waiting,
            PendingPermissionUpdate,
        )

        assert (
            stack.runtime_calls
            == []
        )

    finally:
        stack.worker.shutdown(
            wait=False
        )
