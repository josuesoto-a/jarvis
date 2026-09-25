from uuid import uuid4

import pytest

from core.approval import ApprovalSubject
from core.contracts import RiskLevel
from integrations.local_approval import LocalApproval
from integrations.local_approval_registry import (
    AmbiguousLocalApprovalError,
    InvalidLocalApprovalAdapterError,
    LocalApprovalAdapter,
    LocalApprovalAdapterAlreadyRegisteredError,
    LocalApprovalAdapterNotFoundError,
    LocalApprovalAdapterRegistry,
)


def approval(
    capability="browser",
):
    arguments = (
        {
            "url":
                "https://example.com"
        }
        if capability == "browser"
        else {
            "command":
                "echo hello"
        }
    )

    return LocalApproval(
        call_id="call-1",
        subject=ApprovalSubject(
            request_id=uuid4(),
            step_number=1,
            capability=capability,
            risk=RiskLevel.LOW,
            arguments=arguments,
        ),
    )


def adapter(
    capability="browser",
    *,
    preview=None,
    render=None,
    is_current=None,
):
    return LocalApprovalAdapter(
        capability=capability,
        preview=(
            preview
            if preview is not None
            else lambda orchestrator, projection:
                None
        ),
        render=(
            render
            if render is not None
            else lambda item:
                f"approve {item.capability}"
        ),
        is_current=(
            is_current
            if is_current is not None
            else lambda orchestrator, session, item:
                True
        ),
    )


def test_register_and_get_adapter():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    registered = registry.register(
        adapter("browser")
    )

    assert (
        registry.get("browser")
        is registered
    )

    assert registry.has(
        "BROWSER"
    )


def test_capability_name_is_canonicalized():
    item = adapter(
        " Browser "
    )

    assert (
        item.capability
        == "browser"
    )


def test_duplicate_registration_rejected():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter("browser")
    )

    with pytest.raises(
        LocalApprovalAdapterAlreadyRegisteredError
    ):
        registry.register(
            adapter("BROWSER")
        )


def test_unknown_adapter_rejected():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    with pytest.raises(
        LocalApprovalAdapterNotFoundError
    ):
        registry.get(
            "terminal"
        )


def test_registry_names_are_deterministic():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter("terminal")
    )

    registry.register(
        adapter("browser")
    )

    assert registry.names() == (
        "browser",
        "terminal",
    )

    assert len(registry) == 2


def test_resolve_returns_single_matching_adapter():
    target = approval(
        "browser"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            preview=(
                lambda orchestrator, projection:
                    target
            ),
        )
    )

    registry.register(
        adapter(
            "terminal"
        )
    )

    resolved = registry.resolve(
        object(),
        object(),
    )

    assert resolved is target


def test_resolve_returns_none_when_no_adapter_matches():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter("browser")
    )

    assert (
        registry.resolve(
            object(),
            object(),
        )
        is None
    )


def test_resolve_fails_closed_on_multiple_matches():
    browser_target = approval(
        "browser"
    )

    terminal_target = approval(
        "terminal"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            preview=(
                lambda orchestrator, projection:
                    browser_target
            ),
        )
    )

    registry.register(
        adapter(
            "terminal",
            preview=(
                lambda orchestrator, projection:
                    terminal_target
            ),
        )
    )

    with pytest.raises(
        AmbiguousLocalApprovalError
    ):
        registry.resolve(
            object(),
            object(),
        )


def test_adapter_cannot_claim_different_capability():
    terminal_target = approval(
        "terminal"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            preview=(
                lambda orchestrator, projection:
                    terminal_target
            ),
        )
    )

    with pytest.raises(
        InvalidLocalApprovalAdapterError
    ):
        registry.resolve(
            object(),
            object(),
        )


def test_render_routes_by_subject_capability():
    target = approval(
        "browser"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            render=(
                lambda item:
                    "BROWSER PROMPT"
            ),
        )
    )

    assert (
        registry.render(
            target
        )
        == "BROWSER PROMPT"
    )


def test_is_current_routes_by_subject_capability():
    target = approval(
        "browser"
    )

    calls = []

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            is_current=(
                lambda orchestrator, session, item:
                    calls.append(item)
                    or True
            ),
        )
    )

    assert registry.is_current(
        object(),
        object(),
        target,
    )

    assert calls == [
        target
    ]


def test_non_boolean_current_result_is_rejected():
    target = approval(
        "browser"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            is_current=(
                lambda orchestrator, session, item:
                    "yes"
            ),
        )
    )

    with pytest.raises(
        InvalidLocalApprovalAdapterError
    ):
        registry.is_current(
            object(),
            object(),
            target,
        )


def test_empty_rendered_prompt_is_rejected():
    target = approval(
        "browser"
    )

    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter(
            "browser",
            render=(
                lambda item: "   "
            ),
        )
    )

    with pytest.raises(
        InvalidLocalApprovalAdapterError
    ):
        registry.render(
            target
        )


@pytest.mark.parametrize(
    "field",
    [
        "preview",
        "render",
        "is_current",
    ],
)
def test_non_callable_handler_rejected(
    field,
):
    values = {
        "preview":
            lambda orchestrator, projection:
                None,
        "render":
            lambda item:
                "prompt",
        "is_current":
            lambda orchestrator, session, item:
                True,
    }

    values[field] = (
        "not-callable"
    )

    with pytest.raises(
        InvalidLocalApprovalAdapterError
    ):
        LocalApprovalAdapter(
            capability="browser",
            **values,
        )
