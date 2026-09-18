import pytest

from core.contracts import CapabilitySpec
from core.registry import (
    CapabilityAlreadyRegisteredError,
    CapabilityNotFoundError,
    CapabilityRegistry,
    InvalidCapabilityError,
    canonicalize_capability_name,
)


def test_register_and_get_capability():
    registry = CapabilityRegistry()

    registered = registry.register(
        CapabilitySpec(
            name="weather",
            description="Get current weather information.",
            tags=("network", "read"),
        )
    )

    result = registry.get(
        "weather"
    )

    assert result == registered
    assert result.name == "weather"
    assert result.tags == (
        "network",
        "read",
    )


def test_capability_names_are_canonicalized():
    registry = CapabilityRegistry()

    registered = registry.register(
        CapabilitySpec(
            name="Weather",
            description="Weather lookup",
        )
    )

    assert registered.name == "weather"

    assert registry.has(
        "WEATHER"
    )

    assert registry.get(
        " weather "
    ) == registered


def test_duplicate_registration_is_rejected():
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="weather",
            description="Weather lookup",
        )
    )

    with pytest.raises(
        CapabilityAlreadyRegisteredError
    ):
        registry.register(
            CapabilitySpec(
                name="WEATHER",
                description="Another weather implementation",
            )
        )


def test_unknown_capability_is_rejected():
    registry = CapabilityRegistry()

    with pytest.raises(
        CapabilityNotFoundError
    ):
        registry.get(
            "computer_use"
        )


def test_registry_listing_is_deterministic():
    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            name="terminal",
            description="Execute terminal operations",
        )
    )

    registry.register(
        CapabilitySpec(
            name="weather",
            description="Get weather information",
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description="Navigate web pages",
        )
    )

    assert registry.names() == (
        "browser",
        "terminal",
        "weather",
    )

    assert tuple(
        spec.name
        for spec in registry.list_capabilities()
    ) == (
        "browser",
        "terminal",
        "weather",
    )


def test_invalid_capability_name_is_rejected():
    registry = CapabilityRegistry()

    with pytest.raises(
        InvalidCapabilityError
    ):
        registry.register(
            CapabilitySpec(
                name="web search",
                description="Search the web",
            )
        )


def test_empty_description_is_rejected():
    registry = CapabilityRegistry()

    with pytest.raises(
        InvalidCapabilityError
    ):
        registry.register(
            CapabilitySpec(
                name="weather",
                description="   ",
            )
        )


def test_tags_are_normalized():
    registry = CapabilityRegistry()

    capability = registry.register(
        CapabilitySpec(
            name="weather",
            description="Weather lookup",
            tags=(
                " Network ",
                "READ",
                "",
            ),
        )
    )

    assert capability.tags == (
        "network",
        "read",
    )


def test_registry_length():
    registry = CapabilityRegistry()

    assert len(registry) == 0

    registry.register(
        CapabilitySpec(
            name="weather",
            description="Weather lookup",
        )
    )

    assert len(registry) == 1


def test_canonicalize_capability_name():
    assert canonicalize_capability_name(
        " Weather "
    ) == "weather"

    assert canonicalize_capability_name(
        "WEB_SEARCH"
    ) == "web_search"
