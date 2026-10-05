"""Stored terminal target presentation; no execution or authorization authority."""

from __future__ import annotations

from dataclasses import dataclass
import json
from uuid import UUID

from capabilities.terminal import (
    TerminalExecutionTarget,
    validate_terminal_approval_target,
)
from core.approval import PendingApprovalTarget
from core.contracts import PermissionMode
from integrations.local_approval import LocalApproval, approval_session_matches
from integrations.openai_live import PendingPermissionUpdate


@dataclass(frozen=True, slots=True)
class LocalTerminalApproval(LocalApproval):
    """Retain the prepared object for presentation, alongside session/subject.

    Coordinates, capability and risk remain authoritative in the subject.
    There is no grant or authorization state in this envelope.
    """

    prepared_target: TerminalExecutionTarget

    def __post_init__(self) -> None:
        LocalApproval.__post_init__(self)
        _validate_retained_approval(self)


def _validate_retained_approval(approval: LocalTerminalApproval) -> None:
    if type(approval) is not LocalTerminalApproval:
        raise TypeError("Terminal renderer requires a LocalTerminalApproval")
    # A validation envelope only: neither target nor subject is reconstructed.
    validate_terminal_approval_target(
        PendingApprovalTarget(
            subject=approval.subject,
            effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            prepared_target=approval.prepared_target,
        ),
        request_id=approval.subject.request_id,
        step_number=approval.subject.step_number,
        risk=approval.subject.risk,
    )


def preview_terminal_target(
    projection: PendingPermissionUpdate,
    pending_target: PendingApprovalTarget,
) -> LocalTerminalApproval | None:
    """Consume the authoritative stored target, without discovery/preparation."""
    if type(pending_target) is not PendingApprovalTarget:
        return None
    try:
        step_number = pending_target.subject.step_number
        if (
            projection.pending_confirmation_steps != (step_number,)
            or any(type(step) is not int for step in projection.pending_confirmation_steps)
            or step_number not in projection.confirmation_steps
        ):
            return None
        validate_terminal_approval_target(
            pending_target,
            request_id=UUID(projection.request_id),
            step_number=step_number,
            risk=pending_target.subject.risk,
        )
        return LocalTerminalApproval(
            call_id=projection.call_id,
            subject=pending_target.subject,
            prepared_target=pending_target.prepared_target,
        )
    except (TypeError, ValueError, AttributeError):
        return None


def display_terminal_string(value: str) -> str:
    """One quoted ASCII JSON string; reversible, deterministic display only.

    Quotes/backslashes, whitespace controls, DEL and every non-ASCII code point
    (including invisible/formatting/bidi characters) are escaped, never dropped.
    The quotes expose empty strings and leading/trailing spaces. This is not
    command-line serialization and never changes a stored value.
    """
    if type(value) is not str:
        raise TypeError("Terminal display values must be plain strings")
    return json.dumps(value, ensure_ascii=True)


def format_terminal_approval(approval: LocalTerminalApproval) -> str:
    """Render retained identity only; no pending lookup or ambient reads."""
    _validate_retained_approval(approval)
    target = approval.prepared_target
    quoted = display_terminal_string
    lines = [
        "", "=" * 60, "AUTORIZACION LOCAL - TERMINAL", "=" * 60,
        f"Request ID: {approval.request_id}",
        f"Paso: {approval.step_number}",
        "Capability: terminal",
        f"Riesgo efectivo: {approval.risk.value}",
        "Permiso efectivo: CONFIRM_BEFORE_EXECUTION",
        f"Contrato: {quoted(target.contract_version)}",
        f"Plataforma: {quoted(target.platform_contract)}",
        "Politica de runtime declarada (sin implementacion de ejecucion):",
        quoted(json.dumps(target.runtime_policy.approval_arguments(), ensure_ascii=True,
                          sort_keys=True, separators=(",", ":"))),
        "Valores entre comillas: cadenas JSON escapadas, sin normalizacion.",
        f"Ejecutable solicitado: {quoted(target.executable_requested)}",
        f"Ejecutable resuelto (ruta COMPLETA): {quoted(target.executable_resolved)}",
        f"Identidad del ejecutable: {quoted(target.executable_identity)}",
        f"argv: {len(target.argv)} argumento(s), en orden:",
    ]
    lines.extend(f"argv[{index}] = {quoted(value)}"
                 for index, value in enumerate(target.argv))
    lines.extend([
        f"cwd EXACTO: {quoted(target.cwd)}",
        f"Timeout (segundos): {target.timeout_seconds}",
        f"stdin: {quoted(target.stdin_mode)} (no interactivo)",
        "shell: false (deshabilitado)",
        f"Politica de entorno: {quoted(target.environment_policy_id)}",
        f"Identidad del entorno: {quoted(target.environment_identity)}",
        "Detalle del entorno congelado (solo allowlist; sin lecturas nuevas):",
    ])
    lines.extend(f"environment[{quoted(key)}] = {quoted(value)}"
                 for key, value in target.environment)
    if not target.environment:
        lines.append("environment = []")
    lines.extend([
        "Los digests identifican el estado preparado; no certifican firma,",
        "confianza, ausencia de malware ni aislamiento.",
        "Estas autorizando UNA ejecucion de este target terminal preparado.",
        "Esta fase solo admite handlers de registro/prueba; no lanza procesos.",
        "Para autorizar, escribe AUTORIZAR en el teclado local y presiona Enter.",
        "La voz no concede permisos.",
        "=" * 60,
    ])
    return "\n".join(lines)


def terminal_approval_is_current(orchestrator, action_session,
                                 approval: LocalTerminalApproval) -> bool:
    """Session and stored correspondence check; atomic claim remains separate."""
    if type(approval) is not LocalTerminalApproval:
        return False
    if not approval_session_matches(action_session, approval):
        return False
    pending = orchestrator.preview_pending_approval(
        approval.subject.request_id, step_number=approval.step_number,
    )
    if type(pending) is not PendingApprovalTarget:
        return False
    try:
        _validate_retained_approval(approval)
        validate_terminal_approval_target(
            pending, request_id=approval.subject.request_id,
            step_number=approval.step_number, risk=approval.risk,
        )
    except (TypeError, ValueError):
        return False
    return pending.subject.same_target_as(approval.subject)
