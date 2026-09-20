"""Deterministic, offline tests for the single action worker."""
import ast
import builtins
import inspect
import json
import socket
import subprocess
import sys
import webbrowser
from queue import Full, Queue
from threading import Barrier, Event, Lock, Thread, current_thread
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import core.action_worker as worker_module
from core.action_worker import ActionWorker
from core.contracts import ActionRequest, ActionStatus
from core.orchestrator import OrchestrationResult


LIMIT = 5
REQUEST_ID = "c2ec0b09-df60-44df-bc9d-e8eae901bb70"


def payload(**changes):
    return {"goal": "Process", "raw_input": "Original input", **changes}


def completed(request):
    return OrchestrationResult(request.request_id, ActionStatus.COMPLETED)


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External effects are forbidden in worker tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, forbidden)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"openai", "capabilities"}:
            forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


@pytest.fixture
def harness():
    workers, gates, callers = [], [], []

    def gate():
        event = Event()
        gates.append(event)
        return event

    def worker(run=completed, capacity=8, start=True):
        instance = ActionWorker(run, capacity=capacity)
        workers.append(instance)
        if start:
            instance.start()
        return instance

    def launch(function):
        results = Queue()

        def invoke():
            try:
                results.put((True, function()))
            except BaseException as error:
                results.put((False, error))

        thread = Thread(target=invoke, name="test-caller", daemon=True)
        callers.append(thread)
        thread.start()
        return results

    yield SimpleNamespace(worker=worker, gate=gate, launch=launch)
    for event in gates:
        event.set()
    for instance in workers:
        instance.shutdown(wait=True, timeout=LIMIT)
    for thread in callers:
        thread.join(LIMIT)
        assert not thread.is_alive(), "Caller deadlock"


def returned(results):
    ok, value = results.get(timeout=LIMIT)
    if not ok:
        raise value
    return value


def test_submit_returns_while_action_is_blocked_in_another_thread(harness):
    entered, release = Event(), harness.gate()
    threads = []
    exited = Event()

    def run(request):
        threads.append(current_thread())
        entered.set()
        try:
            assert release.wait(LIMIT)
            return completed(request)
        finally:
            exited.set()

    worker = harness.worker(run)
    submitted = harness.launch(lambda: worker.submit(payload()))
    request_id = returned(submitted)
    assert UUID(request_id).version == 4
    assert entered.wait(LIMIT)
    assert not release.is_set()
    assert not exited.is_set()
    assert threads[0] is not current_thread()
    assert threads[0].name == "action-worker"
    assert not threads[0].daemon
    with pytest.raises(TimeoutError):
        worker.result(request_id)
    release.set()
    assert worker.result(request_id, timeout=LIMIT)["request_id"] == request_id


def test_one_worker_sequential_fifo_and_repeated_start(harness):
    entered, release = Event(), harness.gate()
    lock = Lock()
    order, identities = [], []
    active = 0

    def run(request):
        nonlocal active
        with lock:
            active += 1
            assert active == 1
            order.append(request.goal)
            identities.append(current_thread())
        if request.goal == "first":
            entered.set()
            assert release.wait(LIMIT)
        with lock:
            active -= 1
        return completed(request)

    worker = harness.worker(run, capacity=2)
    starts = [harness.launch(worker.start) for _ in range(4)]
    for started in starts:
        returned(started)
    first = worker.submit(payload(goal="first"))
    assert entered.wait(LIMIT)
    second = worker.submit(payload(goal="second"))
    third = worker.submit(payload(goal="third"))
    with lock:
        assert order == ["first"]
    release.set()
    for request_id in (third, first, second):
        assert worker.result(request_id, timeout=LIMIT)["request_id"] == request_id
    assert order == ["first", "second", "third"]
    assert len(set(identities)) == 1


