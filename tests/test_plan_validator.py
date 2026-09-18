from uuid import uuid4

from core.contracts import (
    CapabilitySpec,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.plan_validator import (
    PlanValidationStatus,
    PlanValidator,
)
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver


def build_validator() -> PlanValidator:
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="weather",
            description="Get current weather",
        )
    )

    registry.register(
        CapabilitySpec(
            name="web_search",
            description="Search public information",
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description="Navigate web pages",
        )
    )

    resolver = CapabilityResolver(
        registry
    )

    return PlanValidator(
        resolver
    )


def make_step(
    number: int,
    capability: str,
    description: str,
) -> ExecutionStep:

    return ExecutionStep(
        step_number=number,
        description=description,
        capability=capability,
        risk=RiskLevel.LOW,
        permission=PermissionMode.AUTOMATIC,
    )


def test_valid_plan_is_executable():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(
            make_step(
                1,
                "weather",
                "Get current weather",
            ),
        ),
        overall_risk=RiskLevel.LOW,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.EXECUTABLE
    )

    assert report.executable is True
    assert report.missing_capabilities == ()
    assert report.errors == ()


def test_missing_capability_blocks_plan():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(
            make_step(
                1,
                "web_search",
                "Find restaurant",
            ),
            make_step(
                2,
                "computer_use",
                "Interact with ordering interface",
            ),
        ),
        overall_risk=RiskLevel.HIGH,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.BLOCKED
    )

    assert report.executable is False

    assert report.missing_capabilities == (
        "computer_use",
    )


def test_multiple_missing_capabilities_are_reported():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(
            make_step(
                1,
                "terminal",
                "Run local command",
            ),
            make_step(
                2,
                "codex",
                "Analyze repository",
            ),
        ),
        overall_risk=RiskLevel.MEDIUM,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.BLOCKED
    )

    assert report.missing_capabilities == (
        "terminal",
        "codex",
    )


def test_empty_plan_is_invalid():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(),
        overall_risk=RiskLevel.LOW,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.INVALID
    )

    assert report.executable is False

    assert report.capability_report is None

    assert report.errors == (
        "Execution plan must contain at least one step.",
    )


def test_non_contiguous_step_numbers_are_invalid():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(
            make_step(
                1,
                "web_search",
                "Search",
            ),
            make_step(
                3,
                "browser",
                "Open result",
            ),
        ),
        overall_risk=RiskLevel.LOW,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.INVALID
    )

    assert (
        "Execution step numbers must start at 1 "
        "and be contiguous."
        in report.errors
    )


def test_empty_step_description_is_invalid():
    validator = build_validator()

    plan = ExecutionPlan(
        request_id=uuid4(),
        steps=(
            make_step(
                1,
                "weather",
                "   ",
            ),
        ),
        overall_risk=RiskLevel.LOW,
    )

    report = validator.validate(
        plan
    )

    assert report.status == (
        PlanValidationStatus.INVALID
    )

    assert report.errors == (
        "Step 1 has an empty description.",
    )
