from types import SimpleNamespace
from uuid import uuid4

from core.approval import ApprovalSubject
from core.contracts import RiskLevel
from integrations.local_browser_approval import (
    approval_is_current,
    preview_browser_approval,
    preview_checkpoint_browser_approval,
)
from integrations.openai_live import (
    PendingPermissionUpdate,
)


URL = "https://docs.python.org/3/"


def projection(
    request_id,
    *,
    call_id="call-1",
    step_number=1,
):
    return PendingPermissionUpdate(
        delegation_id="delegation-1",
        call_id=call_id,
        request_id=str(request_id),
        confirmation_steps=(
            step_number,
        ),
        pending_confirmation_steps=(
            step_number,
        ),
        message="Permission required",
    )


def fake_orchestrator(
    *,
    literal=URL,
    checkpoint=URL,
):
    return SimpleNamespace(
        preview_single_browser=(
            lambda request_id:
                literal
        ),
        preview_checkpoint_browser=(
            lambda request_id, *, step_number:
                checkpoint
        ),
    )


def session(
    request_id,
    pending,
):
    return SimpleNamespace(
        snapshot=lambda:
            SimpleNamespace(
                action=SimpleNamespace(
                    request_id=str(
                        request_id
                    ),
                    call_id=(
                        pending.call_id
                    ),
                    pending_permission=(
                        pending
                    ),
                )
            )
    )


def test_literal_preview_is_backed_by_generic_subject():
    request_id = uuid4()
    pending = projection(
        request_id
    )

    approval = preview_browser_approval(
        fake_orchestrator(),
        pending,
    )

    assert approval is not None

    assert isinstance(
        approval.subject,
        ApprovalSubject,
    )

    assert (
        approval.subject.request_id
        == request_id
    )

    assert (
        approval.subject.step_number
        == 1
    )

    assert (
        approval.subject.capability
        == "browser"
    )

    assert (
        approval.subject.risk
        == RiskLevel.LOW
    )

    assert approval.subject.arguments == {
        "url": URL,
    }

    # Existing external convenience API remains unchanged.
    assert (
        approval.request_id
        == str(request_id)
    )

    assert approval.step_number == 1
    assert approval.url == URL


def test_checkpoint_preview_uses_same_generic_contract():
    request_id = uuid4()

    pending = projection(
        request_id,
        step_number=2,
    )

    approval = (
        preview_checkpoint_browser_approval(
            fake_orchestrator(),
            pending,
        )
    )

    assert approval is not None

    assert (
        approval.subject.request_id
        == request_id
    )

    assert (
        approval.subject.step_number
        == 2
    )

    assert (
        approval.subject.capability
        == "browser"
    )

    assert approval.subject.arguments == {
        "url": URL,
    }


def test_call_id_is_transport_identity_not_subject_identity():
    request_id = uuid4()

    first = preview_browser_approval(
        fake_orchestrator(),
        projection(
            request_id,
            call_id="call-one",
        ),
    )

    second = preview_browser_approval(
        fake_orchestrator(),
        projection(
            request_id,
            call_id="call-two",
        ),
    )

    assert first is not None
    assert second is not None

    assert (
        first.call_id
        != second.call_id
    )

    assert (
        first.fingerprint
        == second.fingerprint
    )

    assert (
        first.subject.same_target_as(
            second.subject
        )
    )


def test_changed_current_url_invalidates_approval_subject():
    request_id = uuid4()

    pending = projection(
        request_id
    )

    approval = preview_browser_approval(
        fake_orchestrator(
            literal=URL
        ),
        pending,
    )

    assert approval is not None

    changed = fake_orchestrator(
        literal=(
            "https://example.com/changed"
        )
    )

    assert not approval_is_current(
        changed,
        session(
            request_id,
            pending,
        ),
        approval,
    )


def test_call_id_mismatch_still_fails_before_subject_match():
    request_id = uuid4()

    original = projection(
        request_id,
        call_id="original-call",
    )

    approval = preview_browser_approval(
        fake_orchestrator(),
        original,
    )

    assert approval is not None

    wrong = projection(
        request_id,
        call_id="wrong-call",
    )

    assert not approval_is_current(
        fake_orchestrator(),
        session(
            request_id,
            wrong,
        ),
        approval,
    )
