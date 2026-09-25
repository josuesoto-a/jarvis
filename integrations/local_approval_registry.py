"""Capability-agnostic registry for local human approval adapters.

Each adapter owns capability-specific behavior:

- preview: resolve an exact LocalApproval from current pending state
- render: produce the human-readable approval prompt
- is_current: revalidate the exact target immediately before confirmation

The registry owns routing only. It never grants permission and never resumes
execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.registry import (
    canonicalize_capability_name,
)
from integrations.local_approval import (
    LocalApproval,
)


PreviewHandler = Callable[
    [Any, Any],
    LocalApproval | None,
]

RenderHandler = Callable[
    [LocalApproval],
    str,
]

CurrentHandler = Callable[
    [Any, Any, LocalApproval],
    bool,
]


class LocalApprovalRegistryError(
    RuntimeError
):
    """Base error for local approval adapter routing."""


class InvalidLocalApprovalAdapterError(
    LocalApprovalRegistryError
):
    """Adapter configuration or behavior is invalid."""


class LocalApprovalAdapterAlreadyRegisteredError(
    LocalApprovalRegistryError
):
    """A capability already has a local approval adapter."""


class LocalApprovalAdapterNotFoundError(
    LocalApprovalRegistryError
):
    """No local approval adapter exists for this capability."""


class AmbiguousLocalApprovalError(
    LocalApprovalRegistryError
):
    """More than one adapter claimed the same pending approval."""


@dataclass(
    frozen=True,
    slots=True,
)
class LocalApprovalAdapter:
    """Capability-specific approval behavior behind a generic contract."""

    capability: str
    preview: PreviewHandler
    render: RenderHandler
    is_current: CurrentHandler

    def __post_init__(
        self,
    ) -> None:

        try:
            canonical = (
                canonicalize_capability_name(
                    self.capability
                )
            )

        except Exception as error:
            raise InvalidLocalApprovalAdapterError(
                "Invalid approval adapter capability."
            ) from error

        for (
            name,
            handler,
        ) in (
            ("preview", self.preview),
            ("render", self.render),
            ("is_current", self.is_current),
        ):
            if not callable(handler):
                raise InvalidLocalApprovalAdapterError(
                    f"{name} handler must be callable."
                )

        object.__setattr__(
            self,
            "capability",
            canonical,
        )


class LocalApprovalAdapterRegistry:
    """Deterministic registry and router for approval adapters."""

    def __init__(
        self,
    ) -> None:

        self._adapters: dict[
            str,
            LocalApprovalAdapter,
        ] = {}

    def register(
        self,
        adapter: LocalApprovalAdapter,
    ) -> LocalApprovalAdapter:

        if not isinstance(
            adapter,
            LocalApprovalAdapter,
        ):
            raise TypeError(
                "adapter must be a LocalApprovalAdapter"
            )

        name = adapter.capability

        if name in self._adapters:
            raise (
                LocalApprovalAdapterAlreadyRegisteredError(
                    f"Local approval adapter already "
                    f"registered: {name}"
                )
            )

        self._adapters[name] = adapter

        return adapter

    def has(
        self,
        capability: str,
    ) -> bool:

        try:
            name = (
                canonicalize_capability_name(
                    capability
                )
            )

        except Exception:
            return False

        return name in self._adapters

    def get(
        self,
        capability: str,
    ) -> LocalApprovalAdapter:

        try:
            name = (
                canonicalize_capability_name(
                    capability
                )
            )

        except Exception as error:
            raise LocalApprovalAdapterNotFoundError(
                "Invalid or unknown local approval capability."
            ) from error

        try:
            return self._adapters[
                name
            ]

        except KeyError as error:
            raise LocalApprovalAdapterNotFoundError(
                f"No local approval adapter registered "
                f"for: {name}"
            ) from error

    def names(
        self,
    ) -> tuple[str, ...]:

        return tuple(
            sorted(
                self._adapters
            )
        )

    def __len__(
        self,
    ) -> int:

        return len(
            self._adapters
        )

    def resolve(
        self,
        orchestrator,
        projection,
    ) -> LocalApproval | None:
        """Ask registered adapters who owns this pending approval.

        Zero matches means the pending action has no supported local surface.
        More than one match fails closed as an architectural ambiguity.
        """

        matches: list[
            LocalApproval
        ] = []

        for name in self.names():

            adapter = self._adapters[
                name
            ]

            approval = adapter.preview(
                orchestrator,
                projection,
            )

            if approval is None:
                continue

            if not isinstance(
                approval,
                LocalApproval,
            ):
                raise InvalidLocalApprovalAdapterError(
                    f"Adapter {name!r} returned "
                    "a non-LocalApproval value."
                )

            if (
                approval.capability
                != name
            ):
                raise InvalidLocalApprovalAdapterError(
                    f"Adapter {name!r} returned "
                    f"approval for "
                    f"{approval.capability!r}."
                )

            matches.append(
                approval
            )

        if len(matches) > 1:
            raise AmbiguousLocalApprovalError(
                "Multiple local approval adapters "
                "claimed the same pending action."
            )

        if not matches:
            return None

        return matches[0]

    def render(
        self,
        approval: LocalApproval,
    ) -> str:

        if not isinstance(
            approval,
            LocalApproval,
        ):
            raise TypeError(
                "approval must be a LocalApproval"
            )

        adapter = self.get(
            approval.capability
        )

        rendered = adapter.render(
            approval
        )

        if (
            type(rendered) is not str
            or not rendered.strip()
        ):
            raise InvalidLocalApprovalAdapterError(
                f"Adapter {adapter.capability!r} "
                "returned an invalid rendered prompt."
            )

        return rendered

    def is_current(
        self,
        orchestrator,
        action_session,
        approval: LocalApproval,
    ) -> bool:

        if not isinstance(
            approval,
            LocalApproval,
        ):
            raise TypeError(
                "approval must be a LocalApproval"
            )

        adapter = self.get(
            approval.capability
        )

        result = adapter.is_current(
            orchestrator,
            action_session,
            approval,
        )

        if type(result) is not bool:
            raise InvalidLocalApprovalAdapterError(
                f"Adapter {adapter.capability!r} "
                "returned a non-boolean current-state result."
            )

        return result
