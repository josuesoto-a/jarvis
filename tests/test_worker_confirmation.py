"""Offline adversarial coverage for queued, plan-bound confirmations."""
from dataclasses import FrozenInstanceError, replace
from queue import Full
from threading import Barrier, Event, current_thread
from uuid import uuid4

import pytest

from core.action_worker import ActionWorker
from core.contracts import ActionStatus, ArgumentSource, ExecutionArgument, ExecutionPlan, ExecutionStep
from core.orchestrator import OrchestrationResult
from test_action_worker import LIMIT, harness, no_external_effects, payload, returned
from test_plan_resumption import setup


def submit(worker, setup):
    return worker.submit(payload(
        request_id=str(setup.request.request_id), goal=setup.request.goal,
        raw_input=setup.request.raw_input,
    ))


def published(worker, request_id):
    with worker._condition:
        assert worker._condition.wait_for(
            lambda: worker._work[request_id].state == "waiting_for_permission", timeout=LIMIT,
        )


def test_confirm_is_public_and_requires_explicit_steps():
    assert callable(ActionWorker.confirm)
    with pytest.raises(TypeError, match="confirmed_steps"):
        ActionWorker(lambda request: None).confirm(str(uuid4()))


def test_waiting_result_then_partial_confirms_preserve_plan_and_identity(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    first = worker.result(request_id, timeout=LIMIT)
    assert first["status"] == "waiting_for_permission"
    assert first["confirmation_steps"] == first["pending_confirmation_steps"] == [2, 3]
    assert setup.handler_calls == []
    assert worker.confirm(request_id, confirmed_steps=frozenset({2})) == request_id
    partial = worker.result(request_id, timeout=LIMIT)
    assert partial["status"] == "waiting_for_permission"
    assert partial["confirmation_steps"] == [2, 3]
    assert partial["pending_confirmation_steps"] == [3]
    assert setup.handler_calls == []
    with pytest.raises(ValueError, match="already confirmed"):
        worker.confirm(request_id, confirmed_steps=frozenset({2}))
    worker.confirm(request_id, confirmed_steps=frozenset({3}))
    final = worker.result(request_id, timeout=LIMIT)
    assert final["status"] == "completed"
    assert final["confirmation_steps"] == [2, 3]
    assert final["pending_confirmation_steps"] == []
    assert {r["request_id"] for r in (first, partial, final)} == {request_id}
    assert len(setup.planner_calls) == 1
    assert all(plan is setup.plan for plan, _ in setup.executions)
    assert [steps for _, steps in setup.executions] == [frozenset(), frozenset({2}), frozenset({2, 3})]
    assert len(setup.handler_calls) == 3
    with pytest.raises(ValueError, match="terminal"):
        worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))


def test_confirm_returns_before_slow_resume_on_same_single_thread(setup, harness, monkeypatch):
    entered, release = Event(), harness.gate()
    threads = []
    execute = setup.executor.execute
    resume = setup.engine.resume

    def record_execute(plan, **kwargs):
        threads.append(current_thread())
        return execute(plan, **kwargs)

    def slow_resume(request_id, **kwargs):
        threads.append(current_thread())
        entered.set()
        assert release.wait(LIMIT)
        return resume(request_id, **kwargs)

    monkeypatch.setattr(setup.executor, "execute", record_execute)
    monkeypatch.setattr(setup.engine, "resume", slow_resume)
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    # Event-gated rather than a timing assertion: return is required while resume
    # remains blocked, so slow CI cannot turn this into a false positive.
    confirmed = harness.launch(lambda: worker.confirm(request_id, confirmed_steps=frozenset({2, 3})))
    assert returned(confirmed) == request_id
    assert entered.wait(LIMIT)
    assert not release.is_set()
    assert setup.handler_calls == []
    with pytest.raises(ValueError, match="queued/running"):
        worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    with pytest.raises(TimeoutError):
        worker.result(request_id)
    release.set()
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert len(set(threads)) == 1
    assert threads[0] is worker._thread and threads[0] is not current_thread()
    assert len(setup.planner_calls) == 1


