from core.contracts import (
    CapabilityRequirement,
    CapabilitySpec,
)
from core.registry import CapabilityRegistry
from core.resolver import (
    CapabilityResolutionStatus,
    CapabilityResolver,
)


def build_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="weather",
            description="Weather lookup",
        )
    )

    registry.register(
        CapabilitySpec(
            name="web_search",
            description="Search the web",
        )
    )

    return registry


def test_available_capability_resolves():
    resolver = CapabilityResolver(
        build_registry()
    )

    requirement = CapabilityRequirement(
        capability="weather",
        reason="Need current weather",
    )

    result = resolver.resolve(
        requirement
    )

    assert result.status == (
        CapabilityResolutionStatus.AVAILABLE
    )

    assert result.available is True
    assert result.capability_name == "weather"


def test_missing_capability_resolves_as_missing():
    resolver = CapabilityResolver(
        build_registry()
    )

    requirement = CapabilityRequirement(
        capability="computer_use",
        reason="Need GUI interaction",
    )

    result = resolver.resolve(
        requirement
    )

    assert result.status == (
        CapabilityResolutionStatus.MISSING
    )

    assert result.available is False
    assert result.capability_name is None


def test_all_required_capabilities_available():
    resolver = CapabilityResolver(
        build_registry()
    )

    report = resolver.resolve_all(
        (
            CapabilityRequirement(
                capability="weather",
                reason="Need weather",
            ),
            CapabilityRequirement(
                capability="web_search",
                reason="Need search",
            ),
        )
    )

    assert (
        report.all_required_available
        is True
    )

    assert report.missing_required == ()


def test_missing_required_blocks_report():
    resolver = CapabilityResolver(
        build_registry()
    )

    report = resolver.resolve_all(
        (
            CapabilityRequirement(
                capability="weather",
                reason="Need weather",
            ),
            CapabilityRequirement(
                capability="computer_use",
                reason="Need GUI",
            ),
        )
    )

    assert (
        report.all_required_available
        is False
    )

    assert tuple(
        requirement.capability
        for requirement
        in report.missing_required
    ) == (
        "computer_use",
    )


def test_missing_optional_does_not_block_report():
    resolver = CapabilityResolver(
        build_registry()
    )

    report = resolver.resolve_all(
        (
            CapabilityRequirement(
                capability="weather",
                reason="Need weather",
            ),
            CapabilityRequirement(
                capability="browser",
                reason="Could provide additional context",
                required=False,
            ),
        )
    )

    assert (
        report.all_required_available
        is True
    )

    assert report.missing_required == ()

    assert tuple(
        requirement.capability
        for requirement
        in report.missing_optional
    ) == (
        "browser",
    )


def test_resolution_order_is_preserved():
    resolver = CapabilityResolver(
        build_registry()
    )

    report = resolver.resolve_all(
        (
            CapabilityRequirement(
                capability="web_search",
                reason="First",
            ),
            CapabilityRequirement(
                capability="computer_use",
                reason="Second",
            ),
            CapabilityRequirement(
                capability="weather",
                reason="Third",
            ),
        )
    )

    assert tuple(
        resolution.requirement.capability
        for resolution
        in report.resolutions
    ) == (
        "web_search",
        "computer_use",
        "weather",
    )
