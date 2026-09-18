from uuid import uuid4

from core.contracts import (
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.permissions import (
    PermissionEngine,
)


def make_step(
    number: int,
    capability: str,
    risk: RiskLevel,
    permission: PermissionMode,
) -> ExecutionStep:

    return ExecutionStep(
        step_number=number,
        description="Test action",
        capability=capability,
        risk=risk,
        permission=permission,
    )


def make_plan(
    *steps: ExecutionStep,
) -> ExecutionPlan:

    return ExecutionPlan(
        request_id=uuid4(),
        steps=tuple(steps),
        overall_risk=max(
            (
                step.risk
                for step in steps
            ),
            key=lambda risk: {
                RiskLevel.LOW: 0,
                RiskLevel.MEDIUM: 1,
                RiskLevel.HIGH: 2,
                RiskLevel.CRITICAL: 3,
            }[risk],
        ),
    )


def test_low_risk_web_search_can_be_automatic():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "web_search",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        )
    )

    report = engine.evaluate(plan)

    decision = report.decisions[0]

    assert decision.automatic is True

    assert (
        decision.effective_permission
        == PermissionMode.AUTOMATIC
    )


def test_medium_risk_requires_confirmation():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "web_search",
            RiskLevel.MEDIUM,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True


def test_high_risk_requires_confirmation():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "browser",
            RiskLevel.HIGH,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True


def test_critical_risk_is_forbidden():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "computer_use",
            RiskLevel.CRITICAL,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.forbidden is True

    assert (
        decision.effective_permission
        == PermissionMode.FORBIDDEN
    )


def test_terminal_cannot_be_made_automatic_by_planner():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "terminal",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True

    assert (
        decision.planner_permission
        == PermissionMode.AUTOMATIC
    )


def test_filesystem_requires_confirmation():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "filesystem",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True


def test_planner_can_request_stricter_permission():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "weather",
            RiskLevel.LOW,
            PermissionMode.CONFIRM_BEFORE_EXECUTION,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True


def test_unknown_capability_defaults_to_confirmation():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "future_capability",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        )
    )

    decision = (
        engine
        .evaluate(plan)
        .decisions[0]
    )

    assert decision.requires_confirmation is True


def test_report_detects_confirmation_steps():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "web_search",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        ),
        make_step(
            2,
            "computer_use",
            RiskLevel.MEDIUM,
            PermissionMode.AUTOMATIC,
        ),
    )

    report = engine.evaluate(plan)

    assert report.all_automatic is False
    assert report.requires_confirmation is True

    assert tuple(
        decision.step_number
        for decision
        in report.confirmation_steps
    ) == (
        2,
    )


def test_report_detects_forbidden_steps():
    engine = PermissionEngine()

    plan = make_plan(
        make_step(
            1,
            "web_search",
            RiskLevel.LOW,
            PermissionMode.AUTOMATIC,
        ),
        make_step(
            2,
            "terminal",
            RiskLevel.CRITICAL,
            PermissionMode.AUTOMATIC,
        ),
    )

    report = engine.evaluate(plan)

    assert report.has_forbidden_steps is True

    assert tuple(
        decision.step_number
        for decision
        in report.forbidden_steps
    ) == (
        2,
    )