@pytest.mark.parametrize("supplied", [False, True])
def test_correlation_is_available_before_execution_and_payload_is_detached(harness, supplied):
    entered, release = Event(), harness.gate()
    captured = []

    def run(request):
        if request.goal == "hold":
            entered.set()
            assert release.wait(LIMIT)
        captured.append(request)
        return completed(request)

    worker = harness.worker(run)
    hold = worker.submit(payload(goal="hold"))
    assert entered.wait(LIMIT)
    incoming = payload(context={"nested": [{"value": 1}]})
    if supplied:
        incoming["request_id"] = REQUEST_ID.upper()
    request_id = worker.submit(incoming)
    if supplied:
        assert request_id == REQUEST_ID
    else:
        assert UUID(request_id).version == 4
        assert "request_id" not in incoming
    incoming["goal"] = "changed"
    incoming["raw_input"] = "changed"
    incoming["request_id"] = str(uuid4())
    incoming["context"]["nested"][0]["value"] = 2
    release.set()
    worker.result(hold, timeout=LIMIT)
    result = worker.result(request_id, timeout=LIMIT)
    assert result["request_id"] == request_id
    assert isinstance(captured[1], ActionRequest)
    assert str(captured[1].request_id) == request_id
    assert captured[1].goal == "Process"
    assert captured[1].raw_input == "Original input"
    assert captured[1].context == {"nested": [{"value": 1}]}


@pytest.mark.parametrize("status", [
    ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.BLOCKED,
    ActionStatus.WAITING_FOR_PERMISSION,
])
def test_status_is_a_complete_transport_response(harness, status):
    worker = harness.worker(lambda request: OrchestrationResult(
        request.request_id, status, error="domain error" if status == ActionStatus.FAILED else None,
    ))
    request_id = worker.submit(payload())
    result = worker.result(request_id, timeout=LIMIT)
    assert result == {
        "request_id": request_id, "status": status.value,
        "message": None, "error": "domain error" if status == ActionStatus.FAILED else None,
        "step_results": [], "confirmation_steps": [], "pending_confirmation_steps": [], "metadata": {},
    }
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize("failure", ["execution", "broken_str", "conversion", "wrong_id"])
def test_failure_is_correlated_and_worker_processes_next_job(harness, failure):
    threads = []

    class BadError(Exception):
        def __str__(self):
            raise RuntimeError("broken exception formatter")

    def run(request):
        threads.append(current_thread())
        if request.goal == "bad":
            if failure == "execution":
                raise RuntimeError("unexpected")
            if failure == "broken_str":
                raise BadError()
            if failure == "conversion":
                return OrchestrationResult(request.request_id, ActionStatus.COMPLETED, error=object())
            return OrchestrationResult(uuid4(), ActionStatus.COMPLETED)
        return completed(request)

    worker = harness.worker(run)
    first = worker.submit(payload(goal="bad"))
    second = worker.submit(payload())
    failed = worker.result(first, timeout=LIMIT)
    assert failed["status"] == "failed"
    assert failed["request_id"] == first
    assert failed["metadata"]["worker_error"]["phase"] == (
        "execution" if failure in {"execution", "broken_str"} else "response_conversion"
    )
    assert json.loads(json.dumps(failed, allow_nan=False)) == failed
    assert worker.result(second, timeout=LIMIT)["status"] == "completed"
    assert len(set(threads)) == 1


def test_queue_full_rejects_promptly_without_registering_or_executing(harness):
    entered, release = Event(), harness.gate()
    calls = []

    def run(request):
        calls.append(request.goal)
        if request.goal == "first":
            entered.set()
            assert release.wait(LIMIT)
        return completed(request)

    worker = harness.worker(run, capacity=1)
    first = worker.submit(payload(goal="first"))
    assert entered.wait(LIMIT)
    second = worker.submit(payload(goal="second"))
    attempt = harness.launch(lambda: worker.submit(payload(request_id=REQUEST_ID)))
    ok, error = attempt.get(timeout=LIMIT)
    assert not ok and isinstance(error, Full)
    with pytest.raises(KeyError):
        worker.result(REQUEST_ID)
    assert calls == ["first"]
    release.set()
    worker.result(first, timeout=LIMIT)
    worker.result(second, timeout=LIMIT)
    # Failed admission did not reserve the ID.
    assert worker.submit(payload(request_id=REQUEST_ID)) == REQUEST_ID
    worker.result(REQUEST_ID, timeout=LIMIT)
    assert calls == ["first", "second", "Process"]