def test_confirm_before_result_keeps_all_snapshots_in_order(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    published(worker, request_id)
    worker.confirm(request_id, confirmed_steps=frozenset({2}))
    published(worker, request_id)
    worker.confirm(request_id, confirmed_steps=frozenset({3}))
    worker.shutdown(wait=True, timeout=LIMIT)
    results = [worker.result(request_id) for _ in range(3)]
    assert [r["status"] for r in results] == ["waiting_for_permission", "waiting_for_permission", "completed"]
    assert [r["pending_confirmation_steps"] for r in results] == [[2, 3], [3], []]
    with pytest.raises(KeyError):
        worker.result(request_id)


def test_two_concurrent_confirms_execute_once(setup, harness, monkeypatch):
    release, entered = harness.gate(), Event()
    resume = setup.engine.resume

    def gated(request_id, **kwargs):
        entered.set()
        assert release.wait(LIMIT)
        return resume(request_id, **kwargs)

    monkeypatch.setattr(setup.engine, "resume", gated)
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    barrier = Barrier(3)

    def confirm():
        barrier.wait(LIMIT)
        return worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))

    callers = [harness.launch(confirm) for _ in range(2)]
    barrier.wait(LIMIT)
    results = [caller.get(timeout=LIMIT) for caller in callers]
    assert sum(ok for ok, _ in results) == 1
    assert isinstance(next(value for ok, value in results if not ok), ValueError)
    assert entered.wait(LIMIT)
    release.set()
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert len(setup.executions) == 2 and len(setup.handler_calls) == 3


def test_queue_full_does_not_grant_and_retry_obeys_fifo(setup, harness, monkeypatch):
    release, entered = harness.gate(), Event()
    order = []
    run, resume = setup.engine.run, setup.engine.resume

    def gated(request):
        if request.goal == "hold":
            order.append("hold")
            entered.set()
            assert release.wait(LIMIT)
            return OrchestrationResult(request.request_id, ActionStatus.COMPLETED)
        if request.goal == "queued":
            order.append("queued")
            return OrchestrationResult(request.request_id, ActionStatus.COMPLETED)
        return run(request)

    # Keep run bound to the same owner: no separate authorization provider.
    from types import MethodType
    monkeypatch.setattr(setup.engine, "run", MethodType(lambda self, request: gated(request), setup.engine))

    def record_resume(request_id, **kwargs):
        order.append("resume")
        return resume(request_id, **kwargs)

    monkeypatch.setattr(setup.engine, "resume", record_resume)
    worker = harness.worker(setup.engine.run, capacity=1)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    hold = worker.submit(payload(goal="hold"))
    assert entered.wait(LIMIT)
    queued = worker.submit(payload(goal="queued"))
    attempt = harness.launch(lambda: worker.confirm(request_id, confirmed_steps=frozenset({2})))
    ok, error = attempt.get(timeout=LIMIT)
    assert not ok and isinstance(error, Full)
    assert len(setup.executions) == 1
    pending = setup.engine._pending[setup.request.request_id]
    assert pending.result.plan is setup.plan
    assert pending.confirmed_steps == frozenset()
    assert worker._work[request_id].state == "waiting_for_permission"
    release.set()
    worker.result(hold, timeout=LIMIT)
    worker.result(queued, timeout=LIMIT)
    worker.confirm(request_id, confirmed_steps=frozenset({3}))
    partial = worker.result(request_id, timeout=LIMIT)
    assert partial["pending_confirmation_steps"] == [2]  # rejected {2} never applied
    worker.confirm(request_id, confirmed_steps=frozenset({2}))
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert order == ["hold", "queued", "resume", "resume"]
    assert len(setup.handler_calls) == 3


def test_queued_resume_rejects_second_confirmation(setup, harness, monkeypatch):
    entered, release = Event(), harness.gate()
    execute = setup.executor.execute

    def gated(plan, **kwargs):
        if plan.request_id != setup.request.request_id:
            entered.set()
            assert release.wait(LIMIT)
        return execute(plan, **kwargs)

    monkeypatch.setattr(setup.executor, "execute", gated)
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    hold = worker.submit(payload())
    assert entered.wait(LIMIT)
    worker.confirm(request_id, confirmed_steps=frozenset({2}))
    assert worker._work[request_id].state == "queued_resume"
    with pytest.raises(ValueError, match="queued/running"):
        worker.confirm(request_id, confirmed_steps=frozenset({3}))
    assert setup.engine._pending[setup.request.request_id].confirmed_steps == frozenset()
    release.set()
    worker.result(hold, timeout=LIMIT)
    assert worker.result(request_id, timeout=LIMIT)["pending_confirmation_steps"] == [3]


def test_confirm_racing_result_does_not_lose_waiting_or_terminal(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    published(worker, request_id)
    barrier = Barrier(3)

    def read():
        barrier.wait(LIMIT)
        return worker.result(request_id, timeout=LIMIT)

    def confirm():
        barrier.wait(LIMIT)
        return worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))

    reader, confirmer = harness.launch(read), harness.launch(confirm)
    barrier.wait(LIMIT)
    assert returned(reader)["status"] == "waiting_for_permission"
    assert returned(confirmer) == request_id
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert len(setup.handler_calls) == 3


