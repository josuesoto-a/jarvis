from uuid import uuid4

import pytest

from core.approval import (
    APPROVAL_SUBJECT_VERSION,
    ApprovalContractError,
    ApprovalSubject,
)
from core.contracts import RiskLevel


def make_subject(
    *,
    request_id=None,
    step_number=2,
    capability="browser",
    risk=RiskLevel.LOW,
    arguments=None,
):
    return ApprovalSubject(
        request_id=(
            request_id
            or uuid4()
        ),
        step_number=step_number,
        capability=capability,
        risk=risk,
        arguments=(
            arguments
            if arguments is not None
            else {
                "url":
                    "https://example.com"
            }
        ),
    )


def test_subject_has_stable_version():
    assert (
        APPROVAL_SUBJECT_VERSION
        == 1
    )


def test_argument_order_does_not_change_fingerprint():
    request_id = uuid4()

    first = make_subject(
        request_id=request_id,
        arguments={
            "url":
                "https://example.com",
            "options": {
                "new_tab": True,
                "focus": False,
            },
        },
    )

    second = make_subject(
        request_id=request_id,
        arguments={
            "options": {
                "focus": False,
                "new_tab": True,
            },
            "url":
                "https://example.com",
        },
    )

    assert (
        first.fingerprint
        == second.fingerprint
    )

    assert first.same_target_as(
        second
    )


def test_every_authority_dimension_affects_identity():
    request_id = uuid4()

    base = make_subject(
        request_id=request_id,
    )

    variants = (
        make_subject(),
        make_subject(
            request_id=request_id,
            step_number=3,
        ),
        make_subject(
            request_id=request_id,
            capability="terminal",
        ),
        make_subject(
            request_id=request_id,
            risk=RiskLevel.MEDIUM,
        ),
        make_subject(
            request_id=request_id,
            arguments={
                "url":
                    "https://example.org"
            },
        ),
    )

    for variant in variants:
        assert (
            variant.fingerprint
            != base.fingerprint
        )

        assert not base.same_target_as(
            variant
        )


def test_source_arguments_are_detached():
    request_id = uuid4()

    original = {
        "url":
            "https://example.com",
        "options": {
            "headers": [
                "one",
                "two",
            ],
        },
    }

    subject = make_subject(
        request_id=request_id,
        arguments=original,
    )

    fingerprint = (
        subject.fingerprint
    )

    original["url"] = (
        "https://evil.example"
    )

    original[
        "options"
    ][
        "headers"
    ].append(
        "evil"
    )

    assert subject.arguments == {
        "url":
            "https://example.com",
        "options": {
            "headers": [
                "one",
                "two",
            ],
        },
    }

    assert (
        subject.fingerprint
        == fingerprint
    )


def test_returned_arguments_are_fresh_copies():
    subject = make_subject()

    first = subject.arguments

    first["url"] = (
        "https://evil.example"
    )

    second = subject.arguments

    assert second == {
        "url":
            "https://example.com"
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {1: "not allowed"},
        {
            "value":
                float("nan")
        },
        {
            "value":
                float("inf")
        },
        {
            "value":
                {1, 2, 3}
        },
        {
            "value":
                object()
        },
        {
            "value":
                ("tuple",)
        },
    ],
)
def test_non_json_authority_values_fail_closed(
    arguments,
):
    with pytest.raises(
        ApprovalContractError
    ):
        make_subject(
            arguments=arguments
        )


@pytest.mark.parametrize(
    "step_number",
    [
        0,
        -1,
        True,
        1.0,
        "1",
    ],
)
def test_invalid_step_number_is_rejected(
    step_number,
):
    with pytest.raises(
        ApprovalContractError
    ):
        make_subject(
            step_number=step_number
        )


@pytest.mark.parametrize(
    "capability",
    [
        "",
        " browser",
        "browser ",
        "web search",
        "\tbrowser",
    ],
)
def test_invalid_capability_identity_is_rejected(
    capability,
):
    with pytest.raises(
        ApprovalContractError
    ):
        make_subject(
            capability=capability
        )


def test_wrong_request_id_type_is_rejected():
    with pytest.raises(
        TypeError
    ):
        ApprovalSubject(
            request_id="not-a-uuid",
            step_number=1,
            capability="browser",
            risk=RiskLevel.LOW,
            arguments={
                "url":
                    "https://example.com"
            },
        )


def test_wrong_risk_type_is_rejected():
    with pytest.raises(
        TypeError
    ):
        ApprovalSubject(
            request_id=uuid4(),
            step_number=1,
            capability="browser",
            risk="low",
            arguments={
                "url":
                    "https://example.com"
            },
        )


def test_unicode_arguments_have_stable_identity():
    request_id = uuid4()

    first = make_subject(
        request_id=request_id,
        arguments={
            "query":
                "documentación de Python"
        },
    )

    second = make_subject(
        request_id=request_id,
        arguments={
            "query":
                "documentación de Python"
        },
    )

    assert first.same_target_as(
        second
    )


def test_same_target_rejects_other_types():
    subject = make_subject()

    assert not subject.same_target_as(
        subject.fingerprint
    )