@pytest.mark.parametrize("incoming,error_type", [
    ({}, ValueError),
    (payload(goal=" "), ValueError),
    (payload(request_id="bad"), ValueError),
    (payload(context={"x": object()}), TypeError),
    (payload(confirmed_steps=[1]), ValueError),
])
def test_invalid_input_is_rejected_before_admission(harness, incoming, error_type):
    calls = []
    worker = harness.worker(lambda request: calls.append(request) or completed(request))
    with pytest.raises(error_type):
        worker.submit(incoming)
    good = worker.submit(payload())
    assert worker.result(good, timeout=LIMIT)["status"] == "completed"
    assert len(calls) == 1


def test_unknown_timeout_then_success_and_consumed_result(harness):
    release = harness.gate()
    worker = harness.worker(lambda request: release.wait(LIMIT) and completed(request))
    with pytest.raises(KeyError):
        worker.result(REQUEST_ID, timeout=None)
    request_id = worker.submit(payload())
    for timeout in (0, 0.01):
        with pytest.raises(TimeoutError):
            worker.result(request_id, timeout=timeout)
    waiter = harness.launch(lambda: worker.result(request_id, timeout=None))
    release.set()
    assert returned(waiter)["status"] == "completed"
    with pytest.raises(KeyError):
        worker.result(request_id)


def test_duplicate_id_rejected_even_after_terminal_consumption(harness):
    entered, release = Event(), harness.gate()
    calls = []

    def run(request):
        calls.append(request)
        entered.set()
        assert release.wait(LIMIT)
        return completed(request)

    worker = harness.worker(run)
    worker.submit(payload(request_id=REQUEST_ID))
    assert entered.wait(LIMIT)
    with pytest.raises(ValueError, match="already used"):
        worker.submit(payload(request_id=REQUEST_ID.upper()))
    release.set()
    # Observe publication without consuming and without an unbounded join.
    with worker._condition:
        assert worker._condition.wait_for(
            lambda: worker._work[REQUEST_ID].response is not None, timeout=LIMIT,
        )
    with pytest.raises(ValueError, match="already used"):
        worker.submit(payload(request_id=REQUEST_ID))
    first = worker.result(REQUEST_ID)
    first["metadata"]["caller_mutation"] = True
    with pytest.raises(ValueError, match="already used"):
        worker.submit(payload(request_id=REQUEST_ID, goal="Replacement"))
    assert len(calls) == 1


def test_concurrent_same_id_has_one_admission(harness):
    barrier = Barrier(3)
    worker = harness.worker()

    def submit():
        barrier.wait(LIMIT)
        return worker.submit(payload(request_id=REQUEST_ID))

    attempts = [harness.launch(submit) for _ in range(2)]
    barrier.wait(LIMIT)
    outcomes = [attempt.get(timeout=LIMIT) for attempt in attempts]
    assert sum(ok for ok, _ in outcomes) == 1
    assert isinstance(next(value for ok, value in outcomes if not ok), ValueError)
    assert worker.result(REQUEST_ID, timeout=LIMIT)["status"] == "completed"


def test_two_waiting_readers_only_one_consumes(harness, monkeypatch):
    entered, release = Event(), harness.gate()
    worker = harness.worker(lambda request: release.wait(LIMIT) and completed(request))
    request_id = worker.submit(payload())
    original_wait = worker._condition.wait
    waiting = 0

    def tracked_wait(timeout=None):
        nonlocal waiting
        if current_thread().name == "test-caller":
            waiting += 1
            if waiting == 2:
                entered.set()
        return original_wait(timeout)

    monkeypatch.setattr(worker._condition, "wait", tracked_wait)
    readers = [harness.launch(lambda: worker.result(request_id, timeout=LIMIT)) for _ in range(2)]
    assert entered.wait(LIMIT)
    release.set()
    outcomes = [reader.get(timeout=LIMIT) for reader in readers]
    assert sum(ok for ok, _ in outcomes) == 1
    assert isinstance(next(value for ok, value in outcomes if not ok), KeyError)


