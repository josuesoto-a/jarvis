from integrations.local_approval_defaults import (
    build_default_local_approval_registry,
)
from integrations.local_browser_approval import (
    approval_is_current,
    format_browser_approval,
    preview_browser_target,
)


def test_default_registry_contains_browser():
    registry = (
        build_default_local_approval_registry()
    )

    assert registry.names() == (
        "browser",
        "terminal",
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
        is preview_browser_target
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

    assert first.get("terminal") is not second.get("terminal")


def test_default_terminal_adapter_is_presentation_only():
    from integrations.local_terminal_approval import (
        format_terminal_approval, preview_terminal_target, terminal_approval_is_current,
    )
    from core.bootstrap import build_default_jarvis

    adapter = build_default_local_approval_registry().get("terminal")
    assert adapter.preview is preview_terminal_target
    assert adapter.render is format_terminal_approval
    assert adapter.is_current is terminal_approval_is_current
    app = build_default_jarvis(client=object())
    assert not app.capability_registry.has("terminal")
    assert not app.runtime_registry.has("terminal")
