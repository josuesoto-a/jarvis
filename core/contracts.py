"""
Core contracts for Jarvis.

These objects describe what Jarvis wants to do without actually
performing external actions.

No web access.
No filesystem writes.
No computer control.
No API calls.

This module defines data only.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import UUID, uuid4


# ============================================================
# RISK
# ============================================================

class RiskLevel(str, Enum):
    """
    General risk associated with an action.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================
# PERMISSIONS
# ============================================================

class PermissionMode(str, Enum):
    """
    How Jarvis is allowed to perform an action.
    """

    AUTOMATIC = "automatic"
    CONFIRM_BEFORE_EXECUTION = "confirm_before_execution"
    FORBIDDEN = "forbidden"


# ============================================================
# ACTION STATUS
# ============================================================

class ActionStatus(str, Enum):
    """
    Lifecycle state of an action.
    """

    PENDING = "pending"
    PLANNING = "planning"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


# ============================================================
# ARGUMENT SOURCE
# ============================================================

class ArgumentSource(str, Enum):
    """
    Where an execution argument gets its value.
    """

    LITERAL = "literal"
    STEP_OUTPUT = "step_output"


# ============================================================
# EXECUTION ARGUMENT
# ============================================================

@dataclass(frozen=True, slots=True)
class ExecutionArgument:
    """
    One argument passed to a capability.

    A literal argument is already known when the plan is created.

    Example:

        query = "precio actual Big Mac"

    A step_output argument is produced by an earlier execution step.

    Example:

        step 2 browser.url
            <- step 1 best_result_url
    """

    source: ArgumentSource

    value: str | None = None

    step_number: int | None = None

    output_key: str | None = None


    def __post_init__(self) -> None:

        if self.source == ArgumentSource.LITERAL:

            if self.value is None or not self.value.strip():
                raise ValueError(
                    "Literal execution arguments require "
                    "a non-empty value."
                )

            if (
                self.step_number is not None
                or self.output_key is not None
            ):
                raise ValueError(
                    "Literal execution arguments cannot "
                    "reference a step output."
                )

            return


        if self.source == ArgumentSource.STEP_OUTPUT:

            if self.value is not None:
                raise ValueError(
                    "Step-output arguments cannot contain "
                    "a literal value."
                )

            if (
                self.step_number is None
                or self.step_number < 1
            ):
                raise ValueError(
                    "Step-output arguments require a positive "
                    "step number."
                )

            if (
                self.output_key is None
                or not self.output_key.strip()
            ):
                raise ValueError(
                    "Step-output arguments require "
                    "a non-empty output key."
                )

            return


        raise ValueError(
            f"Unsupported argument source: {self.source}"
        )


    @classmethod
    def literal(
        cls,
        value: str,
    ) -> "ExecutionArgument":

        return cls(
            source=ArgumentSource.LITERAL,
            value=value.strip(),
        )


    @classmethod
    def step_output(
        cls,
        step_number: int,
        output_key: str,
    ) -> "ExecutionArgument":

        return cls(
            source=ArgumentSource.STEP_OUTPUT,
            step_number=step_number,
            output_key=output_key.strip(),
        )


# ============================================================
# ACTION REQUEST
# ============================================================

@dataclass(frozen=True, slots=True)
class ActionRequest:
    """
    A normalized goal received by Jarvis.
    """

    goal: str

    raw_input: str

    context: Mapping[str, Any] = field(
        default_factory=dict
    )

    request_id: UUID = field(
        default_factory=uuid4
    )


# ============================================================
# CAPABILITY
# ============================================================

@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """
    Pure description of one capability available to Jarvis.
    """

    name: str

    description: str

    version: str = "1.0"

    tags: tuple[str, ...] = ()


# ============================================================
# CAPABILITY REQUIREMENT
# ============================================================

@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """
    A capability needed to accomplish a goal.
    """

    capability: str

    reason: str

    required: bool = True


# ============================================================
# EXECUTION STEP
# ============================================================

@dataclass(frozen=True, slots=True)
class ExecutionStep:
    """
    One step inside an execution plan.

    arguments contains the inputs required by the capability.

    Arguments may either be literal values or references to
    outputs produced by earlier steps.
    """

    step_number: int

    description: str

    capability: str

    arguments: Mapping[
        str,
        ExecutionArgument,
    ] = field(
        default_factory=dict
    )

    risk: RiskLevel = RiskLevel.LOW

    permission: PermissionMode = (
        PermissionMode.AUTOMATIC
    )


# ============================================================
# EXECUTION PLAN
# ============================================================

@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """
    Ordered plan created from an ActionRequest.
    """

    request_id: UUID

    steps: tuple[
        ExecutionStep,
        ...
    ]

    overall_risk: RiskLevel


# ============================================================
# ACTION RESULT
# ============================================================

@dataclass(frozen=True, slots=True)
class ActionResult:
    """
    Final outcome of an action or execution step.
    """

    request_id: UUID

    status: ActionStatus

    summary: str

    data: Mapping[str, Any] = field(
        default_factory=dict
    )

    error: str | None = None
