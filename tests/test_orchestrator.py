from uuid import uuid4

import pytest

from core.contracts import (
    ActionRequest,
    ActionStatus,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.executor import (
    ExecutionReport,
)
from core.orchestrator import (
    Orchestrator,
)


# ============================================================
# FAKES
# ============================================================

class FakePlanner:

    def __init__(
        self,
        plan=None,
        error=None,
    ):

        self.plan_result = plan

        self.error = error

        self.calls = []


    def plan(
        self,
        request,
    ):

        self.calls.append(
            request
        )

        if self.error is not None:

            raise self.error

        return self.plan_result


class FakeExecutor:

    def __init__(
        self,
        report=None,
        error=None,
    ):

        self.report = report

        self.error = error

        self.calls = []


    def execute(
        self,
        plan,
        *,
        confirmed_steps=frozenset(),
    ):

        self.calls.append(
            (
                plan,
                confirmed_steps,
            )
        )

        if self.error is not None:

            raise self.error

        return self.report


# ============================================================
# HELPERS
# ============================================================

def make_request():

    return ActionRequest(
        goal="Search something",
        raw_input="Busca algo.",
    )


def make_plan(
    request_id,
):

    return ExecutionPlan(
        request_id=request_id,

        steps=(
            ExecutionStep(
                step_number=1,
                description="Search",
                capability="web_search",
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
        ),

        overall_risk=RiskLevel.LOW,
    )


def make_report(
    request_id,
    status=ActionStatus.COMPLETED,
):

    return ExecutionReport(
        request_id=request_id,
        status=status,
        message="test",
    )


# ============================================================
# TESTS
# ============================================================

def test_orchestrator_plans_and_executes():

    request = make_request()

    plan = make_plan(
        request.request_id
    )

    report = make_report(
        request.request_id
    )

    planner = FakePlanner(
        plan=plan
    )

    executor = FakeExecutor(
        report=report
    )

    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )


    result = orchestrator.run(
        request
    )


    assert (
        result.status
        == ActionStatus.COMPLETED
    )

    assert result.completed is True

    assert result.plan is plan

    assert (
        result.execution_report
        is report
    )

    assert planner.calls == [
        request
    ]

    assert executor.calls == [
        (
            plan,
            frozenset(),
        )
    ]


def test_waiting_for_permission_is_preserved():

    request = make_request()

    plan = make_plan(
        request.request_id
    )

    report = make_report(
        request.request_id,
        status=(
            ActionStatus
            .WAITING_FOR_PERMISSION
        ),
    )

    orchestrator = Orchestrator(
        planner=FakePlanner(
            plan=plan
        ),
        executor=FakeExecutor(
            report=report
        ),
    )


    result = orchestrator.run(
        request
    )


    assert (
        result.status
        == ActionStatus.WAITING_FOR_PERMISSION
    )

    assert (
        result.waiting_for_permission
        is True
    )


def test_blocked_status_is_preserved():

    request = make_request()

    plan = make_plan(
        request.request_id
    )

    report = make_report(
        request.request_id,
        status=ActionStatus.BLOCKED,
    )


    result = Orchestrator(
        planner=FakePlanner(
            plan=plan
        ),
        executor=FakeExecutor(
            report=report
        ),
    ).run(
        request
    )


    assert (
        result.status
        == ActionStatus.BLOCKED
    )


def test_planning_failure_prevents_execution():

    request = make_request()

    planner = FakePlanner(
        error=RuntimeError(
            "planner exploded"
        )
    )

    executor = FakeExecutor()


    result = Orchestrator(
        planner=planner,
        executor=executor,
    ).run(
        request
    )


    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert result.plan is None

    assert (
        result.execution_report
        is None
    )

    assert executor.calls == []

    assert (
        "planner exploded"
        in result.error
    )


def test_plan_request_id_must_match_request():

    request = make_request()

    wrong_plan = make_plan(
        uuid4()
    )

    executor = FakeExecutor()


    result = Orchestrator(
        planner=FakePlanner(
            plan=wrong_plan
        ),
        executor=executor,
    ).run(
        request
    )


    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert executor.calls == []

    assert (
        "different request_id"
        in result.error
    )


def test_run_rejects_confirmations_before_planning():
    request = make_request()
    planner = FakePlanner(plan=make_plan(request.request_id))
    executor = FakeExecutor(report=make_report(request.request_id))
    orchestrator = Orchestrator(planner=planner, executor=executor)

    with pytest.raises(TypeError, match="confirmed_steps"):
        orchestrator.run(request, confirmed_steps=frozenset({1}))

    assert planner.calls == []
    assert executor.calls == []


def test_executor_failure_is_preserved():

    request = make_request()

    plan = make_plan(
        request.request_id
    )

    report = make_report(
        request.request_id,
        status=ActionStatus.FAILED,
    )


    result = Orchestrator(
        planner=FakePlanner(
            plan=plan
        ),
        executor=FakeExecutor(
            report=report
        ),
    ).run(
        request
    )


    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert (
        result.execution_report
        is report
    )


def test_unexpected_executor_exception_is_captured():

    request = make_request()

    plan = make_plan(
        request.request_id
    )


    result = Orchestrator(
        planner=FakePlanner(
            plan=plan
        ),
        executor=FakeExecutor(
            error=RuntimeError(
                "executor exploded"
            )
        ),
    ).run(
        request
    )


    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert result.plan is plan

    assert (
        result.execution_report
        is None
    )

    assert (
        "executor exploded"
        in result.error
    )
