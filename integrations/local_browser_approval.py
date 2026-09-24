"""Trusted local browser approval surface for Jarvis voice.

Browser-specific human presentation is backed by the generic
ApprovalSubject identity contract.

This module NEVER grants permission from a model event.
It only previews an existing pending action, binds the exact browser
target to an ApprovalSubject, and verifies that a local keyboard
approval still refers to that same target.

The Live call_id remains transport/session identity and deliberately
does not belong to the core ApprovalSubject.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from core.approval import (
    ApprovalContractError,
    ApprovalSubject,
)
from core.contracts import RiskLevel
from core.orchestrator import Orchestrator
from integrations.openai_live import (
    PendingPermissionUpdate,
)


@dataclass(
    frozen=True,
    slots=True,
)
class LocalBrowserApproval:
    """Live/session wrapper around one generic approval subject."""

    call_id: str
    subject: ApprovalSubject

    def __post_init__(
        self,
    ) -> None:

        if (
            type(self.call_id) is not str
            or not self.call_id
        ):
            raise ValueError(
                "call_id must be a nonempty string"
            )

        arguments = self.subject.arguments

        if (
            self.subject.capability
            != "browser"
            or self.subject.risk
            != RiskLevel.LOW
            or set(arguments)
            != {"url"}
            or type(arguments["url"])
            is not str
        ):
            raise ValueError(
                "LocalBrowserApproval requires "
                "one low-risk browser URL subject."
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

        return (
            self.subject.step_number
        )

    @property
    def url(
        self,
    ) -> str:

        value = (
            self.subject.arguments[
                "url"
            ]
        )

        assert isinstance(
            value,
            str,
        )

        return value

    @property
    def fingerprint(
        self,
    ) -> str:

        return (
            self.subject.fingerprint
        )


def _make_browser_approval(
    *,
    projection: PendingPermissionUpdate,
    request_id: UUID,
    step_number: int,
    url: str,
) -> LocalBrowserApproval | None:
    """Bind one validated browser preview to generic approval identity."""

    try:
        subject = ApprovalSubject(
            request_id=request_id,
            step_number=step_number,
            capability="browser",
            risk=RiskLevel.LOW,
            arguments={
                "url": url,
            },
        )

        return LocalBrowserApproval(
            call_id=projection.call_id,
            subject=subject,
        )

    except (
        ApprovalContractError,
        TypeError,
        ValueError,
    ):
        return None


def preview_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Original C1 literal browser approval, now using ApprovalSubject."""

    if (
        projection
        .pending_confirmation_steps
        != (1,)
        or 1
        not in projection.confirmation_steps
    ):
        return None

    try:
        request_id = UUID(
            projection.request_id
        )

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None

    url = (
        orchestrator
        .preview_single_browser(
            request_id
        )
    )

    if url is None:
        return None

    return _make_browser_approval(
        projection=projection,
        request_id=request_id,
        step_number=1,
        url=url,
    )


def preview_checkpoint_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """C2 browser approval for an exact URL frozen in checkpoint."""

    if (
        len(
            projection
            .pending_confirmation_steps
        )
        != 1
    ):
        return None

    step_number = (
        projection
        .pending_confirmation_steps[0]
    )

    if (
        type(step_number) is not int
        or step_number < 1
        or step_number
        not in projection.confirmation_steps
    ):
        return None

    try:
        request_id = UUID(
            projection.request_id
        )

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None

    url = (
        orchestrator
        .preview_checkpoint_browser(
            request_id,
            step_number=step_number,
        )
    )

    if url is None:
        return None

    return _make_browser_approval(
        projection=projection,
        request_id=request_id,
        step_number=step_number,
        url=url,
    )


def preview_browser_approval_v2(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Support C1 literal plus C2 checkpoint browser approvals."""

    literal = (
        preview_browser_approval(
            orchestrator,
            projection,
        )
    )

    if literal is not None:
        return literal

    return (
        preview_checkpoint_browser_approval(
            orchestrator,
            projection,
        )
    )


def format_browser_approval(
    approval: LocalBrowserApproval,
) -> str:
    """Render the existing browser-specific local human prompt."""

    hostname = urlsplit(
        approval.url
    ).hostname

    return "\n".join(
        [
            "",
            "=" * 60,
            "AUTORIZACIÓN LOCAL — NAVEGADOR",
            "=" * 60,
            (
                "Request ID: "
                f"{approval.request_id}"
            ),
            (
                "Paso: "
                f"{approval.step_number}"
            ),
            "Capability: browser",
            f"Dominio: {hostname}",
            (
                "URL EXACTA: "
                f"{approval.url}"
            ),
            "",
            (
                "Para permitir ESTA apertura, "
                "escribe AUTORIZAR"
            ),
            (
                "en el teclado local y "
                "presiona Enter."
            ),
            "La voz no concede permisos.",
            "=" * 60,
        ]
    )


def approval_is_current(
    orchestrator: Orchestrator,
    action_session,
    approval: LocalBrowserApproval,
) -> bool:
    """Rebuild and compare the exact current approval subject.

    call_id protects Live/session identity.
    ApprovalSubject protects execution-target identity.
    Orchestrator remains the source of current pending state.
    """

    snapshot = (
        action_session.snapshot()
    )

    action = snapshot.action

    if action is None:
        return False

    pending = (
        action.pending_permission
    )

    if (
        action.request_id
        != approval.request_id
        or action.call_id
        != approval.call_id
        or pending is None
        or pending.request_id
        != approval.request_id
        or pending.call_id
        != approval.call_id
        or pending
        .pending_confirmation_steps
        != (
            approval.step_number,
        )
    ):
        return False

    try:
        request_id = UUID(
            approval.request_id
        )

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return False

    current_url = None

    # Preserve C1 behavior.
    if approval.step_number == 1:
        current_url = (
            orchestrator
            .preview_single_browser(
                request_id
            )
        )

    # Preserve C2 behavior.
    if current_url is None:
        current_url = (
            orchestrator
            .preview_checkpoint_browser(
                request_id,
                step_number=(
                    approval.step_number
                ),
            )
        )

    if current_url is None:
        return False

    try:
        current_subject = (
            ApprovalSubject(
                request_id=request_id,
                step_number=(
                    approval.step_number
                ),
                capability="browser",
                risk=RiskLevel.LOW,
                arguments={
                    "url":
                        current_url,
                },
            )
        )

    except (
        ApprovalContractError,
        TypeError,
        ValueError,
    ):
        return False

    return (
        approval.subject
        .same_target_as(
            current_subject
        )
    )
