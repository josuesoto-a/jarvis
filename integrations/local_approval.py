"""Generic local human-approval envelope.

ApprovalSubject identifies the exact execution target.
LocalApproval adds interaction/session identity.

Neither grants authority. Actual authorization remains owned by
Orchestrator.validate_confirmation()/resume().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.approval import ApprovalSubject
from core.contracts import RiskLevel


@dataclass(
    frozen=True,
    slots=True,
)
class LocalApproval:
    """One exact approval target presented in one local interaction."""

    call_id: str
    subject: ApprovalSubject

    def __post_init__(
        self,
    ) -> None:

        if (
            type(self.call_id) is not str
            or not self.call_id
            or self.call_id.strip()
            != self.call_id
        ):
            raise ValueError(
                "call_id must be a nonempty trimmed string"
            )

        if not isinstance(
            self.subject,
            ApprovalSubject,
        ):
            raise TypeError(
                "subject must be an ApprovalSubject"
            )

    @property
    def request_id(
        self,
    ) -> str:

        return str(
            self.subject.request_id
        )

    @property
    def step_number(
        self,
    ) -> int:

        return self.subject.step_number

    @property
    def capability(
        self,
    ) -> str:

        return self.subject.capability

    @property
    def risk(
        self,
    ) -> RiskLevel:

        return self.subject.risk

    @property
    def arguments(
        self,
    ) -> dict[str, Any]:

        return self.subject.arguments

    @property
    def fingerprint(
        self,
    ) -> str:

        return self.subject.fingerprint


def approval_session_matches(
    action_session,
    approval: LocalApproval,
) -> bool:
    """Check exact Live/session identity.

    This grants no permission and does not prove that the execution
    target itself remains current.
    """

    try:
        snapshot = action_session.snapshot()
        action = snapshot.action

    except Exception:
        return False

    if action is None:
        return False

    pending = getattr(
        action,
        "pending_permission",
        None,
    )

    if pending is None:
        return False

    return (
        action.request_id
        == approval.request_id
        and action.call_id
        == approval.call_id
        and pending.request_id
        == approval.request_id
        and pending.call_id
        == approval.call_id
        and pending.pending_confirmation_steps
        == (
            approval.step_number,
        )
    )
