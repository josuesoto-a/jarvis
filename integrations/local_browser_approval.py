"""Trusted local browser approval surface for Jarvis voice.

Browser-specific human presentation is backed by the generic
ApprovalSubject identity contract.

This module NEVER grants permission from a model event.
It only previews stored browser identity and verifies that a local keyboard
approval still refers to that same target.

The Live call_id remains transport/session identity and deliberately
does not belong to the core ApprovalSubject.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from core.approval import (
    PendingApprovalTarget,
)
from core.contracts import PermissionMode, RiskLevel
from core.orchestrator import Orchestrator
from integrations.local_approval import (
    LocalApproval,
    approval_session_matches,
)
from integrations.openai_live import (
    PendingPermissionUpdate,
)


class LocalBrowserApproval(
    LocalApproval
):
    """Browser compatibility view over the generic local envelope."""

    __slots__ = ()

    @property
    def url(
        self,
    ) -> str:
        """Exact browser URL retained for the existing public API."""

        value = self.arguments.get(
            "url"
        )

        if type(value) is not str:
            raise ValueError(
                "Browser approval does not contain a valid URL."
            )

        return value


def _is_browser_approval(
    approval: LocalApproval,
) -> bool:
    arguments = approval.arguments

    return (
        approval.capability
        == "browser"
        and approval.risk
        == RiskLevel.LOW
        and set(arguments)
        == {"url"}
        and type(arguments["url"])
        is str
    )


def _stored_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Retain stored capability identity; add only interaction metadata."""
    if len(projection.pending_confirmation_steps) != 1:
        return None
    step_number = projection.pending_confirmation_steps[0]
    if type(step_number) is not int or step_number < 1 or step_number not in projection.confirmation_steps:
        return None
    try:
        request_id = UUID(projection.request_id)
        target = orchestrator.preview_pending_approval(request_id, step_number=step_number)
        if (not _is_stored_browser_target(target)
                or target.subject.request_id != request_id
                or target.subject.step_number != step_number):
            return None
        return LocalBrowserApproval(call_id=projection.call_id, subject=target.subject)
    except (TypeError, ValueError, AttributeError):
        return None


def _is_stored_browser_target(target: object) -> bool:
    return (
        isinstance(target, PendingApprovalTarget)
        and target.subject.capability == "browser"
        and target.subject.risk == RiskLevel.LOW
        and target.effective_permission == PermissionMode.CONFIRM_BEFORE_EXECUTION
        and type(target.prepared_target) is str
        and target.subject.arguments == {"url": target.prepared_target}
    )


def preview_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Compatibility entry point for stored C1 identity."""
    if projection.pending_confirmation_steps != (1,):
        return None
    return _stored_browser_approval(orchestrator, projection)


def preview_checkpoint_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Compatibility entry point for stored browser identity."""
    return _stored_browser_approval(orchestrator, projection)


def preview_browser_approval_v2(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Read the stored identity for supported C1/C2 browser confirmation."""
    return _stored_browser_approval(orchestrator, projection)


def format_browser_approval(
    approval: LocalBrowserApproval,
) -> str:
    """Render the existing browser-specific local human prompt."""

    if not _is_browser_approval(
        approval
    ):
        raise ValueError(
            "Browser renderer received a non-browser approval."
        )

    url = approval.arguments["url"]
    assert isinstance(url, str)

    hostname = urlsplit(
        url
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
                f"{url}"
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
    """Check session freshness and stored identity; atomic claim follows later."""
    if not _is_browser_approval(approval) or not approval_session_matches(action_session, approval):
        return False
    target = orchestrator.preview_pending_approval(
        approval.subject.request_id, step_number=approval.step_number,
    )
    return _is_stored_browser_target(target) and target.subject.same_target_as(approval.subject)
