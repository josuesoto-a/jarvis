from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import PermissionMode, RiskLevel
from integrations.local_approval import LocalApproval
from integrations.local_approval_registry import (
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
            "example":
                "value"
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
            else lambda projection, pending_target:
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
            "future_capability"
        )


def test_registry_names_are_deterministic():
    registry = (
        LocalApprovalAdapterRegistry()
    )

    registry.register(
        adapter("future_capability")
    )

    registry.register(
        adapter("browser")
    )

    assert registry.names() == (
        "browser",
        "future_capability",
    )

    assert len(registry) == 2


def resolve_inputs(item):
    target = PendingApprovalTarget(
        subject=item.subject,
        effective_permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
        prepared_target=item.arguments.get("url", "opaque"),
    )
    projection = SimpleNamespace(
        request_id=item.request_id, call_id=item.call_id,
        confirmation_steps=(item.step_number,),
        pending_confirmation_steps=(item.step_number,),
    )
    engine = SimpleNamespace(preview_pending_approval=lambda *a, **k: target)
    return engine, projection


def test_resolve_returns_selected_adapter_approval():
    item = approval()
    registry = LocalApprovalAdapterRegistry()
    registry.register(adapter(preview=lambda projection, pending_target: item))
    assert registry.resolve(*resolve_inputs(item)) is item


def test_resolve_returns_none_when_selected_adapter_declines():
    registry = LocalApprovalAdapterRegistry()
    registry.register(adapter())
    assert registry.resolve(*resolve_inputs(approval())) is None


def test_adapter_cannot_claim_different_capability():
    registry = LocalApprovalAdapterRegistry()
    registry.register(adapter(
        preview=lambda projection, pending_target: approval("future_capability"),
    ))
    with pytest.raises(InvalidLocalApprovalAdapterError):
        registry.resolve(*resolve_inputs(approval()))


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
            lambda projection, pending_target:
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
