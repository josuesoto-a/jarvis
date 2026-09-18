import pytest
from uuid import uuid4

from core.contracts import (
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
    CapabilitySpec,
)
from core.plan_validator import (
    PlanValidationStatus,
    PlanValidator,
)
from core.registry import (
    CapabilityRegistry,
)
from core.resolver import (
    CapabilityResolver,
)


def build_validator() -> PlanValidator:

    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="web_search",
            description="Search the web",
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description="Open websites",
        )
    )

    return PlanValidator(
        CapabilityResolver(
            registry
        )
    )


def test_previous_step_output_reference_is_valid():

    plan = ExecutionPlan(
        request_id=uuid4(),

        steps=(
            ExecutionStep(
                step_number=1,
                description="Search",
                capability="web_search",
                arguments={
                    "query":
                        ExecutionArgument.literal(
                            "Python"
                        ),
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),

            ExecutionStep(
                step_number=2,
                description="Open result",
                capability="browser",
                arguments={
                    "url":
                        ExecutionArgument.step_output(
                            step_number=1,
                            output_key=(
                                "best_result_url"
                            ),
                        ),
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
        ),

        overall_risk=RiskLevel.LOW,
    )

    report = (
        build_validator()
        .validate(
            plan
        )
    )

    assert (
        report.status
        == PlanValidationStatus.EXECUTABLE
    )


def test_step_cannot_depend_on_itself():

    plan = ExecutionPlan(
        request_id=uuid4(),

        steps=(
            ExecutionStep(
                step_number=1,
                description="Search",
                capability="web_search",
                arguments={
                    "query":
                        ExecutionArgument.step_output(
                            step_number=1,
                            output_key="query",
                        ),
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
        ),

        overall_risk=RiskLevel.LOW,
    )

    report = (
        build_validator()
        .validate(
            plan
        )
    )

    assert (
        report.status
        == PlanValidationStatus.INVALID
    )

    assert (
        "must reference an earlier step"
        in report.errors[
            0
        ]
    )


def test_step_cannot_depend_on_future_step():

    plan = ExecutionPlan(
        request_id=uuid4(),

        steps=(
            ExecutionStep(
                step_number=1,
                description="Open result",
                capability="browser",
                arguments={
                    "url":
                        ExecutionArgument.step_output(
                            step_number=2,
                            output_key=(
                                "best_result_url"
                            ),
                        ),
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),

            ExecutionStep(
                step_number=2,
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

    report = (
        build_validator()
        .validate(
            plan
        )
    )

    assert (
        report.status
        == PlanValidationStatus.INVALID
    )


def test_step_output_requires_positive_step_number():

    with pytest.raises(
        ValueError
    ):

        ExecutionArgument.step_output(
            step_number=0,
            output_key="result",
        )