def test_waiter_cannot_observe_replacement_after_rejected_id_reuse(harness, monkeypatch):
    waiting = Event()
    release = harness.gate()
    worker = harness.worker(lambda request: release.wait(LIMIT) and completed(request))
    worker.submit(payload(request_id=REQUEST_ID))
    original_wait = worker._condition.wait

    def controlled_wait(timeout=None):
        if current_thread().name != "test-caller":
            return original_wait(timeout)
        waiting.set()
        notified = original_wait(timeout)
        # Consume and attempt forbidden ID reuse while still holding the
        # same lock, before the losing reader can recheck its old record.
        worker.result(REQUEST_ID)
        with pytest.raises(ValueError, match="already used"):
            worker.submit(payload(request_id=REQUEST_ID))
        return notified

    monkeypatch.setattr(worker._condition, "wait", controlled_wait)
    reader = harness.launch(lambda: worker.result(REQUEST_ID, timeout=LIMIT))
    assert waiting.wait(LIMIT)
    release.set()
    ok, error = reader.get(timeout=LIMIT)
    assert not ok and isinstance(error, KeyError)
    with pytest.raises(KeyError):
        worker.result(REQUEST_ID)


def test_shutdown_drains_full_queue_preserves_results_and_is_idempotent(harness):
    entered, release = Event(), harness.gate()
    calls = []

    def run(request):
        calls.append(request.goal)
        if request.goal == "first":
            entered.set()
            assert release.wait(LIMIT)
        return completed(request)

    worker = harness.worker(run, capacity=1)
    first = worker.submit(payload(goal="first"))
    assert entered.wait(LIMIT)
    second = worker.submit(payload(goal="second"))
    returned(harness.launch(worker.shutdown))
    worker.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        worker.submit(payload())
    with pytest.raises(RuntimeError, match="shut down"):
        worker.start()
    with pytest.raises(TimeoutError, match="draining"):
        worker.shutdown(wait=True, timeout=0)
    assert calls == ["first"]
    release.set()
    worker.shutdown(wait=True, timeout=LIMIT)
    worker.shutdown(wait=True, timeout=LIMIT)
    assert worker.result(first)["status"] == "completed"
    assert worker.result(second)["status"] == "completed"
    assert calls == ["first", "second"]
    assert not worker._thread.is_alive()


def test_shutdown_before_start_and_submit_before_start(harness):
    worker = harness.worker(start=False)
    with pytest.raises(RuntimeError, match="not been started"):
        worker.submit(payload())
    worker.shutdown(wait=True)
    worker.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        worker.start()
    with pytest.raises(RuntimeError, match="shut down"):
        worker.submit(payload())


def test_idle_worker_blocks_on_condition_and_shutdown_wakes_it(harness, monkeypatch):
    worker = harness.worker(start=False)
    waiting = Event()
    original_wait = worker._condition.wait
    calls = []

    def tracked_wait(timeout=None):
        calls.append(timeout)
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(worker._condition, "wait", tracked_wait)
    worker.start()
    assert waiting.wait(LIMIT)
    worker.shutdown(wait=True, timeout=LIMIT)
    assert calls and all(timeout is None for timeout in calls)
    assert not worker._thread.is_alive()


