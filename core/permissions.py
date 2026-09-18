"""
Deterministic permission policy for Jarvis.

The Permission Engine decides what Jarvis is actually allowed to do.

Important:

- Planner permissions are proposals only.
- The model cannot weaken local policy.
- Local policy may always require stronger permission.
- Critical actions are forbidden by default.
- This module performs no external actions.
"""

from dataclasses import dataclass

from core.contracts import (
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)


# ============================================================
# PERMISSION ORDER
# ============================================================

_PERMISSION_ORDER = {
    PermissionMode.AUTOMATIC: 0,
    PermissionMode.CONFIRM_BEFORE_EXECUTION: 1,
    PermissionMode.FORBIDDEN: 2,
}


# ============================================================
# RISK POLICY
# ============================================================

_RISK_PERMISSION_FLOOR = {
    RiskLevel.LOW:
        PermissionMode.AUTOMATIC,

    RiskLevel.MEDIUM:
        PermissionMode.CONFIRM_BEFORE_EXECUTION,

    RiskLevel.HIGH:
        PermissionMode.CONFIRM_BEFORE_EXECUTION,

    RiskLevel.CRITICAL:
        PermissionMode.FORBIDDEN,
}


# ============================================================
# CAPABILITY POLICY
# ============================================================

_CAPABILITY_PERMISSION_FLOOR = {
    "weather":
        PermissionMode.AUTOMATIC,

    "web_search":
        PermissionMode.AUTOMATIC,

    "browser":
        PermissionMode.AUTOMATIC,

    "computer_use":
        PermissionMode.CONFIRM_BEFORE_EXECUTION,

    "filesystem":
        PermissionMode.CONFIRM_BEFORE_EXECUTION,

    "terminal":
        PermissionMode.CONFIRM_BEFORE_EXECUTION,

    "codex":
        PermissionMode.CONFIRM_BEFORE_EXECUTION,
}


# ============================================================
# DECISION
# ============================================================

@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """
    Effective permission decision for one ExecutionStep.
    """

    step_number: int

    capability: str

    planner_permission: PermissionMode

    effective_permission: PermissionMode

    reasons: tuple[str, ...]


    @property
    def automatic(self) -> bool:
        return (
            self.effective_permission
            == PermissionMode.AUTOMATIC
        )


    @property
    def requires_confirmation(self) -> bool:
        return (
            self.effective_permission
            == PermissionMode.CONFIRM_BEFORE_EXECUTION
        )


    @property
    def forbidden(self) -> bool:
        return (
            self.effective_permission
            == PermissionMode.FORBIDDEN
        )


# ============================================================
# REPORT
# ============================================================

@dataclass(frozen=True, slots=True)
class PermissionReport:
    """
    Permission decisions for a complete ExecutionPlan.
    """

    decisions: tuple[
        PermissionDecision,
        ...
    ]


    @property
    def all_automatic(self) -> bool:
        return all(
            decision.automatic
            for decision in self.decisions
        )


    @property
    def requires_confirmation(self) -> bool:
        return any(
            decision.requires_confirmation
            for decision in self.decisions
        )


    @property
    def has_forbidden_steps(self) -> bool:
        return any(
            decision.forbidden
            for decision in self.decisions
        )


    @property
    def confirmation_steps(
        self,
    ) -> tuple[PermissionDecision, ...]:

        return tuple(
            decision
            for decision in self.decisions
            if decision.requires_confirmation
        )


    @property
    def forbidden_steps(
        self,
    ) -> tuple[PermissionDecision, ...]:

        return tuple(
            decision
            for decision in self.decisions
            if decision.forbidden
        )


# ============================================================
# HELPERS
# ============================================================

def _stricter_permission(
    *permissions: PermissionMode,
) -> PermissionMode:
    """
    Return the strictest supplied permission mode.
    """

    return max(
        permissions,
        key=lambda permission:
            _PERMISSION_ORDER[permission],
    )


# ============================================================
# ENGINE
# ============================================================

class PermissionEngine:
    """
    Apply deterministic local policy to ExecutionPlans.
    """

    def evaluate_step(
        self,
        step: ExecutionStep,
    ) -> PermissionDecision:
        """
        Determine the effective permission for one step.
        """

        reasons: list[str] = []


        # ----------------------------------------------------
        # PLANNER PROPOSAL
        # ----------------------------------------------------

        planner_permission = (
            step.permission
        )


        # ----------------------------------------------------
        # RISK FLOOR
        # ----------------------------------------------------

        risk_floor = (
            _RISK_PERMISSION_FLOOR[
                step.risk
            ]
        )


        if (
            _PERMISSION_ORDER[risk_floor]
            >
            _PERMISSION_ORDER[
                planner_permission
            ]
        ):
            reasons.append(
                f"Risk level {step.risk.value} "
                f"requires at least "
                f"{risk_floor.value}."
            )


        # ----------------------------------------------------
        # CAPABILITY FLOOR
        # ----------------------------------------------------

        capability_floor = (
            _CAPABILITY_PERMISSION_FLOOR.get(
                step.capability,
                PermissionMode.CONFIRM_BEFORE_EXECUTION,
            )
        )


        if (
            _PERMISSION_ORDER[
                capability_floor
            ]
            >
            _PERMISSION_ORDER[
                planner_permission
            ]
        ):
            reasons.append(
                f"Capability {step.capability} "
                f"requires at least "
                f"{capability_floor.value}."
            )


        # ----------------------------------------------------
        # EFFECTIVE POLICY
        # ----------------------------------------------------

        effective = _stricter_permission(
            planner_permission,
            risk_floor,
            capability_floor,
        )


        if not reasons:

            reasons.append(
                "Planner proposal satisfies "
                "local permission policy."
            )


        return PermissionDecision(
            step_number=step.step_number,
            capability=step.capability,
            planner_permission=planner_permission,
            effective_permission=effective,
            reasons=tuple(reasons),
        )


    def evaluate(
        self,
        plan: ExecutionPlan,
    ) -> PermissionReport:
        """
        Evaluate every step in plan order.
        """

        return PermissionReport(
            decisions=tuple(
                self.evaluate_step(step)
                for step in plan.steps
            )
        )
