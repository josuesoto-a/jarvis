from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.action_worker import ActionWorker
from core.approval import (
    ApprovalSubject,
    PendingApprovalTarget,
)
from core.contracts import (
    ActionRequest,
    ActionStatus,
    CapabilitySpec,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.executor import Executor
from core.orchestrator import (
    OrchestrationResult,
    Orchestrator,
)
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from integrations.openai_live_coordinator import (
    LiveActionCoordinator,
)
from integrations.openai_live_voice import (
    VoiceActionBridge,
)
from integrations.openai_live_voice_session import (
    RearmingVoiceActionSession,
)


LIMIT = 3


class _StaticPlanner:
    def __init__(self, plan):
        self.plan_value = plan
        self.calls = []

    def plan(self, request):
        self.calls.append(request)
        return self.plan_value


def _build_system(*, handler=None):
    calls = []

    if handler is None:
        def handler(arguments):
            calls.append(dict(arguments))
            return {"opened_url": arguments["url"]}

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "browser",
            "Open an exact browser URL",
        )
    )

    runtimes = CapabilityRuntimeRegistry()
    runtimes.register("browser", handler)

    permissions = PermissionEngine()
    validator = PlanValidator(
        CapabilityResolver(registry)
    )
    executor = Executor(
        validator=validator,
        permission_engine=permissions,
        runtime_registry=runtimes,
    )
    request = ActionRequest(
        "Open documentation",
        "Open documentation",
    )
    plan = ExecutionPlan(
        request.request_id,
        (
            ExecutionStep(
                1,
                "Open documentation",
                "browser",
                arguments={
                    "url": ExecutionArgument.literal(
                        "https://docs.python.org/3/"
                    ),
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.AUTOMATIC,
            ),
        ),
        RiskLevel.LOW,
    )
    planner = _StaticPlanner(plan)
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )

    return SimpleNamespace(
        calls=calls,
        executor=executor,
        orchestrator=orchestrator,
        permissions=permissions,
        plan=plan,
        planner=planner,
        request=request,
        validator=validator,
    )


def _subject(
    setup,
    *,
    request_id=None,
    step_number=1,
    url="https://docs.python.org/3/",
):
    return ApprovalSubject(
        request_id=(
            setup.request.request_id
            if request_id is None
            else request_id
        ),
        step_number=step_number,
        capability="browser",
        risk=RiskLevel.LOW,
        arguments={"url": url},
    )


def _attach_target(
    setup,
    subject,
):
    target = PendingApprovalTarget(
        subject=subject,
        effective_permission=(
            PermissionMode.CONFIRM_BEFORE_EXECUTION
        ),
        prepared_target=subject.arguments["url"],
    )

    with setup.orchestrator._lock:
        pending = setup.orchestrator._pending[
            setup.request.request_id
        ]
        identity_bound = replace(
            pending,
            pending_approval_target=target,
        )
        setup.orchestrator._pending[
            setup.request.request_id
        ] = identity_bound

    return identity_bound


def _run_identity_bound(setup):
    waiting = setup.orchestrator.run(
        setup.request
    )
    assert waiting.waiting_for_permission
    subject = _subject(setup)
    pending = _attach_target(
        setup,
        subject,
    )
    return subject, pending


def _resume(
    setup,
    subject,
    *,
    confirmed_steps=frozenset({1}),
):
    return setup.orchestrator.resume(
        setup.request.request_id,
        confirmed_steps=confirmed_steps,
        expected_subject=subject,
    )


def test_matching_subject_claims_and_consumes_once():
    setup = _build_system()
    subject, _ = _run_identity_bound(setup)

    result = _resume(setup, subject)

    assert result.completed
    assert setup.calls == [
        {"url": "https://docs.python.org/3/"}
    ]
    assert (
        setup.request.request_id
        not in setup.orchestrator._pending
    )

    replay = _resume(setup, subject)
    assert replay.status is ActionStatus.FAILED
    assert len(setup.calls) == 1


