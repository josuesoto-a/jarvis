"""
Capability resolution for Jarvis.

The resolver compares capability requirements against the
currently registered capabilities.

It performs no external actions.
"""

from dataclasses import dataclass
from enum import Enum

from core.contracts import CapabilityRequirement
from core.registry import CapabilityRegistry


# ============================================================
# STATUS
# ============================================================

class CapabilityResolutionStatus(str, Enum):
    """
    Result of resolving one capability requirement.
    """

    AVAILABLE = "available"
    MISSING = "missing"


# ============================================================
# SINGLE RESOLUTION
# ============================================================

@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """
    Resolution result for one requirement.
    """

    requirement: CapabilityRequirement

    status: CapabilityResolutionStatus

    capability_name: str | None = None


    @property
    def available(self) -> bool:
        return (
            self.status
            == CapabilityResolutionStatus.AVAILABLE
        )


# ============================================================
# RESOLUTION REPORT
# ============================================================

@dataclass(frozen=True, slots=True)
class CapabilityResolutionReport:
    """
    Resolution result for a group of requirements.
    """

    resolutions: tuple[
        CapabilityResolution,
        ...
    ]


    @property
    def all_required_available(self) -> bool:
        """
        True only when every required capability exists.
        """

        return all(
            resolution.available
            or not resolution.requirement.required
            for resolution in self.resolutions
        )


    @property
    def missing_required(
        self,
    ) -> tuple[CapabilityRequirement, ...]:
        """
        Return only missing required capabilities.
        """

        return tuple(
            resolution.requirement
            for resolution in self.resolutions
            if (
                resolution.requirement.required
                and not resolution.available
            )
        )


    @property
    def missing_optional(
        self,
    ) -> tuple[CapabilityRequirement, ...]:
        """
        Return missing optional capabilities.
        """

        return tuple(
            resolution.requirement
            for resolution in self.resolutions
            if (
                not resolution.requirement.required
                and not resolution.available
            )
        )


# ============================================================
# RESOLVER
# ============================================================

class CapabilityResolver:
    """
    Deterministic resolver backed by a CapabilityRegistry.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
    ) -> None:

        self._registry = registry


    def resolve(
        self,
        requirement: CapabilityRequirement,
    ) -> CapabilityResolution:
        """
        Resolve exactly one capability requirement.
        """

        if self._registry.has(
            requirement.capability
        ):

            capability = self._registry.get(
                requirement.capability
            )

            return CapabilityResolution(
                requirement=requirement,
                status=(
                    CapabilityResolutionStatus.AVAILABLE
                ),
                capability_name=capability.name,
            )


        return CapabilityResolution(
            requirement=requirement,
            status=(
                CapabilityResolutionStatus.MISSING
            ),
            capability_name=None,
        )


    def resolve_all(
        self,
        requirements: tuple[
            CapabilityRequirement,
            ...
        ],
    ) -> CapabilityResolutionReport:
        """
        Resolve an ordered collection of requirements.
        """

        return CapabilityResolutionReport(
            resolutions=tuple(
                self.resolve(requirement)
                for requirement in requirements
            )
        )