def test_shutdown_during_normalization_rejects_submit_without_losing_work(harness, monkeypatch):
    normalizing, release = Event(), harness.gate()
    original_conversion = worker_module.to_action_request
    calls = []
    worker = harness.worker(lambda request: calls.append(request) or completed(request))

    def convert(incoming):
        normalizing.set()
        assert release.wait(LIMIT)
        return original_conversion(incoming)

    monkeypatch.setattr(worker_module, "to_action_request", convert)
    submitted = harness.launch(lambda: worker.submit(payload(request_id=REQUEST_ID)))
    assert normalizing.wait(LIMIT)
    worker.shutdown(wait=True, timeout=LIMIT)
    release.set()
    ok, error = submitted.get(timeout=LIMIT)
    assert not ok and isinstance(error, RuntimeError)
    assert calls == []
    with pytest.raises(KeyError):
        worker.result(REQUEST_ID)


def test_simultaneous_shutdown_and_submit_is_rejected_or_drained(harness):
    barrier = Barrier(3)
    worker = harness.worker()

    def submit():
        barrier.wait(LIMIT)
        return worker.submit(payload())

    def shutdown():
        barrier.wait(LIMIT)
        worker.shutdown(wait=True, timeout=LIMIT)

    submitted, stopped = harness.launch(submit), harness.launch(shutdown)
    barrier.wait(LIMIT)
    returned(stopped)
    accepted, value = submitted.get(timeout=LIMIT)
    if accepted:
        assert worker.result(value)["status"] == "completed"
    else:
        assert isinstance(value, RuntimeError)


def test_reentrant_waits_are_rejected_instead_of_deadlocking(harness):
    ready = harness.gate()
    request_id = None

    def run(request):
        assert ready.wait(LIMIT)
        with pytest.raises(RuntimeError, match="join itself"):
            worker.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="own results"):
            worker.result(request_id, timeout=None)
        # Nonwaiting shutdown is safe even from the worker.
        worker.shutdown()
        return completed(request)

    worker = harness.worker(run)
    request_id = worker.submit(payload())
    ready.set()
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, None])
def test_capacity_must_be_finite_positive_integer(capacity):
    with pytest.raises(ValueError, match="capacity"):
        ActionWorker(completed, capacity=capacity)


def test_runner_must_be_callable():
    with pytest.raises(TypeError, match="callable"):
        ActionWorker(None)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_timeouts_do_not_close_worker(harness, timeout):
    worker = harness.worker()
    with pytest.raises(ValueError, match="timeout"):
        worker.result(REQUEST_ID, timeout=timeout)
    with pytest.raises(ValueError, match="timeout"):
        worker.shutdown(wait=True, timeout=timeout)
    request_id = worker.submit(payload())
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


def test_shutdown_timeout_requires_explicit_wait(harness):
    worker = harness.worker()
    with pytest.raises(ValueError, match="wait=True"):
        worker.shutdown(timeout=1)
    request_id = worker.submit(payload())
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