def test_wrong_subject_is_rejected_before_pending_deletion():
    setup = _build_system()
    current, pending = _run_identity_bound(setup)
    wrong = _subject(
        setup,
        url="https://example.com/different",
    )

    with pytest.raises(
        ValueError,
        match="does not match",
    ):
        _resume(setup, wrong)

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert setup.calls == []

    assert _resume(setup, current).completed


def test_missing_subject_is_rejected_without_consumption():
    setup = _build_system()
    subject, pending = _run_identity_bound(setup)

    with pytest.raises(
        ValueError,
        match="required",
    ):
        setup.orchestrator.resume(
            setup.request.request_id,
            confirmed_steps=frozenset({1}),
        )

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert _resume(setup, subject).completed


def test_non_subject_identity_input_is_rejected_without_consumption():
    setup = _build_system()
    _, pending = _run_identity_bound(setup)

    with pytest.raises(
        TypeError,
        match="ApprovalSubject",
    ):
        setup.orchestrator.resume(
            setup.request.request_id,
            confirmed_steps=frozenset({1}),
            expected_subject=(
                "sha256:not-an-approval-subject"
            ),
        )

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert setup.calls == []


def test_subject_for_another_request_is_rejected():
    setup = _build_system()
    current, pending = _run_identity_bound(setup)
    other_request = _subject(
        setup,
        request_id=uuid4(),
    )

    with pytest.raises(
        ValueError,
        match="request_id",
    ):
        _resume(setup, other_request)

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert _resume(setup, current).completed


@pytest.mark.parametrize(
    ("subject_step", "confirmed_steps"),
    [
        (2, frozenset({1})),
        (1, frozenset({1, 2})),
    ],
)
def test_one_subject_cannot_claim_a_different_or_multiple_steps(
    subject_step,
    confirmed_steps,
):
    setup = _build_system()
    current, pending = _run_identity_bound(setup)
    inconsistent = _subject(
        setup,
        step_number=subject_step,
    )

    with pytest.raises(ValueError):
        _resume(
            setup,
            inconsistent,
            confirmed_steps=confirmed_steps,
        )

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert _resume(setup, current).completed


def test_pending_subject_request_must_match_pending_key():
    setup = _build_system()
    waiting = setup.orchestrator.run(
        setup.request
    )
    assert waiting.waiting_for_permission
    foreign = _subject(
        setup,
        request_id=uuid4(),
    )
    pending = _attach_target(
        setup,
        foreign,
    )

    with pytest.raises(
        ValueError,
        match="pending approval subject request_id",
    ):
        _resume(setup, foreign)

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is pending
    )
    assert setup.calls == []


def test_old_subject_cannot_consume_replacement_target():
    setup = _build_system()
    old_subject, _ = _run_identity_bound(setup)
    replacement_subject = _subject(
        setup,
        url="https://example.com/replacement",
    )
    replacement = _attach_target(
        setup,
        replacement_subject,
    )

    with pytest.raises(
        ValueError,
        match="does not match",
    ):
        _resume(setup, old_subject)

    assert (
        setup.orchestrator._pending[
            setup.request.request_id
        ]
        is replacement
    )
    assert setup.calls == []

    assert _resume(
        setup,
        replacement_subject,
    ).completed


def test_two_matching_direct_resumes_claim_and_execute_once():
    entered = Event()
    release = Event()
    calls = []

    def handler(arguments):
        calls.append(dict(arguments))
        entered.set()
        assert release.wait(LIMIT)
        return {"opened_url": arguments["url"]}

    setup = _build_system(handler=handler)
    subject, _ = _run_identity_bound(setup)

    with ThreadPoolExecutor(
        max_workers=2
    ) as pool:
        first = pool.submit(
            _resume,
            setup,
            subject,
        )
        try:
            assert entered.wait(LIMIT)
            second = pool.submit(
                _resume,
                setup,
                subject,
            )
            assert (
                second.result(timeout=LIMIT).status
                is ActionStatus.FAILED
            )
        finally:
            release.set()

        assert first.result(
            timeout=LIMIT
        ).completed

    assert len(calls) == 1


