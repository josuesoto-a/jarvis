from integrations.local_approval_defaults import (
    build_default_local_approval_registry,
)
from integrations.local_browser_approval import (
    approval_is_current,
    format_browser_approval,
    preview_browser_approval_v2,
)


def test_default_registry_contains_browser():
    registry = (
        build_default_local_approval_registry()
    )

    assert registry.names() == (
        "browser",
    )


def test_default_browser_adapter_uses_existing_authority_surface():
    registry = (
        build_default_local_approval_registry()
    )

    adapter = registry.get(
        "browser"
    )

    assert (
        adapter.preview
        is preview_browser_approval_v2
    )

    assert (
        adapter.render
        is format_browser_approval
    )

    assert (
        adapter.is_current
        is approval_is_current
    )


def test_default_registry_instances_are_independent():
    first = (
        build_default_local_approval_registry()
    )

    second = (
        build_default_local_approval_registry()
    )

    assert first is not second

    assert (
        first.get("browser")
        is not second.get("browser")
    )
