"""
Runtime capability bindings for Jarvis.

The CapabilityRegistry describes what capabilities exist.

The CapabilityRuntimeRegistry binds those capability IDs to executable
Python handlers.

This module does not decide permissions and does not execute plans.
"""

from collections.abc import Callable, Mapping
from typing import Any

from core.registry import (
    canonicalize_capability_name,
)


# ============================================================
# TYPES
# ============================================================

CapabilityHandler = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any],
]


# ============================================================
# ERRORS
# ============================================================

class CapabilityRuntimeError(Exception):
    """
    Base error for runtime capability bindings.
    """


class RuntimeAlreadyRegisteredError(
    CapabilityRuntimeError
):
    """
    Raised when an implementation is registered twice.
    """


class RuntimeNotFoundError(
    CapabilityRuntimeError
):
    """
    Raised when no runtime implementation exists.
    """


class InvalidRuntimeHandlerError(
    CapabilityRuntimeError
):
    """
    Raised when the supplied handler is not callable.
    """


# ============================================================
# RUNTIME REGISTRY
# ============================================================

class CapabilityRuntimeRegistry:
    """
    Deterministic mapping from capability IDs to Python handlers.

    Example:

        web_search -> search_web
        weather    -> get_weather
    """

    def __init__(self) -> None:

        self._handlers: dict[
            str,
            CapabilityHandler,
        ] = {}


    def register(
        self,
        name: str,
        handler: CapabilityHandler,
    ) -> None:
        """
        Bind one capability ID to one executable handler.
        """

        canonical = canonicalize_capability_name(
            name
        )

        if not callable(handler):

            raise InvalidRuntimeHandlerError(
                f"Handler for {canonical} must be callable."
            )

        if canonical in self._handlers:

            raise RuntimeAlreadyRegisteredError(
                f"Runtime already registered: {canonical}"
            )

        self._handlers[
            canonical
        ] = handler


    def get(
        self,
        name: str,
    ) -> CapabilityHandler:
        """
        Retrieve one executable handler.
        """

        canonical = canonicalize_capability_name(
            name
        )

        try:

            return self._handlers[
                canonical
            ]

        except KeyError as error:

            raise RuntimeNotFoundError(
                f"No runtime registered for: {canonical}"
            ) from error


    def has(
        self,
        name: str,
    ) -> bool:
        """
        Return whether an implementation exists.
        """

        canonical = canonicalize_capability_name(
            name
        )

        return canonical in self._handlers


    def names(
        self,
    ) -> tuple[str, ...]:
        """
        Return runtime IDs in deterministic order.
        """

        return tuple(
            sorted(
                self._handlers
            )
        )


    def __len__(
        self,
    ) -> int:

        return len(
            self._handlers
        )