def test_matching_identity_does_not_bypass_forbidden_policy(
    monkeypatch,
):
    setup = _build_system()
    subject, _ = _run_identity_bound(setup)
    original_evaluate = (
        setup.permissions.evaluate
    )

    def forbid(plan):
        report = original_evaluate(plan)
        return replace(
            report,
            decisions=tuple(
                replace(
                    decision,
                    effective_permission=(
                        PermissionMode.FORBIDDEN
                    ),
                )
                for decision in report.decisions
            ),
        )

    monkeypatch.setattr(
        setup.permissions,
        "evaluate",
        forbid,
    )

    result = _resume(setup, subject)

    assert result.status is ActionStatus.BLOCKED
    assert setup.calls == []


def test_plan_validator_reruns_after_matching_identity(
    monkeypatch,
):
    setup = _build_system()
    subject, _ = _run_identity_bound(setup)
    calls = []
    original_validate = setup.validator.validate

    def record(plan):
        calls.append(plan)
        return original_validate(plan)

    monkeypatch.setattr(
        setup.validator,
        "validate",
        record,
    )

    assert _resume(setup, subject).completed
    assert calls == [setup.plan]


def test_legacy_pending_accepts_no_subject_and_rejects_subject():
    legacy = _build_system()
    waiting = legacy.orchestrator.run(
        legacy.request
    )
    assert waiting.waiting_for_permission
    assert legacy.orchestrator.resume(
        legacy.request.request_id,
        confirmed_steps=frozenset({1}),
    ).completed

    rejected = _build_system()
    waiting = rejected.orchestrator.run(
        rejected.request
    )
    assert waiting.waiting_for_permission
    pending = rejected.orchestrator._pending[
        rejected.request.request_id
    ]

    with pytest.raises(
        ValueError,
        match="legacy",
    ):
        rejected.orchestrator.resume(
            rejected.request.request_id,
            confirmed_steps=frozenset({1}),
            expected_subject=_subject(
                rejected
            ),
        )

    assert (
        rejected.orchestrator._pending[
            rejected.request.request_id
        ]
        is pending
    )
    assert rejected.calls == []


class _CapturingEngine:
    def __init__(self):
        self.validation_subjects = []
        self.resume_subjects = []

    def run(self, request):
        return OrchestrationResult(
            request_id=request.request_id,
            status=(
                ActionStatus
                .WAITING_FOR_PERMISSION
            ),
            pending_confirmation_steps=(1,),
        )

    def validate_confirmation(
        self,
        request_id,
        *,
        confirmed_steps,
        expected_subject=None,
    ):
        self.validation_subjects.append(
            expected_subject
        )

    def resume(
        self,
        request_id,
        *,
        confirmed_steps,
        expected_subject=None,
    ):
        self.resume_subjects.append(
            expected_subject
        )
        return OrchestrationResult(
            request_id=request_id,
            status=ActionStatus.COMPLETED,
        )


def test_worker_operation_forwards_same_subject_object():
    request_id = uuid4()
    subject = ApprovalSubject(
        request_id=request_id,
        step_number=1,
        capability="browser",
        risk=RiskLevel.LOW,
        arguments={
            "url":
                "https://docs.python.org/3/"
        },
    )
    engine = _CapturingEngine()
    worker = ActionWorker(
        engine.run,
        capacity=2,
    )
    worker.start()

    try:
        accepted = worker.submit(
            {
                "goal": "Open documentation",
                "raw_input":
                    "Open documentation",
                "request_id":
                    str(request_id),
            }
        )
        waiting = worker.result(
            accepted,
            timeout=LIMIT,
        )
        assert (
            waiting["status"]
            == "waiting_for_permission"
        )

        assert worker.confirm(
            accepted,
            confirmed_steps=frozenset({1}),
            expected_subject=subject,
        ) == accepted

        completed = worker.result(
            accepted,
            timeout=LIMIT,
        )
        assert completed["status"] == "completed"
        assert (
            engine.validation_subjects
            == [subject]
        )
        assert (
            engine.resume_subjects
            == [subject]
        )
        assert (
            engine.validation_subjects[0]
            is subject
        )
        assert (
            engine.resume_subjects[0]
            is subject
        )

    finally:
        worker.shutdown(
            wait=True,
            timeout=LIMIT,
        )