@pytest.mark.parametrize("consumed", [False, True])
def test_shutdown_never_auto_confirms_waiting_plan(setup, harness, consumed):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    published(worker, request_id)
    if consumed:
        worker.result(request_id)
    worker.shutdown(wait=True, timeout=LIMIT)
    assert len(setup.executions) == 1 and setup.handler_calls == []
    assert setup.engine._pending[setup.request.request_id].result.plan is setup.plan
    with pytest.raises(RuntimeError, match="shut down"):
        worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    if not consumed:
        assert worker.result(request_id)["status"] == "waiting_for_permission"


def test_confirm_racing_shutdown_is_rejected_or_drained_once(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    barrier = Barrier(3)

    def confirm():
        barrier.wait(LIMIT)
        return worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))

    def shutdown():
        barrier.wait(LIMIT)
        worker.shutdown(wait=True, timeout=LIMIT)

    confirmer, closer = harness.launch(confirm), harness.launch(shutdown)
    barrier.wait(LIMIT)
    returned(closer)
    ok, value = confirmer.get(timeout=LIMIT)
    if ok:
        assert worker.result(request_id)["status"] == "completed"
        assert len(setup.handler_calls) == 3
    else:
        assert isinstance(value, RuntimeError)
        assert setup.handler_calls == []
        assert len(setup.executions) == 1


@pytest.mark.parametrize("after_execution", [False, True])
def test_resume_exception_keeps_worker_alive_without_fallback(setup, harness, monkeypatch, after_execution):
    class BrokenError(Exception):
        def __str__(self):
            raise AssertionError("must not stringify exception")

    resume = setup.engine.resume
    def broken(request_id, **kwargs):
        if request_id != setup.request.request_id:
            return resume(request_id, **kwargs)
        if after_execution:
            resume(request_id, **kwargs)
        raise BrokenError()

    monkeypatch.setattr(setup.engine, "resume", broken)
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    failed = worker.result(request_id, timeout=LIMIT)
    assert failed["status"] == "failed" and failed["request_id"] == request_id
    assert failed["pending_confirmation_steps"] == []
    assert failed["metadata"]["worker_error"] == {"phase": "resume", "exception_type": "BrokenError"}
    assert len(setup.planner_calls) == 1
    with pytest.raises(ValueError, match="terminal"):
        worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    next_id = worker.submit(payload())
    assert worker.result(next_id, timeout=LIMIT)["status"] == "waiting_for_permission"
    worker.confirm(next_id, confirmed_steps=frozenset({1}))
    assert worker.result(next_id, timeout=LIMIT)["status"] == "completed"
    assert worker._thread.is_alive()
    assert len(setup.planner_calls) == 2
    assert len(setup.handler_calls) == (4 if after_execution else 1)


@pytest.mark.parametrize("steps, error_type", [
    (frozenset(), ValueError), (frozenset({1}), ValueError),
    (frozenset({99}), ValueError), (frozenset({2, 99}), ValueError),
    (frozenset({True}), ValueError), (frozenset({2.0}), ValueError),
    (frozenset({"2"}), ValueError), ({2}, TypeError), ([2], TypeError), (None, TypeError),
])
def test_invalid_confirm_is_rejected_synchronously_without_admission(setup, harness, steps, error_type):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    with pytest.raises(error_type, match="confirmed_steps"):
        worker.confirm(request_id, confirmed_steps=steps)
    assert len(setup.executions) == 1
    worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


