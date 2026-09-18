"""
Deterministic execution engine for Jarvis.

The Executor is responsible for running an already-created ExecutionPlan.

Safety boundaries:

- The plan is validated before execution.
- Permissions are evaluated before execution.
- Required confirmations must already be supplied.
- Forbidden steps block the entire plan.
- Every runtime binding is checked before the first handler runs.
- Step-output dependencies are resolved deterministically.
- Execution stops on the first failure.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from core.contracts import (
    ActionStatus,
    ArgumentSource,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
)
from core.permissions import (
    PermissionEngine,
    PermissionReport,
)
from core.plan_validator import (
    PlanValidationReport,
    PlanValidator,
)
from core.runtime import (
    CapabilityRuntimeRegistry,
)


# ============================================================
# ERRORS
# ============================================================

class ExecutorError(Exception):
    """
    Base error for execution failures.
    """


class DependencyResolutionError(
    ExecutorError
):
    """
    Raised when an argument cannot be resolved.
    """


class InvalidCapabilityResultError(
    ExecutorError
):
    """
    Raised when a runtime handler returns an invalid result.
    """


# ============================================================
# STEP RESULT
# ============================================================

@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    """
    Outcome of one executed step.
    """

    step_number: int

    capability: str

    status: ActionStatus

    data: Mapping[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    error: str | None = None


# ============================================================
# EXECUTION REPORT
# ============================================================

@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """
    Outcome of attempting to execute a complete plan.
    """

    request_id: UUID

    status: ActionStatus

    step_results: tuple[
        StepExecutionResult,
        ...
    ] = ()

    message: str = ""

    validation_report: (
        PlanValidationReport
        | None
    ) = None

    permission_report: (
        PermissionReport
        | None
    ) = None


    @property
    def completed(
        self,
    ) -> bool:

        return (
            self.status
            == ActionStatus.COMPLETED
        )


# ============================================================
# EXECUTOR
# ============================================================

class Executor:
    """
    Execute validated Jarvis plans using registered runtimes.
    """

    def __init__(
        self,
        *,
        validator: PlanValidator,
        permission_engine: PermissionEngine,
        runtime_registry: CapabilityRuntimeRegistry,
    ) -> None:

        self._validator = validator

        self._permission_engine = (
            permission_engine
        )

        self._runtime_registry = (
            runtime_registry
        )


    # --------------------------------------------------------
    # ARGUMENT RESOLUTION
    # --------------------------------------------------------

    def _resolve_argument(
        self,
        *,
        current_step: ExecutionStep,
        argument_name: str,
        argument: ExecutionArgument,
        outputs: Mapping[
            int,
            Mapping[str, Any],
        ],
    ) -> Any:

        # ----------------------------------------------------
        # LITERAL
        # ----------------------------------------------------

        if (
            argument.source
            == ArgumentSource.LITERAL
        ):

            if argument.value is None:

                raise DependencyResolutionError(
                    f"Step {current_step.step_number} "
                    f"argument {argument_name!r} "
                    "has no literal value."
                )

            return argument.value


        # ----------------------------------------------------
        # STEP OUTPUT
        # ----------------------------------------------------

        if (
            argument.source
            == ArgumentSource.STEP_OUTPUT
        ):

            source_step = (
                argument.step_number
            )

            output_key = (
                argument.output_key
            )


            if (
                source_step is None
                or output_key is None
            ):

                raise DependencyResolutionError(
                    f"Step {current_step.step_number} "
                    f"argument {argument_name!r} "
                    "contains an incomplete dependency."
                )


            if source_step not in outputs:

                raise DependencyResolutionError(
                    f"Step {current_step.step_number} "
                    f"depends on step {source_step}, "
                    "but that step produced no available output."
                )


            source_output = (
                outputs[
                    source_step
                ]
            )


            if (
                output_key
                not in source_output
            ):

                raise DependencyResolutionError(
                    f"Step {current_step.step_number} "
                    f"requires output {output_key!r} "
                    f"from step {source_step}, "
                    "but it was not produced."
                )


            return source_output[
                output_key
            ]


        raise DependencyResolutionError(
            f"Unsupported argument source "
            f"for step {current_step.step_number}: "
            f"{argument.source}"
        )


    def _resolve_arguments(
        self,
        *,
        step: ExecutionStep,
        outputs: Mapping[
            int,
            Mapping[str, Any],
        ],
    ) -> dict[
        str,
        Any,
    ]:

        return {
            name: self._resolve_argument(
                current_step=step,
                argument_name=name,
                argument=argument,
                outputs=outputs,
            )
            for name, argument
            in step.arguments.items()
        }


    # --------------------------------------------------------
    # RESULT VALIDATION
    # --------------------------------------------------------

    def _validate_handler_result(
        self,
        *,
        step: ExecutionStep,
        result: object,
    ) -> dict[
        str,
        Any,
    ]:

        if not isinstance(
            result,
            Mapping,
        ):

            raise InvalidCapabilityResultError(
                f"Runtime for capability "
                f"{step.capability!r} "
                "must return a mapping."
            )


        normalized = dict(
            result
        )


        for key in normalized:

            if not isinstance(
                key,
                str,
            ):

                raise InvalidCapabilityResultError(
                    f"Runtime for capability "
                    f"{step.capability!r} "
                    "returned a non-string output key."
                )


        return normalized


    # --------------------------------------------------------
    # EXECUTION
    # --------------------------------------------------------

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        confirmed_steps: frozenset[int] = frozenset(),
    ) -> ExecutionReport:
        """
        Execute one complete plan.

        confirmed_steps contains step numbers whose requested
        confirmation has already been granted by a higher-level
        interaction layer.
        """

        # ====================================================
        # PREFLIGHT 1 — PLAN VALIDATION
        # ====================================================

        validation_report = (
            self._validator.validate(
                plan
            )
        )


        if not validation_report.executable:

            return ExecutionReport(
                request_id=plan.request_id,
                status=ActionStatus.BLOCKED,
                message=(
                    "Execution plan failed validation."
                ),
                validation_report=(
                    validation_report
                ),
            )


        # ====================================================
        # PREFLIGHT 2 — PERMISSIONS
        # ====================================================

        permission_report = (
            self._permission_engine.evaluate(
                plan
            )
        )


        if (
            permission_report
            .has_forbidden_steps
        ):

            forbidden_numbers = tuple(
                decision.step_number
                for decision
                in permission_report.forbidden_steps
            )

            return ExecutionReport(
                request_id=plan.request_id,
                status=ActionStatus.BLOCKED,
                message=(
                    "Execution plan contains "
                    "forbidden steps: "
                    f"{forbidden_numbers}"
                ),
                validation_report=(
                    validation_report
                ),
                permission_report=(
                    permission_report
                ),
            )


        missing_confirmations = tuple(
            decision.step_number
            for decision
            in permission_report.confirmation_steps
            if (
                decision.step_number
                not in confirmed_steps
            )
        )


        if missing_confirmations:

            return ExecutionReport(
                request_id=plan.request_id,
                status=(
                    ActionStatus
                    .WAITING_FOR_PERMISSION
                ),
                message=(
                    "Execution requires confirmation "
                    "for steps: "
                    f"{missing_confirmations}"
                ),
                validation_report=(
                    validation_report
                ),
                permission_report=(
                    permission_report
                ),
            )


        # ====================================================
        # PREFLIGHT 3 — RUNTIME AVAILABILITY
        # ====================================================

        missing_runtimes = tuple(
            step.capability
            for step in plan.steps
            if not self._runtime_registry.has(
                step.capability
            )
        )


        if missing_runtimes:

            return ExecutionReport(
                request_id=plan.request_id,
                status=ActionStatus.BLOCKED,
                message=(
                    "Missing runtime implementations: "
                    f"{missing_runtimes}"
                ),
                validation_report=(
                    validation_report
                ),
                permission_report=(
                    permission_report
                ),
            )


        # ====================================================
        # EXECUTION
        # ====================================================

        outputs: dict[
            int,
            Mapping[str, Any],
        ] = {}

        step_results: list[
            StepExecutionResult
        ] = []


        for step in plan.steps:

            # ------------------------------------------------
            # RESOLVE INPUTS
            # ------------------------------------------------

            try:

                resolved_arguments = (
                    self._resolve_arguments(
                        step=step,
                        outputs=outputs,
                    )
                )

            except ExecutorError as error:

                step_results.append(
                    StepExecutionResult(
                        step_number=(
                            step.step_number
                        ),
                        capability=(
                            step.capability
                        ),
                        status=(
                            ActionStatus.FAILED
                        ),
                        error=str(
                            error
                        ),
                    )
                )

                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    step_results=tuple(
                        step_results
                    ),
                    message=(
                        "Execution failed while "
                        "resolving dependencies."
                    ),
                    validation_report=(
                        validation_report
                    ),
                    permission_report=(
                        permission_report
                    ),
                )


            # ------------------------------------------------
            # GET HANDLER
            # ------------------------------------------------

            handler = (
                self._runtime_registry.get(
                    step.capability
                )
            )


            # ------------------------------------------------
            # EXECUTE HANDLER
            # ------------------------------------------------

            try:

                raw_result = handler(
                    resolved_arguments
                )

                result = (
                    self._validate_handler_result(
                        step=step,
                        result=raw_result,
                    )
                )

            except Exception as error:

                step_results.append(
                    StepExecutionResult(
                        step_number=(
                            step.step_number
                        ),
                        capability=(
                            step.capability
                        ),
                        status=(
                            ActionStatus.FAILED
                        ),
                        error=(
                            f"{type(error).__name__}: "
                            f"{error}"
                        ),
                    )
                )

                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    step_results=tuple(
                        step_results
                    ),
                    message=(
                        f"Execution failed at "
                        f"step {step.step_number}."
                    ),
                    validation_report=(
                        validation_report
                    ),
                    permission_report=(
                        permission_report
                    ),
                )


            # ------------------------------------------------
            # STORE OUTPUT
            # ------------------------------------------------

            outputs[
                step.step_number
            ] = result


            step_results.append(
                StepExecutionResult(
                    step_number=(
                        step.step_number
                    ),
                    capability=(
                        step.capability
                    ),
                    status=(
                        ActionStatus.COMPLETED
                    ),
                    data=result,
                )
            )


        # ====================================================
        # COMPLETED
        # ====================================================

        return ExecutionReport(
            request_id=plan.request_id,
            status=ActionStatus.COMPLETED,
            step_results=tuple(
                step_results
            ),
            message=(
                "Execution plan completed successfully."
            ),
            validation_report=(
                validation_report
            ),
            permission_report=(
                permission_report
            ),
        )
