from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.approval import ApprovalSubject
from core.contracts import RiskLevel
from integrations.local_approval import (
    LocalApproval,
    approval_session_matches,
)


def subject(
    *,
    capability="browser",
    step_number=2,
):
    return ApprovalSubject(
        request_id=uuid4(),
        step_number=step_number,
        capability=capability,
        risk=RiskLevel.LOW,
        arguments=(
            {
                "url":
                    "https://example.com"
            }
            if capability == "browser"
            else {
                "command":
                    "echo hello"
            }
        ),
    )


def session_for(
    approval,
    *,
    request_id=None,
    call_id=None,
    step_number=None,
):
    request_id = (
        request_id
        if request_id is not None
        else approval.request_id
    )

    call_id = (
        call_id
        if call_id is not None
        else approval.call_id
    )

    step_number = (
        step_number
        if step_number is not None
        else approval.step_number
    )

    pending = SimpleNamespace(
        request_id=request_id,
        call_id=call_id,
        pending_confirmation_steps=(
            step_number,
        ),
    )

    return SimpleNamespace(
        snapshot=lambda:
            SimpleNamespace(
                action=SimpleNamespace(
                    request_id=request_id,
                    call_id=call_id,
                    pending_permission=pending,
                )
            )
    )


def test_projects_generic_subject_identity():
    target = subject()

    approval = LocalApproval(
        call_id="call-1",
        subject=target,
    )

    assert (
        approval.request_id
        == str(target.request_id)
    )

    assert approval.step_number == 2
    assert approval.capability == "browser"
    assert approval.risk == RiskLevel.LOW

    assert approval.arguments == {
        "url":
            "https://example.com"
    }

    assert (
        approval.fingerprint
        == target.fingerprint
    )


def test_envelope_is_not_browser_specific():
    target = subject(
        capability="terminal",
        step_number=4,
    )

    approval = LocalApproval(
        call_id="call-terminal",
        subject=target,
    )

    assert (
        approval.capability
        == "terminal"
    )

    assert approval.arguments == {
        "command":
            "echo hello"
    }


def test_session_match_requires_exact_transport_identity():
    approval = LocalApproval(
        call_id="call-1",
        subject=subject(),
    )

    assert approval_session_matches(
        session_for(approval),
        approval,
    )

    assert not approval_session_matches(
        session_for(
            approval,
            call_id="different-call",
        ),
        approval,
    )

    assert not approval_session_matches(
        session_for(
            approval,
            request_id=str(
                uuid4()
            ),
        ),
        approval,
    )

    assert not approval_session_matches(
        session_for(
            approval,
            step_number=3,
        ),
        approval,
    )


def test_missing_pending_fails_closed():
    approval = LocalApproval(
        call_id="call-1",
        subject=subject(),
    )

    session = SimpleNamespace(
        snapshot=lambda:
            SimpleNamespace(
                action=SimpleNamespace(
                    request_id=(
                        approval.request_id
                    ),
                    call_id=approval.call_id,
                    pending_permission=None,
                )
            )
    )

    assert not approval_session_matches(
        session,
        approval,
    )


def test_broken_snapshot_fails_closed():
    approval = LocalApproval(
        call_id="call-1",
        subject=subject(),
    )

    session = SimpleNamespace(
        snapshot=lambda:
            (_ for _ in ())
            .throw(
                RuntimeError("boom")
            )
    )

    assert not approval_session_matches(
        session,
        approval,
    )


@pytest.mark.parametrize(
    "call_id",
    [
        "",
        " call",
        "call ",
        "\t",
    ],
)
def test_invalid_call_id_rejected(
    call_id,
):
    with pytest.raises(
        ValueError
    ):
        LocalApproval(
            call_id=call_id,
            subject=subject(),
        )


def test_wrong_subject_type_rejected():
    with pytest.raises(
        TypeError
    ):
        LocalApproval(
            call_id="call-1",
            subject="not-subject",
        )
