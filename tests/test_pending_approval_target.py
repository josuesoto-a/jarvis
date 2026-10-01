from dataclasses import FrozenInstanceError, fields
import os
from pathlib import Path
from uuid import uuid4

import pytest

from capabilities.terminal import (
    TerminalExecutionRequest,
    prepare_terminal_execution,
    terminal_approval_arguments as _terminal_projection,
)
from core.approval import (
    ApprovalContractError,
    ApprovalSubject,
    PendingApprovalTarget,
)
from core.contracts import (
    PermissionMode,
    RiskLevel,
)


def _browser_subject(
    *,
    arguments=None,
) -> ApprovalSubject:
    return ApprovalSubject(
        request_id=uuid4(),
        step_number=2,
        capability='browser',
        risk=RiskLevel.MEDIUM,
        arguments=(
            arguments
            if arguments is not None
            else {
                'url':
                    'https://docs.python.org/3/'
            }
        ),
    )


def test_browser_pending_target_preserves_exact_contract_fields():
    prepared_target = (
        'https://docs.python.org/3/'
    )
    subject = _browser_subject()

    pending = PendingApprovalTarget(
        subject=subject,
        effective_permission=(
            PermissionMode
            .CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=prepared_target,
    )

    assert pending.subject is subject
    assert pending.effective_permission is (
        PermissionMode
        .CONFIRM_BEFORE_EXECUTION
    )
    assert pending.prepared_target is prepared_target
    assert subject.arguments == {
        'url': prepared_target
    }


def test_pending_target_is_frozen_and_has_only_generic_fields():
    pending = PendingApprovalTarget(
        subject=_browser_subject(),
        effective_permission=(
            PermissionMode
            .CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=(
            'https://docs.python.org/3/'
        ),
    )

    assert tuple(
        field.name
        for field in fields(
            PendingApprovalTarget
        )
    ) == (
        'subject',
        'effective_permission',
        'prepared_target',
    )

    with pytest.raises(FrozenInstanceError):
        pending.prepared_target = (
            'https://example.com/'
        )


@pytest.mark.parametrize(
    'permission',
    [
        PermissionMode.AUTOMATIC,
        PermissionMode.FORBIDDEN,
    ],
)
def test_pending_target_rejects_non_confirmation_permissions(
    permission,
):
    with pytest.raises(
        ApprovalContractError,
        match='CONFIRM_BEFORE_EXECUTION',
    ):
        PendingApprovalTarget(
            subject=_browser_subject(),
            effective_permission=permission,
            prepared_target=(
                'https://docs.python.org/3/'
            ),
        )


def test_pending_target_rejects_non_subject_identity():
    with pytest.raises(
        TypeError,
        match='ApprovalSubject',
    ):
        PendingApprovalTarget(
            subject='not-a-subject',
            effective_permission=(
                PermissionMode
                .CONFIRM_BEFORE_EXECUTION
            ),
            prepared_target=(
                'https://docs.python.org/3/'
            ),
        )


def test_identity_remains_in_subject_without_authority_state():
    subject = _browser_subject()
    fingerprint = subject.fingerprint
    pending = PendingApprovalTarget(
        subject=subject,
        effective_permission=(
            PermissionMode
            .CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=(
            'https://docs.python.org/3/'
        ),
    )

    assert pending.subject.request_id == subject.request_id
    assert pending.subject.step_number == 2
    assert pending.subject.capability == 'browser'
    assert pending.subject.risk is RiskLevel.MEDIUM
    assert pending.subject.fingerprint == fingerprint
    assert not hasattr(pending, 'request_id')
    assert not hasattr(pending, 'step_number')
    assert not hasattr(pending, 'capability')
    assert not hasattr(pending, 'risk')
    assert not hasattr(pending, 'fingerprint')
    assert not hasattr(pending, 'authorized')


def test_source_argument_mutation_cannot_change_pending_identity():
    arguments = {
        'url': 'https://docs.python.org/3/',
        'options': {
            'headers': ['Accept: text/html'],
        },
    }
    subject = _browser_subject(
        arguments=arguments
    )
    pending = PendingApprovalTarget(
        subject=subject,
        effective_permission=(
            PermissionMode
            .CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=(
            'https://docs.python.org/3/'
        ),
    )
    fingerprint = (
        pending.subject.fingerprint
    )

    arguments['url'] = (
        'https://evil.example/'
    )
    arguments['options']['headers'].append(
        'X-Evil: true'
    )
    returned = pending.subject.arguments
    returned['url'] = (
        'https://also-evil.example/'
    )

    assert pending.subject.arguments == {
        'url': 'https://docs.python.org/3/',
        'options': {
            'headers': [
                'Accept: text/html'
            ],
        },
    }
    assert (
        pending.subject.fingerprint
        == fingerprint
    )


@pytest.mark.skipif(
    os.name != 'nt',
    reason='D1F-A target is Windows-native',
)
def test_terminal_semantic_change_changes_approval_identity(
    tmp_path: Path,
):
    executable = tmp_path / 'fixture.exe'
    executable.write_bytes(b'contract fixture; never executed')
    first_target = prepare_terminal_execution(
        TerminalExecutionRequest(
            executable=str(executable),
            argv=['--version'],
            cwd=str(tmp_path),
        )
    )
    changed_target = prepare_terminal_execution(
        TerminalExecutionRequest(
            executable=str(executable),
            argv=['-I', '--version'],
            cwd=str(tmp_path),
        )
    )
    first_projection = _terminal_projection(
        first_target
    )
    changed_projection = _terminal_projection(
        changed_target
    )
    request_id = uuid4()
    first_subject = ApprovalSubject(
        request_id=request_id,
        step_number=1,
        capability='terminal',
        risk=RiskLevel.HIGH,
        arguments=first_projection,
    )
    changed_subject = ApprovalSubject(
        request_id=request_id,
        step_number=1,
        capability='terminal',
        risk=RiskLevel.HIGH,
        arguments=changed_projection,
    )
    pending = PendingApprovalTarget(
        subject=first_subject,
        effective_permission=(
            PermissionMode
            .CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=first_target,
    )

    assert pending.prepared_target is first_target
    assert pending.subject.arguments == first_projection
    assert (
        first_projection['argv']
        != changed_projection['argv']
    )
    assert {
        key: value
        for key, value in first_projection.items()
        if key != 'argv'
    } == {
        key: value
        for key, value in changed_projection.items()
        if key != 'argv'
    }
    assert (
        first_subject.fingerprint
        != changed_subject.fingerprint
    )

    with pytest.raises(FrozenInstanceError):
        pending.prepared_target.argv = ()
