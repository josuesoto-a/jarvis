"""Trusted local browser approval surface for Jarvis voice.

This module NEVER grants permission from a model event.
It only previews an existing immutable pending plan and verifies that
a local keyboard approval still refers to the same action and URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from core.orchestrator import Orchestrator
from integrations.openai_live import PendingPermissionUpdate


@dataclass(frozen=True, slots=True)
class LocalBrowserApproval:
    request_id: str
    call_id: str
    step_number: int
    url: str


def preview_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Only one outstanding literal browser step is eligible."""

    if (
        projection.pending_confirmation_steps != (1,)
        or 1 not in projection.confirmation_steps
    ):
        return None

    try:
        request_id = UUID(projection.request_id)
    except (TypeError, ValueError, AttributeError):
        return None

    url = orchestrator.preview_single_browser(request_id)

    if url is None:
        return None

    return LocalBrowserApproval(
        request_id=projection.request_id,
        call_id=projection.call_id,
        step_number=1,
        url=url,
    )


def preview_checkpoint_browser_approval(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Preview one browser step whose URL is frozen in a checkpoint."""

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

    return LocalBrowserApproval(
        request_id=projection.request_id,
        call_id=projection.call_id,
        step_number=step_number,
        url=url,
    )


def preview_browser_approval_v2(
    orchestrator: Orchestrator,
    projection: PendingPermissionUpdate,
) -> LocalBrowserApproval | None:
    """Support C1 literal URLs plus C2 frozen dependent URLs."""

    literal = preview_browser_approval(
        orchestrator,
        projection,
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
    hostname = urlsplit(approval.url).hostname

    return "\n".join(
        [
            "",
            "=" * 60,
            "AUTORIZACIÓN LOCAL — NAVEGADOR",
            "=" * 60,
            f"Request ID: {approval.request_id}",
            f"Paso: {approval.step_number}",
            "Capability: browser",
            f"Dominio: {hostname}",
            f"URL EXACTA: {approval.url}",
            "",
            "Para permitir ESTA apertura, escribe AUTORIZAR",
            "en el teclado local y presiona Enter.",
            "La voz no concede permisos.",
            "=" * 60,
        ]
    )


def approval_is_current(
    orchestrator: Orchestrator,
    action_session,
    approval: LocalBrowserApproval,
) -> bool:
    """Recheck the active call, pending grant and immutable URL."""

    snapshot = action_session.snapshot()
    action = snapshot.action

    if action is None:
        return False

    pending = action.pending_permission

    if (
        action.request_id != approval.request_id
        or action.call_id != approval.call_id
        or pending is None
        or pending.request_id != approval.request_id
        or pending.call_id != approval.call_id
        or pending.pending_confirmation_steps
        != (approval.step_number,)
    ):
        return False

    try:
        request_id = UUID(approval.request_id)
    except (TypeError, ValueError, AttributeError):
        return False

    literal_url = None

    if approval.step_number == 1:
        literal_url = (
            orchestrator
            .preview_single_browser(
                request_id
            )
        )

    if literal_url == approval.url:
        return True

    checkpoint_url = (
        orchestrator
        .preview_checkpoint_browser(
            request_id,
            step_number=(
                approval.step_number
            ),
        )
    )

    return (
        checkpoint_url
        == approval.url
    )
