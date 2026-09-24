"""Generic human-approval identity contract for Jarvis.

This module does not grant permission, resume execution, inspect Live
sessions, or know how a capability should be presented to a human.

ApprovalSubject answers one question only:

    "What exact execution target is the human being asked to approve?"

The fingerprint is an integrity/identity token, not authentication and
not authority by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import math
from typing import Any
from uuid import UUID

from core.contracts import RiskLevel


APPROVAL_SUBJECT_VERSION = 1


class ApprovalContractError(
    ValueError
):
    """The requested approval target cannot be represented safely."""


def _normalize_json_value(
    value: Any,
    *,
    path: str,
) -> Any:
    """Detach and validate one JSON-like authority value."""

    if value is None:
        return None

    if type(value) is bool:
        return value

    if type(value) is int:
        return value

    if type(value) is float:
        if not math.isfinite(value):
            raise ApprovalContractError(
                f"{path} contains a non-finite float."
            )

        return value

    if type(value) is str:
        return value

    if type(value) is list:
        return [
            _normalize_json_value(
                item,
                path=f"{path}[{index}]",
            )
            for index, item
            in enumerate(value)
        ]

    if isinstance(
        value,
        Mapping,
    ):
        normalized: dict[
            str,
            Any,
        ] = {}

        for key, item in value.items():

            if type(key) is not str:
                raise ApprovalContractError(
                    f"{path} contains a non-string key."
                )

            normalized[key] = (
                _normalize_json_value(
                    item,
                    path=(
                        f"{path}.{key}"
                    ),
                )
            )

        return normalized

    raise ApprovalContractError(
        f"{path} contains unsupported type "
        f"{type(value).__name__}."
    )


def _canonical_json(
    value: Any,
) -> str:
    """Produce stable UTF-8 JSON identity text."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    except (
        TypeError,
        ValueError,
    ) as error:
        raise ApprovalContractError(
            "Approval target cannot be "
            "serialized canonically."
        ) from error


@dataclass(
    frozen=True,
    slots=True,
    init=False,
)
class ApprovalSubject:
    """Immutable identity of one exact step awaiting human approval.

    The subject deliberately excludes transport/session details such as
    GPT-Live call_id. Those belong to the interaction layer.

    Likewise, the fingerprint does not grant authority. A higher layer
    must still verify that this subject is current and then ask the
    Orchestrator to perform its normal atomic confirmation/resume flow.
    """

    request_id: UUID
    step_number: int
    capability: str
    risk: RiskLevel

    _canonical_arguments_json: str

    fingerprint: str

    def __init__(
        self,
        *,
        request_id: UUID,
        step_number: int,
        capability: str,
        risk: RiskLevel,
        arguments: Mapping[
            str,
            Any,
        ],
    ) -> None:

        if not isinstance(
            request_id,
            UUID,
        ):
            raise TypeError(
                "request_id must be a UUID"
            )

        if (
            type(step_number) is not int
            or step_number < 1
        ):
            raise ApprovalContractError(
                "step_number must be a "
                "positive integer."
            )

        if (
            type(capability) is not str
            or not capability
            or capability.strip()
            != capability
            or any(
                character.isspace()
                for character
                in capability
            )
        ):
            raise ApprovalContractError(
                "capability must be a "
                "nonempty token without whitespace."
            )

        if not isinstance(
            risk,
            RiskLevel,
        ):
            raise TypeError(
                "risk must be a RiskLevel"
            )

        if not isinstance(
            arguments,
            Mapping,
        ):
            raise TypeError(
                "arguments must be a mapping"
            )

        normalized_arguments = (
            _normalize_json_value(
                arguments,
                path="arguments",
            )
        )

        if not isinstance(
            normalized_arguments,
            dict,
        ):
            raise ApprovalContractError(
                "arguments must normalize "
                "to an object."
            )

        canonical_arguments = (
            _canonical_json(
                normalized_arguments
            )
        )

        identity = {
            "version":
                APPROVAL_SUBJECT_VERSION,
            "request_id":
                str(request_id),
            "step_number":
                step_number,
            "capability":
                capability,
            "risk":
                risk.value,
            "arguments":
                normalized_arguments,
        }

        canonical_identity = (
            _canonical_json(
                identity
            )
        )

        fingerprint = (
            "sha256:"
            + sha256(
                canonical_identity.encode(
                    "utf-8"
                )
            ).hexdigest()
        )

        object.__setattr__(
            self,
            "request_id",
            request_id,
        )

        object.__setattr__(
            self,
            "step_number",
            step_number,
        )

        object.__setattr__(
            self,
            "capability",
            capability,
        )

        object.__setattr__(
            self,
            "risk",
            risk,
        )

        object.__setattr__(
            self,
            "_canonical_arguments_json",
            canonical_arguments,
        )

        object.__setattr__(
            self,
            "fingerprint",
            fingerprint,
        )

    @property
    def arguments(
        self,
    ) -> dict[str, Any]:
        """Return a fresh detached copy of the exact arguments."""

        value = json.loads(
            self._canonical_arguments_json
        )

        assert isinstance(
            value,
            dict,
        )

        return value

    @property
    def canonical_arguments_json(
        self,
    ) -> str:
        """Stable representation used by the approval identity."""

        return (
            self
            ._canonical_arguments_json
        )

    def same_target_as(
        self,
        other: object,
    ) -> bool:
        """Constant-time comparison of two approval identities."""

        if not isinstance(
            other,
            ApprovalSubject,
        ):
            return False

        return hmac.compare_digest(
            self.fingerprint,
            other.fingerprint,
        )
