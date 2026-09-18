"""
Capability Registry for Jarvis.

The registry answers a simple question:

    What can Jarvis currently do?

It does not execute capabilities.

It only stores and retrieves immutable capability specifications.
"""

from dataclasses import replace
import re

from core.contracts import CapabilitySpec


# ============================================================
# ERRORS
# ============================================================

class CapabilityRegistryError(Exception):
    """
    Base error for capability registry operations.
    """


class InvalidCapabilityError(CapabilityRegistryError):
    """
    Raised when a capability specification is invalid.
    """


class CapabilityAlreadyRegisteredError(
    CapabilityRegistryError
):
    """
    Raised when a capability is registered more than once.
    """


class CapabilityNotFoundError(
    CapabilityRegistryError
):
    """
    Raised when a requested capability does not exist.
    """


# ============================================================
# NAME RULES
# ============================================================

_CAPABILITY_NAME_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*$"
)


def canonicalize_capability_name(
    name: str,
) -> str:
    """
    Convert an external capability name into its canonical ID.

    Examples:

        Weather
            -> weather

        WEB_SEARCH
            -> web_search

        " terminal "
            -> terminal

    Spaces and hyphens are intentionally not silently converted.
    Internal capability IDs must remain explicit and stable.
    """

    if not isinstance(name, str):
        raise InvalidCapabilityError(
            "Capability name must be a string."
        )

    canonical = name.strip().lower()

    if not canonical:
        raise InvalidCapabilityError(
            "Capability name cannot be empty."
        )

    if not _CAPABILITY_NAME_PATTERN.fullmatch(
        canonical
    ):
        raise InvalidCapabilityError(
            "Capability name must start with a letter and "
            "contain only lowercase letters, digits, or underscores."
        )

    return canonical


# ============================================================
# VALIDATION
# ============================================================

def _validate_spec(
    spec: CapabilitySpec,
) -> CapabilitySpec:
    """
    Validate a CapabilitySpec and return a normalized immutable copy.
    """

    if not isinstance(
        spec,
        CapabilitySpec,
    ):
        raise InvalidCapabilityError(
            "Registry entries must be CapabilitySpec objects."
        )

    canonical_name = canonicalize_capability_name(
        spec.name
    )

    description = spec.description.strip()

    if not description:
        raise InvalidCapabilityError(
            "Capability description cannot be empty."
        )

    version = spec.version.strip()

    if not version:
        raise InvalidCapabilityError(
            "Capability version cannot be empty."
        )

    normalized_tags = tuple(
        tag.strip().lower()
        for tag in spec.tags
        if tag.strip()
    )

    return replace(
        spec,
        name=canonical_name,
        description=description,
        version=version,
        tags=normalized_tags,
    )


# ============================================================
# REGISTRY
# ============================================================

class CapabilityRegistry:
    """
    Deterministic in-memory registry of Jarvis capabilities.

    Registration is explicit.

    Duplicate registrations are rejected.

    Listing is always sorted by canonical capability name.
    """

    def __init__(self) -> None:

        self._capabilities: dict[
            str,
            CapabilitySpec,
        ] = {}


    # --------------------------------------------------------
    # REGISTER
    # --------------------------------------------------------

    def register(
        self,
        spec: CapabilitySpec,
    ) -> CapabilitySpec:
        """
        Register exactly one capability.

        Returns the normalized specification.
        """

        normalized = _validate_spec(
            spec
        )

        if normalized.name in self._capabilities:

            raise CapabilityAlreadyRegisteredError(
                f"Capability already registered: "
                f"{normalized.name}"
            )

        self._capabilities[
            normalized.name
        ] = normalized

        return normalized


    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def get(
        self,
        name: str,
    ) -> CapabilitySpec:
        """
        Retrieve one registered capability.
        """

        canonical = canonicalize_capability_name(
            name
        )

        try:

            return self._capabilities[
                canonical
            ]

        except KeyError as error:

            raise CapabilityNotFoundError(
                f"Capability not registered: "
                f"{canonical}"
            ) from error


    # --------------------------------------------------------
    # HAS
    # --------------------------------------------------------

    def has(
        self,
        name: str,
    ) -> bool:
        """
        Return True when a capability exists.
        """

        canonical = canonicalize_capability_name(
            name
        )

        return (
            canonical
            in self._capabilities
        )


    # --------------------------------------------------------
    # LIST
    # --------------------------------------------------------

    def list_capabilities(
        self,
    ) -> tuple[CapabilitySpec, ...]:
        """
        Return all registered capabilities in deterministic order.
        """

        return tuple(
            self._capabilities[name]
            for name in sorted(
                self._capabilities
            )
        )


    # --------------------------------------------------------
    # NAMES
    # --------------------------------------------------------

    def names(
        self,
    ) -> tuple[str, ...]:
        """
        Return registered capability IDs in deterministic order.
        """

        return tuple(
            sorted(
                self._capabilities
            )
        )


    # --------------------------------------------------------
    # SIZE
    # --------------------------------------------------------

    def __len__(
        self,
    ) -> int:

        return len(
            self._capabilities
        )