def test_worker_has_only_transport_and_contract_domain_imports():
    tree = ast.parse(inspect.getsource(worker_module))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert {name for name in imports if name.startswith("core.")} == {
        "core.contracts", "core.transport", "core.orchestrator",
    }
    # Verify TYPE_CHECKING causes no runtime import of orchestration dependencies.
    script = """
import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name in {'core.orchestrator', 'core.planner', 'core.executor', 'core.permissions'}:
        raise AssertionError(name)
    if name.split('.')[0] in {'openai', 'capabilities'}:
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = checked
import core.action_worker
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=LIMIT,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("expected", ["completed", "failed", "blocked", "waiting_for_permission"])
def test_real_orchestrator_with_offline_dependencies(harness, expected):
    from core.contracts import CapabilitySpec, ExecutionPlan, ExecutionStep, RiskLevel
    from core.executor import Executor
    from core.orchestrator import Orchestrator
    from core.permissions import PermissionEngine
    from core.plan_validator import PlanValidator
    from core.registry import CapabilityRegistry
    from core.resolver import CapabilityResolver
    from core.runtime import CapabilityRuntimeRegistry

    risk = {
        "completed": RiskLevel.LOW, "failed": RiskLevel.LOW,
        "blocked": RiskLevel.CRITICAL, "waiting_for_permission": RiskLevel.MEDIUM,
    }[expected]
    calls = []

    class Planner:
        def plan(self, request):
            return ExecutionPlan(
                request.request_id,
                (ExecutionStep(1, "Offline step", "weather", risk=risk),), risk,
            )

    def handler(arguments):
        calls.append(arguments)
        if expected == "failed":
            raise ValueError("offline failure")
        return {"nested": [1, 2]}

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("weather", "Offline test"))
    runtimes = CapabilityRuntimeRegistry()
    runtimes.register("weather", handler)
    engine = Orchestrator(
        planner=Planner(),
        executor=Executor(
            validator=PlanValidator(CapabilityResolver(registry)),
            permission_engine=PermissionEngine(), runtime_registry=runtimes,
        ),
    )
    worker = harness.worker(engine.run)
    request_id = worker.submit(payload(
        request_id=REQUEST_ID, context={"confirmed_steps": [1], "permission": "automatic"},
    ))
    result = worker.result(request_id, timeout=LIMIT)
    assert result["status"] == expected
    assert result["request_id"] == REQUEST_ID
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert len(calls) == (1 if expected in {"completed", "failed"} else 0)
    if expected == "waiting_for_permission":
        assert result["confirmation_steps"] == [1]
        # Same identity and context cannot grant permission or resume a plan.
        with pytest.raises(ValueError, match="already used"):
            worker.submit(payload(request_id=REQUEST_ID))
        assert calls == []


def test_concurrent_shutdown_waiters_both_finish_after_draining(harness):
    entered, release = Event(), harness.gate()

    def run(request):
        entered.set()
        assert release.wait(LIMIT)
        return completed(request)

    worker = harness.worker(run)
    request_id = worker.submit(payload())
    assert entered.wait(LIMIT)
    barrier = Barrier(3)

    def shutdown():
        barrier.wait(LIMIT)
        worker.shutdown(wait=True, timeout=LIMIT)

    waiters = [harness.launch(shutdown) for _ in range(2)]
    barrier.wait(LIMIT)
    release.set()
    for waiter in waiters:
        returned(waiter)
    assert worker.result(request_id)["status"] == "completed"
    assert not worker._thread.is_alive()


def test_failed_thread_start_can_be_retried(harness, monkeypatch):
    worker = harness.worker(start=False)
    original_start = Thread.start

    def failed_start(self):
        raise RuntimeError("thread resource unavailable")

    monkeypatch.setattr(Thread, "start", failed_start)
    with pytest.raises(RuntimeError, match="resource"):
        worker.start()
    monkeypatch.setattr(Thread, "start", original_start)
    worker.start()
    request_id = worker.submit(payload())
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


def test_cyclic_input_is_not_admitted_and_does_not_reserve_id(harness):
    worker = harness.worker()
    incoming = payload(request_id=REQUEST_ID, context={})
    incoming["context"]["cycle"] = incoming["context"]
    with pytest.raises(ValueError, match="cyclic"):
        worker.submit(incoming)
    assert worker.submit(payload(request_id=REQUEST_ID)) == REQUEST_ID
    assert worker.result(REQUEST_ID, timeout=LIMIT)["status"] == "completed"


def test_waiting_result_survives_shutdown_notification(harness, monkeypatch):
    entered, waiting, release = Event(), Event(), harness.gate()

    def run(request):
        entered.set()
        assert release.wait(LIMIT)
        return completed(request)

    worker = harness.worker(run)
    request_id = worker.submit(payload())
    assert entered.wait(LIMIT)
    original_wait = worker._condition.wait

    def tracked_wait(timeout=None):
        if current_thread().name == "test-caller":
            waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(worker._condition, "wait", tracked_wait)
    reader = harness.launch(lambda: worker.result(request_id, timeout=None))
    assert waiting.wait(LIMIT)
    worker.shutdown()
    release.set()
    assert returned(reader)["status"] == "completed"
    worker.shutdown(wait=True, timeout=LIMIT)
    with pytest.raises(KeyError):
        worker.result(request_id)