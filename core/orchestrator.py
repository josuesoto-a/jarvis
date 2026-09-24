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
from threading import Lock
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

    pending_confirmation_steps: tuple[int, ...] = ()


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

@dataclass(frozen=True, slots=True)
class _PendingExecution:
    result: OrchestrationResult
    confirmed_steps: frozenset[int]


class Orchestrator:
    """Plan once, then resume only the exact plan awaiting confirmation.

    run(request) never accepts confirmations. Call resume(request_id,
    confirmed_steps=...) on this same instance after presenting its waiting
    result. Confirmations are explicit step numbers, cumulative across partial
    resumes, and restricted to steps requested by the latest permission report.

    Each request_id may be planned only once during this instance's lifetime.
    IDs remain reserved after terminal results to prevent delayed confirmations
    from authorizing a replacement plan. Use a fresh ID for a new action.

    State is in memory: pending plans and reserved IDs are not durable and grow
    with use. Cross-process/restart resumption and caller authentication belong
    to a future interaction/persistence layer; an ID alone is not permission.
    """

    def __init__(
        self,
        *,
        planner: Planner,
        executor: Executor,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._pending: dict[UUID, _PendingExecution] = {}
        self._used_request_ids: set[UUID] = set()
        # Protect admission and atomic consumption, never external execution.
        self._lock = Lock()

    def preview_single_browser(self, request_id: UUID) -> str | None:
        """Read-only preview of one pending, literal, low-risk browser step.

        A multi-step plan or an unresolved URL cannot be approved by the
        initial local keyboard UI. This method never grants permission.
        """
        from unicodedata import category
        from urllib.parse import urlsplit

        from core.contracts import ArgumentSource, RiskLevel

        with self._lock:
            pending = self._pending.get(request_id)

            if pending is None:
                return None

            result = pending.result
            plan = result.plan

            if (
                result.status != ActionStatus.WAITING_FOR_PERMISSION
                or result.pending_confirmation_steps != (1,)
                or plan is None
                or len(plan.steps) != 1
                or plan.overall_risk != RiskLevel.LOW
            ):
                return None

            step = plan.steps[0]

            if (
                step.step_number != 1
                or step.capability != "browser"
                or step.risk != RiskLevel.LOW
                or set(step.arguments) != {"url"}
            ):
                return None

            argument = step.arguments["url"]

            if (
                argument.source != ArgumentSource.LITERAL
                or type(argument.value) is not str
            ):
                return None

            url = argument.value

        # Keep console previews unambiguous and safe to display.
        if (
            not url
            or len(url) > 2048
            or "\\" in url
            or any(
                character.isspace()
                or category(character) in {"Cc", "Cf"}
                for character in url
            )
        ):
            return None

        try:
            parsed = urlsplit(url)

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None

            # Accessing .port also rejects malformed port numbers.
            _ = parsed.port

        except ValueError:
            return None

        return url

    def run(self, request: ActionRequest) -> OrchestrationResult:
        """Plan a new request exactly once, without pre-authorizing any step."""
        with self._lock:
            if request.request_id in self._used_request_ids:
                return OrchestrationResult(
                    request_id=request.request_id,
                    status=ActionStatus.FAILED,
                    error="request_id already used; resume the pending plan or use a new ID.",
                )
            self._used_request_ids.add(request.request_id)

        try:
            plan = self._planner.plan(request)
        except Exception as error:
            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.FAILED,
                error=f"Planning failed: {type(error).__name__}: {error}",
            )

        if plan.request_id != request.request_id:
            return OrchestrationResult(
                request_id=request.request_id,
                status=ActionStatus.FAILED,
                plan=plan,
                error="Planner returned a plan with a different request_id.",
            )

        return self._execute(plan, confirmed_steps=frozenset())

    def resume(
        self,
        request_id: UUID,
        *,
        confirmed_steps: frozenset[int],
    ) -> OrchestrationResult:
        """Consume a pending plan atomically and execute it without planning.

        Invalid confirmations raise TypeError/ValueError without consuming the
        plan. An unknown, running or terminal ID returns FAILED without calling
        the executor. A WAITING result retains the same plan and prior grants;
        every other result (including an exception) permanently ends resumption.
        """
        self._validate_confirmation_types(request_id, confirmed_steps)
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return OrchestrationResult(
                    request_id=request_id,
                    status=ActionStatus.FAILED,
                    error="No plan is waiting for permission for this request_id.",
                )
            self._validate_pending_confirmation(pending, confirmed_steps)
            confirmed = pending.confirmed_steps | confirmed_steps
            # Consume atomically; never run the executor under the lock.
            del self._pending[request_id]

        plan = pending.result.plan
        assert plan is not None
        return self._execute(plan, confirmed_steps=confirmed)

    @staticmethod
    def _validate_confirmation_types(
        request_id: UUID, confirmed_steps: frozenset[int],
    ) -> None:
        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be a UUID")
        if type(confirmed_steps) is not frozenset:
            raise TypeError("confirmed_steps must be a frozenset of positive integers")
        if any(type(step) is not int or step < 1 for step in confirmed_steps):
            raise ValueError("confirmed_steps must contain only positive integers")

    @staticmethod
    def _validate_pending_confirmation(
        pending: _PendingExecution, confirmed_steps: frozenset[int],
    ) -> None:
        if not confirmed_steps:
            raise ValueError("confirmed_steps must not be empty")
        if confirmed_steps & pending.confirmed_steps:
            raise ValueError("confirmed_steps contains an already confirmed step")
        if not confirmed_steps <= set(pending.result.pending_confirmation_steps):
            raise ValueError("confirmed_steps contains steps not requested for confirmation")

    def validate_confirmation(
        self, request_id: UUID, *, confirmed_steps: frozenset[int],
    ) -> None:
        """Read-only admission check; neither grant nor reserve authorization.

        resume revalidates atomically when the queued operation is processed.
        The worker never needs a copy of the pending plan or accumulated grants.
        """
        self._validate_confirmation_types(request_id, confirmed_steps)
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise ValueError("No plan is waiting for permission for this request_id.")
            self._validate_pending_confirmation(pending, confirmed_steps)

    def _execute(
        self,
        plan: ExecutionPlan,
        *,
        confirmed_steps: frozenset[int],
    ) -> OrchestrationResult:
        try:
            report = self._executor.execute(plan, confirmed_steps=confirmed_steps)
            if report.request_id != plan.request_id:
                raise ValueError("Executor returned a report with a different request_id.")
            if report.status == ActionStatus.WAITING_FOR_PERMISSION and report.step_results:
                # The executor must wait before running any handler. Never replay
                # a plan whose supposedly waiting report contains executed steps.
                raise ValueError("A waiting plan must not contain executed steps.")
        except Exception as error:
            return OrchestrationResult(
                request_id=plan.request_id,
                status=ActionStatus.FAILED,
                plan=plan,
                error=f"Execution engine failed: {type(error).__name__}: {error}",
            )

        result = OrchestrationResult(
            request_id=plan.request_id,
            status=report.status,
            plan=plan,
            execution_report=report,
            pending_confirmation_steps=tuple(
                decision.step_number
                for decision in report.permission_report.confirmation_steps
                if decision.step_number not in confirmed_steps
            ) if (
                report.status == ActionStatus.WAITING_FOR_PERMISSION
                and report.permission_report is not None
            ) else (),
        )
        if result.waiting_for_permission:
            with self._lock:
                self._pending[plan.request_id] = _PendingExecution(result, confirmed_steps)
        return result