def test_coordinator_voice_and_session_forward_same_subject():
    request_id = uuid4()
    subject = ApprovalSubject(
        request_id=request_id,
        step_number=1,
        capability="browser",
        risk=RiskLevel.LOW,
        arguments={
            "url":
                "https://docs.python.org/3/"
        },
    )
    coordinator_calls = []

    class Worker:
        def confirm(
            self,
            bound_request_id,
            **kwargs,
        ):
            coordinator_calls.append(
                (
                    bound_request_id,
                    kwargs,
                )
            )
            return bound_request_id

    binding = SimpleNamespace(
        request_id=str(request_id)
    )
    event_adapter = SimpleNamespace(
        binding_for_call=(
            lambda call_id: binding
        )
    )
    coordinator = LiveActionCoordinator(
        worker=Worker(),
        event_adapter=event_adapter,
        output_writer=object(),
    )

    assert coordinator.confirm(
        "call-1",
        confirmed_steps=frozenset({1}),
        expected_subject=subject,
    ) == str(request_id)
    assert (
        coordinator_calls[0][1][
            "expected_subject"
        ]
        is subject
    )

    bridge_calls = []

    class Coordinator:
        def confirm(self, call_id, **kwargs):
            bridge_calls.append(
                (call_id, kwargs)
            )
            return str(request_id)

    bridge = object.__new__(
        VoiceActionBridge
    )
    bridge._lock = RLock()
    bridge._state = SimpleNamespace(
        call_id="call-1",
        pending_permission=object(),
    )
    bridge._coordinator = Coordinator()

    assert bridge.confirm_pending(
        confirmed_steps=frozenset({1}),
        expected_subject=subject,
    ) == str(request_id)
    assert (
        bridge_calls[0][1][
            "expected_subject"
        ]
        is subject
    )

    session_calls = []

    class Bridge:
        def confirm_pending(self, **kwargs):
            session_calls.append(kwargs)
            return str(request_id)

    session = object.__new__(
        RearmingVoiceActionSession
    )
    session._bridge = Bridge()

    assert session.confirm_pending(
        confirmed_steps=frozenset({1}),
        expected_subject=subject,
    ) == str(request_id)
    assert (
        session_calls[0][
            "expected_subject"
        ]
        is subject
    )


def test_late_worker_mismatch_leaves_orchestrator_pending(
    monkeypatch,
):
    setup = _build_system()
    worker = ActionWorker(
        setup.orchestrator.run,
        capacity=2,
    )
    worker.start()
    entered = Event()
    release = Event()

    try:
        request_id = worker.submit(
            {
                "goal": setup.request.goal,
                "raw_input":
                    setup.request.raw_input,
                "request_id":
                    str(
                        setup.request.request_id
                    ),
            }
        )
        waiting = worker.result(
            request_id,
            timeout=LIMIT,
        )
        assert (
            waiting["status"]
            == "waiting_for_permission"
        )

        old_subject = _subject(setup)
        _attach_target(
            setup,
            old_subject,
        )
        resume = setup.orchestrator.resume

        def gated(request_id, **kwargs):
            entered.set()
            assert release.wait(LIMIT)
            return resume(
                request_id,
                **kwargs,
            )

        monkeypatch.setattr(
            worker,
            "_resume",
            gated,
        )

        worker.confirm(
            request_id,
            confirmed_steps=frozenset({1}),
            expected_subject=old_subject,
        )
        assert entered.wait(LIMIT)

        replacement_subject = _subject(
            setup,
            url=(
                "https://example.com/"
                "replacement"
            ),
        )
        replacement = _attach_target(
            setup,
            replacement_subject,
        )
        release.set()

        failed = worker.result(
            request_id,
            timeout=LIMIT,
        )
        assert failed["status"] == "failed"
        assert (
            failed["metadata"][
                "worker_error"
            ]["phase"]
            == "resume"
        )
        assert (
            setup.orchestrator._pending[
                setup.request.request_id
            ]
            is replacement
        )
        assert setup.calls == []

    finally:
        release.set()
        worker.shutdown(
            wait=True,
            timeout=LIMIT,
        )
