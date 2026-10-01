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
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from capabilities.browser import (
    BrowserCapabilityError,
    prepare_browser_approval,
    validate_browser_approval_target,
)
from capabilities.terminal import (
    TerminalContractError,
    build_terminal_approval_target,
    prepare_terminal_execution,
    terminal_request_from_plan,
    validate_terminal_approval_target,
    validate_terminal_target_for_request,
)
from core.approval import PendingApprovalTarget
from core.contracts import (
    ActionStatus,
    ArgumentSource,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
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


class InvalidExecutionCheckpointError(
    ExecutorError
):
    """Raised when continuation state is inconsistent with its plan."""


# ============================================================
# STEP RESULT
# ============================================================

@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    """
    Outcome of one executed step.

    Handlers may return this existing result to preserve structured FAILED
    data. Executor validates step/capability and accepts only COMPLETED/FAILED.
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
# EXECUTION CHECKPOINT
# ============================================================

@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    """Internal continuation state. This is never permission."""

    next_step_number: int

    outputs: Mapping[
        int,
        Mapping[str, Any],
    ]

    step_results: tuple[
        StepExecutionResult,
        ...
    ]

    resolved_arguments: Mapping[
        str,
        Any,
    ]


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

    checkpoint: ExecutionCheckpoint | None = None

    pending_approval_target: PendingApprovalTarget | None = None

    pending_confirmation_steps: tuple[
        int,
        ...
    ] = ()


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


    def _run_handler(self, step: ExecutionStep, arguments: Mapping[str, Any]) -> StepExecutionResult:
        """Ordinary mappings succeed; explicit step results can fail with data."""
        raw = self._runtime_registry.get(step.capability)(arguments)
        if isinstance(raw, StepExecutionResult):
            if (type(raw.step_number) is not int
                    or raw.step_number != step.step_number
                    or raw.capability != step.capability
                    or raw.status not in {ActionStatus.COMPLETED, ActionStatus.FAILED}
                    or type(raw.status) is not ActionStatus
                    or (raw.error is not None and type(raw.error) is not str)):
                raise InvalidCapabilityResultError("Runtime returned an inconsistent step result.")
            return StepExecutionResult(
                step.step_number, step.capability, raw.status,
                self._validate_handler_result(step=step, result=raw.data), raw.error,
            )
        return StepExecutionResult(
            step.step_number, step.capability, ActionStatus.COMPLETED,
            self._validate_handler_result(step=step, result=raw),
        )

    def _prepared_runtime_arguments(
        self, step: ExecutionStep, target: PendingApprovalTarget,
    ) -> dict[str, Any]:
        """Explicit dispatch shapes for the two supported prepared capabilities."""
        if step.capability == "browser" and target.subject.capability == "browser":
            return {"url": target.prepared_target}
        if step.capability == "terminal" and target.subject.capability == "terminal":
            return {"target": target.prepared_target}
        raise ExecutorError("Unsupported prepared-target dispatch capability.")

    def _execute_terminal_approval(
        self, plan: ExecutionPlan, *, confirmed_steps: frozenset[int],
        checkpoint: ExecutionCheckpoint | None,
        pending_approval_target: PendingApprovalTarget | None,
        validation_report: PlanValidationReport, permission_report: PermissionReport,
    ) -> ExecutionReport:
        """Single literal terminal approval/continuation, with no launcher."""
        def report(status, message, **kwargs):
            return ExecutionReport(
                request_id=plan.request_id, status=status, message=message,
                validation_report=validation_report, permission_report=permission_report,
                **kwargs,
            )

        try:
            request = terminal_request_from_plan(plan)
            step = plan.steps[0]
            if checkpoint is not None:
                raise TerminalContractError("terminal v1 does not support checkpoints")
            decisions = permission_report.decisions
            if (len(decisions) != 1
                    or decisions[0].step_number != step.step_number
                    or decisions[0].capability != "terminal"
                    or decisions[0].effective_permission is not PermissionMode.CONFIRM_BEFORE_EXECUTION):
                raise TerminalContractError("terminal v1 requires confirmation permission")
            if confirmed_steps and confirmed_steps != frozenset({step.step_number}):
                raise TerminalContractError("terminal confirmation must identify exactly its step")
            if pending_approval_target is not None:
                if confirmed_steps != frozenset({step.step_number}):
                    raise TerminalContractError("terminal continuation has not been confirmed")
                validate_terminal_approval_target(
                    pending_approval_target, request_id=plan.request_id,
                    step_number=step.step_number, risk=step.risk,
                )
                validate_terminal_target_for_request(pending_approval_target.prepared_target, request)
            elif confirmed_steps:
                raise TerminalContractError("terminal continuation requires the stored target")
        except (TerminalContractError, TypeError, ValueError) as error:
            return report(ActionStatus.FAILED, f"Invalid terminal approval state: {error}")

        if not self._runtime_registry.has("terminal"):
            return report(ActionStatus.BLOCKED, "Missing runtime implementations: ('terminal',)")

        if pending_approval_target is None:
            try:
                prepared = prepare_terminal_execution(request)
                pending_approval_target = build_terminal_approval_target(
                    prepared, request_id=plan.request_id, step_number=step.step_number,
                    risk=step.risk, effective_permission=decisions[0].effective_permission,
                )
                validate_terminal_target_for_request(prepared, request)
            except (TerminalContractError, TypeError, ValueError, OSError) as error:
                return report(ActionStatus.FAILED, f"Terminal preparation failed: {error}")
            return report(
                ActionStatus.WAITING_FOR_PERMISSION,
                f"Execution requires confirmation for step: {step.step_number}",
                pending_confirmation_steps=(step.step_number,),
                pending_approval_target=pending_approval_target,
            )

        try:
            result = self._run_handler(
                step, self._prepared_runtime_arguments(step, pending_approval_target),
            )
        except Exception as error:
            result = StepExecutionResult(
                step.step_number, step.capability, ActionStatus.FAILED,
                error=f"{type(error).__name__}: {error}",
            )
        return report(result.status, f"Terminal recording step {result.status.value}.",
                      step_results=(result,))

    # --------------------------------------------------------
    # EXECUTION
    # --------------------------------------------------------

    def _validate_execution_checkpoint(
        self,
        *,
        plan: ExecutionPlan,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        """Verify that continuation state is a faithful plan prefix."""

        matching_steps = tuple(
            step
            for step in plan.steps
            if (
                step.step_number
                == checkpoint.next_step_number
            )
        )

        if len(matching_steps) != 1:
            raise InvalidExecutionCheckpointError(
                "Checkpoint next step is not present exactly once."
            )

        next_step = matching_steps[0]

        prefix_steps = tuple(
            step
            for step in plan.steps
            if (
                step.step_number
                < checkpoint.next_step_number
            )
        )

        expected_numbers = tuple(
            step.step_number
            for step in prefix_steps
        )

        actual_numbers = tuple(
            result.step_number
            for result
            in checkpoint.step_results
        )

        if actual_numbers != expected_numbers:
            raise InvalidExecutionCheckpointError(
                "Checkpoint completed steps do not match "
                "the exact plan prefix."
            )

        if (
            set(checkpoint.outputs)
            != set(expected_numbers)
        ):
            raise InvalidExecutionCheckpointError(
                "Checkpoint outputs do not match "
                "the completed plan prefix."
            )

        for (
            plan_step,
            result,
        ) in zip(
            prefix_steps,
            checkpoint.step_results,
            strict=True,
        ):

            if (
                result.status
                != ActionStatus.COMPLETED
            ):
                raise InvalidExecutionCheckpointError(
                    "Checkpoint contains a non-completed "
                    "prefix result."
                )

            if (
                result.capability
                != plan_step.capability
            ):
                raise InvalidExecutionCheckpointError(
                    "Checkpoint capability does not match "
                    "the plan prefix."
                )

            output = checkpoint.outputs[
                plan_step.step_number
            ]

            if not isinstance(
                output,
                Mapping,
            ):
                raise InvalidExecutionCheckpointError(
                    "Checkpoint output is not a mapping."
                )

            if (
                dict(output)
                != dict(result.data)
            ):
                raise InvalidExecutionCheckpointError(
                    "Checkpoint output does not match "
                    "its recorded step result."
                )

        try:
            recomputed_arguments = (
                self._resolve_arguments(
                    step=next_step,
                    outputs=checkpoint.outputs,
                )
            )

        except ExecutorError as error:
            raise InvalidExecutionCheckpointError(
                "Checkpoint cannot resolve the pending step."
            ) from error

        if (
            dict(checkpoint.resolved_arguments)
            != recomputed_arguments
        ):
            raise InvalidExecutionCheckpointError(
                "Checkpoint resolved arguments do not match "
                "the outputs recorded by its plan prefix."
            )


    def _execute_from_checkpoint_boundary(
        self,
        plan: ExecutionPlan,
        *,
        confirmed_steps: frozenset[int],
        validation_report: PlanValidationReport,
        permission_report: PermissionReport,
        checkpoint: ExecutionCheckpoint | None,
        pending_approval_target: PendingApprovalTarget | None,
    ) -> ExecutionReport:
        """
        Execute the narrow C2A flow.

        This path is entered only for one low-risk browser confirmation
        whose concrete arguments depend on earlier step output, or when
        resuming the checkpoint produced by that path.
        """

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
                validation_report=validation_report,
                permission_report=permission_report,
            )

        decisions = {
            decision.step_number: decision
            for decision in permission_report.decisions
        }

        if checkpoint is None:
            outputs: dict[
                int,
                Mapping[str, Any],
            ] = {}

            step_results: list[
                StepExecutionResult
            ] = []

            next_step = plan.steps[0].step_number
            checkpoint_arguments = None

        else:
            try:
                self._validate_execution_checkpoint(
                    plan=plan,
                    checkpoint=checkpoint,
                )

            except InvalidExecutionCheckpointError as error:
                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    message=(
                        "Invalid execution checkpoint: "
                        f"{error}"
                    ),
                    validation_report=validation_report,
                    permission_report=permission_report,
                )

            outputs = deepcopy(
                dict(checkpoint.outputs)
            )

            step_results = deepcopy(
                list(checkpoint.step_results)
            )

            next_step = checkpoint.next_step_number

            checkpoint_arguments = deepcopy(
                dict(
                    checkpoint.resolved_arguments
                )
            )

        for step in plan.steps:

            if step.step_number < next_step:
                continue

            try:
                if (
                    checkpoint_arguments is not None
                    and step.step_number == next_step
                ):
                    resolved_arguments = (
                        checkpoint_arguments
                    )
                else:
                    resolved_arguments = (
                        self._resolve_arguments(
                            step=step,
                            outputs=outputs,
                        )
                    )

            except ExecutorError as error:
                step_results.append(
                    StepExecutionResult(
                        step_number=step.step_number,
                        capability=step.capability,
                        status=ActionStatus.FAILED,
                        error=str(error),
                    )
                )

                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    step_results=tuple(step_results),
                    message=(
                        "Execution failed while "
                        "resolving dependencies."
                    ),
                    validation_report=validation_report,
                    permission_report=permission_report,
                )

            decision = decisions[
                step.step_number
            ]

            if (
                decision.requires_confirmation
                and step.step_number not in confirmed_steps
            ):
                try:
                    target = None
                    if (plan.overall_risk == RiskLevel.LOW
                            and step.capability == "browser"
                            and step.risk == RiskLevel.LOW):
                        target = prepare_browser_approval(
                            resolved_arguments,
                            request_id=plan.request_id,
                            step_number=step.step_number,
                            risk=step.risk,
                            effective_permission=decision.effective_permission,
                        )
                    continuation = ExecutionCheckpoint(
                        next_step_number=step.step_number,
                        outputs=deepcopy(outputs),
                        step_results=deepcopy(
                            tuple(step_results)
                        ),
                        resolved_arguments=deepcopy(
                            resolved_arguments
                        ),
                    )
                except BrowserCapabilityError as error:
                    return ExecutionReport(
                        request_id=plan.request_id,
                        status=ActionStatus.FAILED,
                        step_results=tuple(step_results),
                        message=f"Browser preparation failed: {error}",
                        validation_report=validation_report,
                        permission_report=permission_report,
                    )
                except Exception as error:
                    return ExecutionReport(
                        request_id=plan.request_id,
                        status=ActionStatus.FAILED,
                        step_results=tuple(step_results),
                        message=(
                            "Failed to create execution "
                            "checkpoint: "
                            f"{type(error).__name__}: {error}"
                        ),
                        validation_report=validation_report,
                        permission_report=permission_report,
                    )

                return ExecutionReport(
                    request_id=plan.request_id,
                    status=(
                        ActionStatus
                        .WAITING_FOR_PERMISSION
                    ),
                    step_results=tuple(step_results),
                    message=(
                        "Execution requires confirmation "
                        f"for step: {step.step_number}"
                    ),
                    validation_report=validation_report,
                    permission_report=permission_report,
                    checkpoint=continuation,
                    pending_approval_target=target,
                    pending_confirmation_steps=(
                        step.step_number,
                    ),
                )

            checkpoint_arguments = None
            if (pending_approval_target is not None
                    and step.step_number == pending_approval_target.subject.step_number):
                resolved_arguments = self._prepared_runtime_arguments(step, pending_approval_target)

            try:
                result = self._run_handler(step, resolved_arguments)

            except Exception as error:
                step_results.append(
                    StepExecutionResult(
                        step_number=step.step_number,
                        capability=step.capability,
                        status=ActionStatus.FAILED,
                        error=(
                            f"{type(error).__name__}: "
                            f"{error}"
                        ),
                    )
                )

                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    step_results=tuple(step_results),
                    message=(
                        f"Execution failed at "
                        f"step {step.step_number}."
                    ),
                    validation_report=validation_report,
                    permission_report=permission_report,
                )

            step_results.append(result)
            if result.status == ActionStatus.FAILED:
                return ExecutionReport(
                    request_id=plan.request_id, status=ActionStatus.FAILED,
                    step_results=tuple(step_results),
                    message=f"Execution failed at step {step.step_number}.",
                    validation_report=validation_report, permission_report=permission_report,
                )
            outputs[step.step_number] = result.data

        return ExecutionReport(
            request_id=plan.request_id,
            status=ActionStatus.COMPLETED,
            step_results=tuple(step_results),
            message=(
                "Execution plan completed successfully."
            ),
            validation_report=validation_report,
            permission_report=permission_report,
        )


    def _validate_browser_continuation(
        self,
        plan: ExecutionPlan,
        target: PendingApprovalTarget,
        *,
        confirmed_steps: frozenset[int],
        checkpoint: ExecutionCheckpoint | None,
    ) -> None:
        """Reject contradictory continuation state before any handler runs."""
        if not isinstance(target, PendingApprovalTarget):
            raise BrowserCapabilityError("Invalid pending approval envelope.")
        matching = tuple(step for step in plan.steps
                         if step.step_number == target.subject.step_number)
        if len(matching) != 1:
            raise BrowserCapabilityError("Pending browser step does not match the plan.")
        step = matching[0]
        if (
            plan.overall_risk != RiskLevel.LOW
            or step.capability != "browser"
            or step.risk != RiskLevel.LOW
            or step.step_number not in confirmed_steps
        ):
            raise BrowserCapabilityError("Unsupported or unconfirmed browser continuation.")
        validate_browser_approval_target(
            target, request_id=plan.request_id,
            step_number=step.step_number, risk=step.risk,
        )
        if checkpoint is not None:
            # The checkpoint flow validates its complete prefix before dispatch.
            if (
                checkpoint.next_step_number != step.step_number
                or dict(checkpoint.resolved_arguments) != {"url": target.prepared_target}
                or set(step.arguments) != {"url"}
                or step.arguments["url"].source != ArgumentSource.STEP_OUTPUT
            ):
                raise BrowserCapabilityError("Checkpoint does not represent the prepared browser url.")
        elif (
            len(plan.steps) != 1
            or set(step.arguments) != {"url"}
            or step.arguments["url"].source != ArgumentSource.LITERAL
            or step.arguments["url"].value != target.prepared_target
        ):
            raise BrowserCapabilityError("Literal plan does not represent the prepared browser url.")

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        confirmed_steps: frozenset[int] = frozenset(),
        checkpoint: ExecutionCheckpoint | None = None,
        pending_approval_target: PendingApprovalTarget | None = None,
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


        if (any(step.capability == "terminal" for step in plan.steps)
                or (isinstance(pending_approval_target, PendingApprovalTarget)
                    and pending_approval_target.subject.capability == "terminal")):
            return self._execute_terminal_approval(
                plan, confirmed_steps=confirmed_steps, checkpoint=checkpoint,
                pending_approval_target=pending_approval_target,
                validation_report=validation_report, permission_report=permission_report,
            )

        if pending_approval_target is not None:
            try:
                self._validate_browser_continuation(
                    plan, pending_approval_target,
                    confirmed_steps=confirmed_steps, checkpoint=checkpoint,
                )
            except (BrowserCapabilityError, ExecutorError) as error:
                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.FAILED,
                    message=f"Invalid browser continuation: {error}",
                    validation_report=validation_report,
                    permission_report=permission_report,
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


        # C2A is intentionally narrow:
        # exactly one LOW browser confirmation may execute
        # automatic prerequisite steps when its concrete target
        # depends on an earlier step output.
        use_checkpoint_flow = (
            checkpoint is not None
        )

        if (
            checkpoint is None
            and len(missing_confirmations) == 1
        ):
            pending_number = (
                missing_confirmations[0]
            )

            pending_step = next(
                step
                for step in plan.steps
                if step.step_number == pending_number
            )

            use_checkpoint_flow = (
                pending_step.capability == "browser"
                and pending_step.risk == RiskLevel.LOW
                and any(
                    argument.source
                    == ArgumentSource.STEP_OUTPUT
                    for argument
                    in pending_step.arguments.values()
                )
            )

        if use_checkpoint_flow:
            return self._execute_from_checkpoint_boundary(
                plan,
                confirmed_steps=confirmed_steps,
                validation_report=validation_report,
                permission_report=permission_report,
                checkpoint=checkpoint,
                pending_approval_target=pending_approval_target,
            )

        if missing_confirmations:
            if (
                len(plan.steps) == 1
                and plan.overall_risk == RiskLevel.LOW
                and plan.steps[0].capability == "browser"
                and plan.steps[0].risk == RiskLevel.LOW
                and not any(argument.source == ArgumentSource.STEP_OUTPUT
                            for argument in plan.steps[0].arguments.values())
            ):
                step = plan.steps[0]
                if not self._runtime_registry.has(step.capability):
                    return ExecutionReport(
                        request_id=plan.request_id,
                        status=ActionStatus.BLOCKED,
                        message="Missing runtime implementations: ('browser',)",
                        validation_report=validation_report,
                        permission_report=permission_report,
                    )
                try:
                    arguments = self._resolve_arguments(step=step, outputs={})
                    decision = next(item for item in permission_report.decisions
                                    if item.step_number == step.step_number)
                    target = prepare_browser_approval(
                        arguments, request_id=plan.request_id,
                        step_number=step.step_number, risk=step.risk,
                        effective_permission=decision.effective_permission,
                    )
                except (BrowserCapabilityError, ExecutorError) as error:
                    return ExecutionReport(
                        request_id=plan.request_id,
                        status=ActionStatus.FAILED,
                        message=f"Browser preparation failed: {error}",
                        validation_report=validation_report,
                        permission_report=permission_report,
                    )
                return ExecutionReport(
                    request_id=plan.request_id,
                    status=ActionStatus.WAITING_FOR_PERMISSION,
                    message=f"Execution requires confirmation for step: {step.step_number}",
                    validation_report=validation_report,
                    permission_report=permission_report,
                    pending_confirmation_steps=(step.step_number,),
                    pending_approval_target=target,
                )

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

                if (pending_approval_target is not None
                        and step.step_number == pending_approval_target.subject.step_number):
                    resolved_arguments = self._prepared_runtime_arguments(step, pending_approval_target)
                else:
                    resolved_arguments = self._resolve_arguments(step=step, outputs=outputs)

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


            # ------------------------------------------------
            # EXECUTE HANDLER
            # ------------------------------------------------

            try:

                result = self._run_handler(step, resolved_arguments)

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

            step_results.append(result)
            if result.status == ActionStatus.FAILED:
                return ExecutionReport(
                    request_id=plan.request_id, status=ActionStatus.FAILED,
                    step_results=tuple(step_results),
                    message=f"Execution failed at step {step.step_number}.",
                    validation_report=validation_report, permission_report=permission_report,
                )
            outputs[step.step_number] = result.data


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
