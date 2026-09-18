"""
Top-level orchestration for Jarvis actions.

The Orchestrator connects:

    ActionRequest
        ->
    Planner
        ->
    ExecutionPlan
        ->
    Executor
        ->
    ExecutionReport

It does not implement capabilities itself.

Planning, validation, permissions, runtime dispatch, dependency
resolution, and execution remain separate concerns.
"""

from dataclasses import dataclass
from uuid import UUID

from core.contracts import (
    ActionRequest,
    ActionStatus,
    ExecutionPlan,
)
from core.executor import (
    ExecutionReport,
    Executor,
)
from core.planner import (
    Planner,
)


# ============================================================
# RESULT
# ============================================================

@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """
    Complete result of attempting to fulfill one ActionRequest.
    """

    request_id: UUID

    status: ActionStatus

    plan: ExecutionPlan | None = None

    execution_report: ExecutionReport | None = None

    error: str | None = None


    @property
    def completed(
        self,
    ) -> bool:

        return (
            self.status
            == ActionStatus.COMPLETED
        )


    @property
    def waiting_for_permission(
        self,
    ) -> bool:

        return (
            self.status
            == ActionStatus.WAITING_FOR_PERMISSION
        )


# ============================================================
# ORCHESTRATOR
# ============================================================

class Orchestrator:
    """
    Coordinate planning and execution for one user goal.
    """

    def __init__(
        self,
        *,
        planner: Planner,
        executor: Executor,
    ) -> None:

        self._planner = planner

        self._executor = executor


    def run(
        self,
        request: ActionRequest,
        *,
        confirmed_steps: frozenset[int] = frozenset(),
    ) -> OrchestrationResult:
        """
        Plan and attempt to execute one ActionRequest.
        """

        # ====================================================
        # PLANNING
        # ====================================================

        try:

            plan = self._planner.plan(
                request
            )

        except Exception as error:

            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.FAILED,
                error=(
                    "Planning failed: "
                    f"{type(error).__name__}: "
                    f"{error}"
                ),
            )


        # ====================================================
        # REQUEST-ID INVARIANT
        # ====================================================

        if (
            plan.request_id
            != request.request_id
        ):

            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.FAILED,
                plan=plan,
                error=(
                    "Planner returned a plan with "
                    "a different request_id."
                ),
            )


        # ====================================================
        # EXECUTION
        # ====================================================

        try:

            execution_report = (
                self._executor.execute(
                    plan,
                    confirmed_steps=(
                        confirmed_steps
                    ),
                )
            )

        except Exception as error:

            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.FAILED,
                plan=plan,
                error=(
                    "Execution engine failed: "
                    f"{type(error).__name__}: "
                    f"{error}"
                ),
            )


        # ====================================================
        # RESULT
        # ====================================================

        return OrchestrationResult(
            request_id=request.request_id,
            status=(
                execution_report.status
            ),
            plan=plan,
            execution_report=(
                execution_report
            ),
        )
