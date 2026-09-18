from uuid import uuid4

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

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def make_plan(
    *steps: ExecutionStep,
) -> ExecutionPlan:

    return ExecutionPlan(
        request_id=uuid4(),
        steps=tuple(
            steps
        ),
        overall_risk=max(
            (
                step.risk
                for step in steps
            ),
            key=lambda risk:
                _RISK_ORDER[risk],
        ),
    )


def make_step(
    number: int,
    capability: str,
    *,
    arguments=None,
    risk: RiskLevel = RiskLevel.LOW,
    permission: PermissionMode = (
        PermissionMode.AUTOMATIC
    ),
) -> ExecutionStep:

    return ExecutionStep(
        step_number=number,
        description=(
            f"Execute {capability}"
        ),
        capability=capability,
        arguments=(
            arguments
            if arguments is not None
            else {}
        ),
        risk=risk,
        permission=permission,
    )


def build_registry() -> CapabilityRegistry:

    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="web_search",
            description="Search the web.",
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description="Open websites.",
        )
    )

    registry.register(
        CapabilitySpec(
            name="terminal",
            description="Run terminal commands.",
        )
    )

    return registry


def build_executor(
    runtime: CapabilityRuntimeRegistry,
) -> Executor:

    registry = build_registry()

    validator = PlanValidator(
        CapabilityResolver(
            registry
        )
    )

    return Executor(
        validator=validator,
        permission_engine=(
            PermissionEngine()
        ),
        runtime_registry=runtime,
    )


# ============================================================
# TESTS
# ============================================================

def test_executor_runs_single_automatic_step():

    calls = []

    def search_handler(arguments):

        calls.append(
            dict(arguments)
        )

        return {
            "best_result_url":
                "https://example.com",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
            arguments={
                "query":
                    ExecutionArgument.literal(
                        "Big Mac price"
                    )
            },
        )
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.COMPLETED
    )

    assert report.completed is True

    assert calls == [
        {
            "query": "Big Mac price",
        }
    ]


def test_executor_resolves_previous_step_output():

    received_browser_arguments = []


    def search_handler(arguments):

        return {
            "best_result_url":
                "https://example.com/big-mac",
        }


    def browser_handler(arguments):

        received_browser_arguments.append(
            dict(arguments)
        )

        return {
            "opened_url":
                arguments["url"],
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )

    runtime.register(
        "browser",
        browser_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
            arguments={
                "query":
                    ExecutionArgument.literal(
                        "Big Mac price"
                    )
            },
        ),

        make_step(
            2,
            "browser",
            arguments={
                "url":
                    ExecutionArgument.step_output(
                        step_number=1,
                        output_key=(
                            "best_result_url"
                        ),
                    )
            },
        ),
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.COMPLETED
    )

    assert (
        received_browser_arguments
        == [
            {
                "url":
                    "https://example.com/big-mac"
            }
        ]
    )


def test_missing_runtime_blocks_before_any_execution():

    calls = []


    def search_handler(arguments):

        calls.append(
            dict(arguments)
        )

        return {
            "best_result_url":
                "https://example.com",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
        ),

        make_step(
            2,
            "browser",
        ),
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.BLOCKED
    )

    assert calls == []

    assert (
        "browser"
        in report.message
    )


def test_confirmation_blocks_before_any_execution():

    calls = []


    def terminal_handler(arguments):

        calls.append(
            dict(arguments)
        )

        return {
            "stdout": "ok",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "terminal",
        terminal_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "terminal",
            arguments={
                "command":
                    ExecutionArgument.literal(
                        "echo hello"
                    )
            },
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.WAITING_FOR_PERMISSION
    )

    assert calls == []


def test_confirmed_step_can_execute():

    calls = []


    def terminal_handler(arguments):

        calls.append(
            dict(arguments)
        )

        return {
            "stdout": "hello",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "terminal",
        terminal_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "terminal",
            arguments={
                "command":
                    ExecutionArgument.literal(
                        "echo hello"
                    )
            },
            risk=RiskLevel.LOW,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )


    report = executor.execute(
        plan,
        confirmed_steps=frozenset(
            {
                1,
            }
        ),
    )


    assert report.status == (
        ActionStatus.COMPLETED
    )

    assert calls == [
        {
            "command":
                "echo hello"
        }
    ]


def test_forbidden_step_blocks_before_execution():

    calls = []


    def search_handler(arguments):

        calls.append(
            dict(arguments)
        )

        return {
            "result": "never reached",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
            risk=RiskLevel.CRITICAL,
            permission=(
                PermissionMode.AUTOMATIC
            ),
        )
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.BLOCKED
    )

    assert calls == []


def test_missing_dependency_output_fails_execution():

    browser_calls = []


    def search_handler(arguments):

        return {
            "title": "Big Mac",
        }


    def browser_handler(arguments):

        browser_calls.append(
            dict(arguments)
        )

        return {
            "opened": True,
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )

    runtime.register(
        "browser",
        browser_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
        ),

        make_step(
            2,
            "browser",
            arguments={
                "url":
                    ExecutionArgument.step_output(
                        step_number=1,
                        output_key=(
                            "best_result_url"
                        ),
                    )
            },
        ),
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.FAILED
    )

    assert browser_calls == []

    assert len(
        report.step_results
    ) == 2

    assert (
        report.step_results[
            0
        ].status
        == ActionStatus.COMPLETED
    )

    assert (
        report.step_results[
            1
        ].status
        == ActionStatus.FAILED
    )


def test_handler_exception_stops_execution():

    browser_calls = []


    def search_handler(arguments):

        raise RuntimeError(
            "network exploded"
        )


    def browser_handler(arguments):

        browser_calls.append(
            dict(arguments)
        )

        return {
            "opened": True,
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )

    runtime.register(
        "browser",
        browser_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
        ),

        make_step(
            2,
            "browser",
        ),
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.FAILED
    )

    assert browser_calls == []

    assert len(
        report.step_results
    ) == 1

    assert (
        "RuntimeError"
        in report.step_results[
            0
        ].error
    )


def test_non_mapping_handler_result_fails():

    def search_handler(arguments):

        return "invalid result"


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
        )
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.FAILED
    )

    assert (
        "InvalidCapabilityResultError"
        in report.step_results[
            0
        ].error
    )


def test_completed_report_preserves_step_outputs():

    def search_handler(arguments):

        return {
            "best_result_url":
                "https://example.com",
            "title":
                "Example",
        }


    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        search_handler,
    )


    executor = build_executor(
        runtime
    )


    plan = make_plan(
        make_step(
            1,
            "web_search",
        )
    )


    report = executor.execute(
        plan
    )


    assert report.status == (
        ActionStatus.COMPLETED
    )

    assert (
        report.step_results[
            0
        ].data[
            "best_result_url"
        ]
        == "https://example.com"
    )

    assert (
        report.step_results[
            0
        ].data[
            "title"
        ]
        == "Example"
    )
