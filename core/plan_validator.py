"""
Execution plan validation for Jarvis.

The validator determines whether an ExecutionPlan is structurally
valid and whether all required capabilities are currently available.

It also validates dependencies between execution steps.

It performs no external actions.
"""

from dataclasses import dataclass
from enum import Enum

from core.contracts import (
    ArgumentSource,
    CapabilityRequirement,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
)
from core.resolver import (
    CapabilityResolutionReport,
    CapabilityResolver,
)


# ============================================================
# STATUS
# ============================================================

class PlanValidationStatus(
    str,
    Enum,
):
    """
    Final validation state of an execution plan.
    """

    EXECUTABLE = "executable"
    BLOCKED = "blocked"
    INVALID = "invalid"


# ============================================================
# REPORT
# ============================================================

@dataclass(frozen=True, slots=True)
class PlanValidationReport:
    """
    Result of validating one ExecutionPlan.
    """

    status: PlanValidationStatus

    plan: ExecutionPlan

    capability_report: (
        CapabilityResolutionReport
        | None
    )

    errors: tuple[
        str,
        ...
    ] = ()


    @property
    def executable(
        self,
    ) -> bool:

        return (
            self.status
            == PlanValidationStatus.EXECUTABLE
        )


    @property
    def missing_capabilities(
        self,
    ) -> tuple[
        str,
        ...
    ]:

        if self.capability_report is None:
            return ()

        return tuple(
            requirement.capability
            for requirement
            in (
                self
                .capability_report
                .missing_required
            )
        )


# ============================================================
# ARGUMENT VALIDATION
# ============================================================

def _validate_argument(
    *,
    current_step_number: int,
    argument_name: str,
    argument: ExecutionArgument,
) -> tuple[
    str,
    ...
]:

    errors: list[
        str
    ] = []


    if not argument_name.strip():

        errors.append(
            f"Step {current_step_number} "
            "contains an empty argument name."
        )

        return tuple(
            errors
        )


    if not isinstance(
        argument,
        ExecutionArgument,
    ):

        errors.append(
            f"Step {current_step_number} "
            f"argument {argument_name!r} "
            "is not an ExecutionArgument."
        )

        return tuple(
            errors
        )


    if (
        argument.source
        == ArgumentSource.STEP_OUTPUT
    ):

        referenced_step = (
            argument.step_number
        )

        if referenced_step is None:

            errors.append(
                f"Step {current_step_number} "
                f"argument {argument_name!r} "
                "has no dependency step."
            )

            return tuple(
                errors
            )


        if (
            referenced_step
            >= current_step_number
        ):

            errors.append(
                f"Step {current_step_number} "
                f"argument {argument_name!r} "
                "must reference an earlier step."
            )


    return tuple(
        errors
    )


# ============================================================
# STRUCTURAL VALIDATION
# ============================================================

def _validate_steps(
    steps: tuple[
        ExecutionStep,
        ...
    ],
) -> tuple[
    str,
    ...
]:

    errors: list[
        str
    ] = []


    # --------------------------------------------------------
    # PLAN MUST HAVE STEPS
    # --------------------------------------------------------

    if not steps:

        errors.append(
            "Execution plan must contain "
            "at least one step."
        )

        return tuple(
            errors
        )


    # --------------------------------------------------------
    # CONTIGUOUS STEP NUMBERS
    # --------------------------------------------------------

    expected_numbers = tuple(
        range(
            1,
            len(steps) + 1,
        )
    )

    actual_numbers = tuple(
        step.step_number
        for step in steps
    )


    if actual_numbers != expected_numbers:

        errors.append(
            "Execution step numbers must "
            "start at 1 and be contiguous."
        )


    # --------------------------------------------------------
    # STEP CONTENT
    # --------------------------------------------------------

    for step in steps:

        if not step.description.strip():

            errors.append(
                f"Step {step.step_number} "
                "has an empty description."
            )


        if not step.capability.strip():

            errors.append(
                f"Step {step.step_number} "
                "has an empty capability."
            )


        for (
            argument_name,
            argument,
        ) in step.arguments.items():

            errors.extend(
                _validate_argument(
                    current_step_number=(
                        step.step_number
                    ),
                    argument_name=(
                        argument_name
                    ),
                    argument=argument,
                )
            )


    return tuple(
        errors
    )


# ============================================================
# PLAN VALIDATOR
# ============================================================

class PlanValidator:
    """
    Validate execution plans against Jarvis capabilities
    and dependency rules.
    """

    def __init__(
        self,
        resolver: CapabilityResolver,
    ) -> None:

        self._resolver = resolver


    def validate(
        self,
        plan: ExecutionPlan,
    ) -> PlanValidationReport:

        structural_errors = (
            _validate_steps(
                plan.steps
            )
        )


        # ----------------------------------------------------
        # INVALID STRUCTURE
        # ----------------------------------------------------

        if structural_errors:

            return PlanValidationReport(
                status=(
                    PlanValidationStatus.INVALID
                ),
                plan=plan,
                capability_report=None,
                errors=structural_errors,
            )


        # ----------------------------------------------------
        # CAPABILITY REQUIREMENTS
        # ----------------------------------------------------

        requirements = tuple(
            CapabilityRequirement(
                capability=(
                    step.capability
                ),
                reason=(
                    "Required by execution step "
                    f"{step.step_number}: "
                    f"{step.description}"
                ),
                required=True,
            )
            for step in plan.steps
        )


        capability_report = (
            self._resolver.resolve_all(
                requirements
            )
        )


        # ----------------------------------------------------
        # BLOCKED
        # ----------------------------------------------------

        if (
            not
            capability_report
            .all_required_available
        ):

            return PlanValidationReport(
                status=(
                    PlanValidationStatus.BLOCKED
                ),
                plan=plan,
                capability_report=(
                    capability_report
                ),
            )


        # ----------------------------------------------------
        # EXECUTABLE
        # ----------------------------------------------------

        return PlanValidationReport(
            status=(
                PlanValidationStatus.EXECUTABLE
            ),
            plan=plan,
            capability_report=(
                capability_report
            ),
        )