def test_unknown_or_other_workers_id_cannot_confirm(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    other = harness.worker(setup.engine.run)
    for target, identity in ((worker, str(uuid4())), (other, request_id)):
        with pytest.raises(KeyError):
            target.confirm(identity, confirmed_steps=frozenset({2, 3}))
    assert setup.handler_calls == []
    worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"


@pytest.mark.parametrize("status", [ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.BLOCKED, ActionStatus.RUNNING])
def test_status_not_pending_list_determines_confirmability(harness, status):
    worker = harness.worker(lambda request: OrchestrationResult(
        request.request_id, status, pending_confirmation_steps=(2, 3),
    ))
    request_id = worker.submit(payload())
    with worker._condition:
        assert worker._condition.wait_for(lambda: worker._work[request_id].response is not None, timeout=LIMIT)
    with pytest.raises(ValueError, match="not waiting"):
        worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    assert worker.result(request_id)["pending_confirmation_steps"] == []


def test_waiting_status_is_authority_even_with_empty_pending_list(harness):
    worker = harness.worker(lambda request: OrchestrationResult(request.request_id, ActionStatus.WAITING_FOR_PERMISSION))
    request_id = worker.submit(payload())
    assert worker.result(request_id, timeout=LIMIT)["pending_confirmation_steps"] == []
    assert worker._work[request_id].state == "waiting_for_permission"
    with pytest.raises(RuntimeError, match="bound Orchestrator.run"):
        worker.confirm(request_id, confirmed_steps=frozenset({1}))


@pytest.mark.parametrize("consumed", [False, True])
def test_pending_id_cannot_be_reused_for_different_action(setup, harness, consumed):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    published(worker, request_id)
    if consumed:
        worker.result(request_id)
    with pytest.raises(ValueError, match="already used"):
        worker.submit(payload(request_id=request_id, goal="replacement action"))
    worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    if not consumed:
        assert worker.result(request_id)["status"] == "waiting_for_permission"
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert len(setup.planner_calls) == 1
    assert all(plan is setup.plan for plan, _ in setup.executions)


def test_mutating_transport_snapshot_cannot_change_authorization(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    waiting = worker.result(request_id, timeout=LIMIT)
    waiting["pending_confirmation_steps"][:] = [1]
    waiting["confirmation_steps"].clear()
    waiting["status"] = "completed"
    with pytest.raises(ValueError):
        worker.confirm(request_id, confirmed_steps=frozenset({1}))
    worker.confirm(request_id, confirmed_steps=frozenset({2}))
    assert worker.result(request_id, timeout=LIMIT)["pending_confirmation_steps"] == [3]


def test_stale_confirmation_rechecked_when_dequeued(setup, harness, monkeypatch):
    entered, release = Event(), harness.gate()
    resume = setup.engine.resume
    def gated(request_id, **kwargs):
        entered.set()
        assert release.wait(LIMIT)
        return resume(request_id, **kwargs)
    monkeypatch.setattr(setup.engine, "resume", gated)
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    worker.confirm(request_id, confirmed_steps=frozenset({2}))
    assert entered.wait(LIMIT)
    # Simulate a trusted independent caller consuming this same authorization.
    assert resume(setup.request.request_id, confirmed_steps=frozenset({2})).waiting_for_permission
    release.set()
    assert worker.result(request_id, timeout=LIMIT)["status"] == "failed"
    assert len(setup.executions) == 2
    assert setup.handler_calls == []


class MutableLiteral(dict):
    def strip(self):
        return self


class StringWithNestedState(str):
    pass


@pytest.mark.parametrize("factory", [True, False])
@pytest.mark.parametrize("value", [[{"target": "original"}], {"target": ["original"]}, MutableLiteral(target=["original"]), StringWithNestedState("original")])
def test_nested_mutable_literal_cannot_enter_plan(value, factory):
    # MutableLiteral used to pass __post_init__ via .strip(), permitting a
    # WAITING -> mutate nested target -> resume attack despite frozen=True.
    with pytest.raises(TypeError, match="string"):
        if factory:
            ExecutionArgument.literal(value)
        else:
            ExecutionArgument(source=ArgumentSource.LITERAL, value=value)


def test_waiting_plan_rejects_nested_mutation_and_executes_original(setup, harness):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    argument = setup.plan.steps[0].arguments["value"]
    with pytest.raises(FrozenInstanceError):
        argument.value = {"target": ["replacement"]}
    with pytest.raises(TypeError, match="string"):
        replace(argument, value=MutableLiteral(target=["replacement"]))
    with pytest.raises(TypeError):
        setup.plan.steps[0].arguments["value"] = ExecutionArgument.literal("replacement")
    setup.borrowed_arguments["value"] = ExecutionArgument.literal("replacement")
    setup.borrowed_steps.clear()
    worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    assert worker.result(request_id, timeout=LIMIT)["status"] == "completed"
    assert setup.handler_calls[0][1] == {"value": "original"}
    assert all(plan is setup.plan for plan, _ in setup.executions)


@pytest.mark.parametrize("argument", [{"nested": []}, ["mutable"], MutableLiteral(target=[])])
def test_step_rejects_mutable_argument_objects(argument):
    with pytest.raises(TypeError, match="ExecutionArgument"):
        ExecutionStep(1, "Operation", "terminal", arguments={"value": argument})


def test_plan_rejects_mutable_step_objects(setup):
    with pytest.raises(TypeError, match="ExecutionStep"):
        ExecutionPlan(setup.plan.request_id, [{"capability": "terminal"}], setup.plan.overall_risk)


def test_closed_waiting_request_wakes_result_reader(setup, harness, monkeypatch):
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    waiting = Event()
    original_wait = worker._condition.wait
    def tracked_wait(timeout=None):
        if current_thread().name == "test-caller":
            waiting.set()
        return original_wait(timeout)
    monkeypatch.setattr(worker._condition, "wait", tracked_wait)
    reader = harness.launch(lambda: worker.result(request_id, timeout=None))
    assert waiting.wait(LIMIT)
    worker.shutdown(wait=True, timeout=LIMIT)
    ok, error = reader.get(timeout=LIMIT)
    assert not ok and isinstance(error, RuntimeError)
    with pytest.raises(RuntimeError, match="cannot resume"):
        worker.result(request_id, timeout=None)
    assert setup.handler_calls == []


def test_initial_running_and_queued_requests_cannot_confirm(setup, harness, monkeypatch):
    entered, release = Event(), harness.gate()
    plan = setup.engine._planner.plan
    def gated(request):
        entered.set()
        assert release.wait(LIMIT)
        return plan(request)
    monkeypatch.setattr(setup.engine._planner, "plan", gated)
    worker = harness.worker(setup.engine.run)
    running = submit(worker, setup)
    assert entered.wait(LIMIT)
    queued = worker.submit(payload())
    for request_id in (running, queued):
        with pytest.raises(ValueError, match="not waiting"):
            worker.confirm(request_id, confirmed_steps=frozenset({2, 3}))
    assert setup.executions == []
    release.set()
    assert worker.result(running, timeout=LIMIT)["status"] == "waiting_for_permission"
    assert worker.result(queued, timeout=LIMIT)["status"] == "waiting_for_permission"


def test_resume_and_submit_share_one_fifo_and_one_thread(setup, harness, monkeypatch):
    entered, release = Event(), harness.gate()
    execute = setup.executor.execute
    calls = []
    def gated(plan, **kwargs):
        calls.append((str(plan.request_id), current_thread()))
        if len(calls) == 2:
            entered.set()
            assert release.wait(LIMIT)
        return execute(plan, **kwargs)
    monkeypatch.setattr(setup.executor, "execute", gated)
    worker = harness.worker(setup.engine.run, capacity=2)
    waiting_id = submit(worker, setup)
    worker.result(waiting_id, timeout=LIMIT)
    running_id = worker.submit(payload())
    assert entered.wait(LIMIT)
    worker.confirm(waiting_id, confirmed_steps=frozenset({2, 3}))
    last_id = worker.submit(payload())
    release.set()
    for request_id in (running_id, waiting_id, last_id):
        worker.result(request_id, timeout=LIMIT)
    assert [request_id for request_id, _ in calls] == [waiting_id, running_id, waiting_id, last_id]
    assert {thread for _, thread in calls} == {worker._thread}


def test_plan_a_confirmation_never_authorizes_plan_b(setup, harness):
    worker = harness.worker(setup.engine.run)
    first_id = submit(worker, setup)
    worker.result(first_id, timeout=LIMIT)
    second_id = worker.submit(payload(goal="other plan"))
    assert worker.result(second_id, timeout=LIMIT)["pending_confirmation_steps"] == [1]
    with pytest.raises(ValueError, match="not requested"):
        worker.confirm(second_id, confirmed_steps=frozenset({2, 3}))
    worker.confirm(first_id, confirmed_steps=frozenset({2, 3}))
    assert worker.result(first_id, timeout=LIMIT)["status"] == "completed"
    assert len(setup.handler_calls) == 3
    assert setup.executions[-1][0] is setup.plan
    worker.confirm(second_id, confirmed_steps=frozenset({1}))
    assert worker.result(second_id, timeout=LIMIT)["status"] == "completed"
    assert setup.handler_calls[-1][1] == {"value": "replacement"}
    assert len(setup.planner_calls) == 2


def test_mutable_frozenset_subclass_is_not_a_confirmation_command(setup, harness):
    class MutableSet(frozenset):
        def __iter__(self):
            return iter(self.values)
    steps = MutableSet({2})
    steps.values = [2]
    worker = harness.worker(setup.engine.run)
    request_id = submit(worker, setup)
    worker.result(request_id, timeout=LIMIT)
    with pytest.raises(TypeError, match="confirmed_steps"):
        worker.confirm(request_id, confirmed_steps=steps)
    steps.values[:] = [2, 3]
    assert setup.handler_calls == []
    worker.confirm(request_id, confirmed_steps=frozenset({3}))
    assert worker.result(request_id, timeout=LIMIT)["pending_confirmation_steps"] == [2]
