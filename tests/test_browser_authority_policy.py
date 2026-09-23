"""Authority policy tests for the browser capability.

Task 15.5B2

The Planner may propose browser, but that proposal is not authority.

Browser has a deterministic local permission floor:

    CONFIRM_BEFORE_EXECUTION

Therefore a browser side effect cannot occur until a trusted local
confirmation is supplied to Executor.
"""

from __future__ import annotations

from core.contracts import (
    ActionStatus,
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


# ============================================================
# HELPERS
# ============================================================

def make_browser_plan():
    return ExecutionPlan(
        request_id=__import__(
            "uuid"
        ).uuid4(),
        steps=(
            ExecutionStep(
                step_number=1,
                description=(
                    "Open the requested website"
                ),
                capability="browser",
                arguments={
                    "url":
                        ExecutionArgument.literal(
                            "https://example.com"
                        )
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
        ),
        overall_risk=RiskLevel.LOW,
    )


def make_executor(
    browser_handler,
):
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="browser",
            description="Open websites.",
        )
    )

    runtimes = (
        CapabilityRuntimeRegistry()
    )

    runtimes.register(
        "browser",
        browser_handler,
    )

    return Executor(
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


# ============================================================
# TESTS
# ============================================================

def test_browser_has_local_confirmation_floor():
    engine = PermissionEngine()

    decision = (
        engine
        .evaluate(
            make_browser_plan()
        )
        .decisions[0]
    )

    assert (
        decision.planner_permission
        == PermissionMode.AUTOMATIC
    )

    assert (
        decision.effective_permission
        == PermissionMode.CONFIRM_BEFORE_EXECUTION
    )

    assert (
        decision.requires_confirmation
        is True
    )


def test_browser_does_not_execute_without_confirmation():
    calls = []

    def browser_handler(
        arguments,
    ):
        calls.append(
            dict(arguments)
        )

        return {
            "opened": True,
            "opened_url": (
                arguments["url"]
            ),
        }

    executor = make_executor(
        browser_handler
    )

    report = executor.execute(
        make_browser_plan()
    )

    assert (
        report.status
        == ActionStatus.WAITING_FOR_PERMISSION
    )

    assert calls == []


def test_browser_executes_after_trusted_confirmation():
    calls = []

    def browser_handler(
        arguments,
    ):
        calls.append(
            dict(arguments)
        )

        return {
            "opened": True,
            "opened_url": (
                arguments["url"]
            ),
        }

    executor = make_executor(
        browser_handler
    )

    report = executor.execute(
        make_browser_plan(),
        confirmed_steps=frozenset(
            {
                1,
            }
        ),
    )

    assert (
        report.status
        == ActionStatus.COMPLETED
    )

    assert calls == [
        {
            "url":
                "https://example.com",
        }
    ]
