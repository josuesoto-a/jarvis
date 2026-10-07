"""D1G-D1b0/D1b1a: inert evidence, ownership and concurrency; no backend."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import gc
from pathlib import Path
from threading import Barrier, Event
import weakref

import pytest

import capabilities.terminal_runtime as lifecycle
from capabilities.terminal_runtime import (
    CancellationReason, CleanupIssue, CleanupReason, CreationClassification, InvocationPhase,
    LaunchAdmissionDomain, LaunchAdmissionError, LaunchAdmissionState,
    OwnershipState, QuarantineReason, RuntimeDisposition, TerminalLifecycleError,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Token:
    """Only a Python identity; never acquires or releases anything external."""


@pytest.fixture
def setup():
    clock = Clock()
    domain = LaunchAdmissionDomain(clock=clock)
    return clock, domain, domain.admit()


def own(invocation, name, token=None):
    resource = invocation.resources.reserve(name)
    invocation.resources.adopt(resource, Token() if token is None else token)
    return resource


def release(invocation, resource):
    owner = (invocation._domain.quarantine_owner
             if invocation.snapshot().phase is InvocationPhase.QUARANTINED else invocation.resources)
    owner.begin_release(resource)
    owner.confirm_released(resource)


def release_all(invocation):
    for item in invocation.snapshot().resources:
        if item.state in {OwnershipState.OWNED, OwnershipState.QUARANTINED}:
            release(invocation, invocation.resources.resource(item.name))


def created(invocation, *, adopt=True):
    assert invocation.commit_creation()
    assert invocation.creation.begin_call()
    tokens = Token(), Token()
    invocation.creation.record_success(tokens)
    if adopt:
        assert invocation.adopt_created()
    return tokens


def running(invocation):
    created(invocation)
    assert invocation.commit_resume()
    invocation.record_resumed()


def finish_preflight(invocation):
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    release_all(invocation)
    invocation.finish_cleanup()


def test_shared_domain_rejects_second_owner_then_reopens_after_confirmed_cleanup(setup):
    _, domain, invocation = setup
    assert domain.snapshot().state is LaunchAdmissionState.RUNNING
    with pytest.raises(LaunchAdmissionError, match="running"):
        domain.admit()
    finish_preflight(invocation)
    assert domain.snapshot().state is LaunchAdmissionState.OPEN
    assert domain.admit() is not invocation


def test_isolated_domains_do_not_share_test_state():
    poisoned, isolated = LaunchAdmissionDomain(), LaunchAdmissionDomain()
    invocation = poisoned.admit()
    own(invocation, "unresolved")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    assert poisoned.snapshot().state is LaunchAdmissionState.POISONED
    assert isolated.admit().snapshot().phase is InvocationPhase.PREFLIGHT


def test_concurrent_admission_has_exactly_one_winner():
    domain, barrier = LaunchAdmissionDomain(), Barrier(3)

    def attempt():
        barrier.wait(timeout=5)
        try:
            return domain.admit()
        except LaunchAdmissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(attempt) for _ in range(2)]
        barrier.wait(timeout=5)
        results = [item.result(timeout=5) for item in attempts]
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert winners[0].creation.snapshot().call_count == 0
    finish_preflight(winners[0])


@pytest.mark.parametrize("bad", [0, -1, True, "30", None, float("nan"), float("inf"), 10**1000])
@pytest.mark.parametrize("field", ["execution_seconds", "cleanup_seconds"])
def test_invalid_budgets_do_not_consume_admission(bad, field):
    domain = LaunchAdmissionDomain()
    with pytest.raises(ValueError):
        domain.admit(**{field: bad})
    assert domain.snapshot().state is LaunchAdmissionState.OPEN


@pytest.mark.parametrize("bad", [True, "100", None, float("nan"), float("inf"), 10**1000])
def test_invalid_clock_does_not_consume_admission(bad):
    domain = LaunchAdmissionDomain(clock=lambda: bad)
    with pytest.raises(TerminalLifecycleError):
        domain.admit()
    assert domain.snapshot().state is LaunchAdmissionState.OPEN


def test_unrepresentable_deadline_fails_before_admission():
    domain = LaunchAdmissionDomain(clock=lambda: 1e308)
    with pytest.raises(TerminalLifecycleError):
        domain.admit()
    assert domain.snapshot().state is LaunchAdmissionState.OPEN


def test_clock_must_not_go_backward(setup):
    clock, _, invocation = setup
    clock.now -= 1
    with pytest.raises(TerminalLifecycleError, match="backward"):
        invocation.commit_creation()
    assert invocation.snapshot().phase is InvocationPhase.PREFLIGHT


def test_preflight_is_included_in_execution_budget(setup):
    clock, _, invocation = setup
    assert invocation.snapshot().execution_deadline == 130
    clock.now = 130
    assert not invocation.commit_creation()
    snap = invocation.snapshot()
    assert snap.creation.classification is CreationClassification.PRE_CREATE
    assert snap.creation.call_count == 0
    assert snap.cleanup_reason is CleanupReason.EXECUTION_TIMEOUT
    assert snap.cleanup_deadline == 135


def test_commit_is_not_a_creation_attempt_and_final_gate_checks_deadline(setup):
    clock, _, invocation = setup
    assert invocation.commit_creation()
    assert invocation.creation.snapshot().call_count == 0
    clock.now = 130
    assert not invocation.creation.begin_call()
    assert invocation.creation.snapshot().classification is CreationClassification.PRE_CREATE


def test_one_cleanup_deadline_and_first_trigger_are_never_reset(setup):
    clock, _, invocation = setup
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    first = invocation.snapshot()
    clock.now = 132
    invocation.begin_cleanup(CleanupReason.EXECUTION_TIMEOUT)
    assert invocation.snapshot().cleanup_reason is first.cleanup_reason
    assert invocation.snapshot().cleanup_deadline == first.cleanup_deadline == 105


def test_failed_call_is_c1_and_cannot_retry_or_later_report_success(setup):
    _, _, invocation = setup
    assert invocation.commit_creation()
    assert invocation.creation.begin_call()
    invocation.creation.record_failed()
    snap = invocation.creation.snapshot()
    assert snap.call_count == 1 and not snap.process_created and not snap.result_pending
    assert snap.classification is CreationClassification.CREATE_CALL_FAILED
    for action in (invocation.creation.begin_call, invocation.creation.record_failed,
                   lambda: invocation.creation.record_success((Token(), Token()))):
        with pytest.raises(TerminalLifecycleError):
            action()
    invocation.begin_cleanup(CleanupReason.CREATE_CALL_FAILED)
    invocation.finish_cleanup()


def test_pending_call_is_not_misrepresented_as_p0_or_c1(setup):
    _, _, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    snap = invocation.creation.snapshot()
    assert snap.call_count == 1 and snap.result_pending
    assert snap.classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert not snap.process_created
    with pytest.raises(TerminalLifecycleError):
        invocation.begin_cleanup(CleanupReason.CREATE_CALL_FAILED)
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()


def test_success_receipt_owns_both_tokens_before_phase_adoption_and_survives_exception(setup):
    _, domain, invocation = setup
    tokens = created(invocation, adopt=False)
    try:
        raise RuntimeError("synthetic exception between receipt and adoption")
    except RuntimeError:
        invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    snap = invocation.snapshot()
    assert snap.phase is InvocationPhase.CLEANING
    assert snap.creation.classification is CreationClassification.PROCESS_CREATED
    assert snap.creation.process_created and snap.creation.call_count == 1
    for name, token in zip(("root-process", "primary-thread"), tokens):
        owner = invocation.resources.resource(name)
        assert owner.snapshot().state is OwnershipState.OWNED
        with invocation.resources.borrow(owner) as borrowed:
            assert borrowed.payload is token
    assert not invocation.adopt_created()
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    release_all(invocation)
    invocation.finish_cleanup()
    assert domain.snapshot().state is LaunchAdmissionState.OPEN
    assert invocation.creation.snapshot().process_created


@pytest.mark.parametrize("fault", [KeyboardInterrupt, SystemExit, MemoryError])
def test_receipt_retains_success_across_fatal_caller_exceptions(setup, fault):
    _, _, invocation = setup
    created(invocation, adopt=False)
    with pytest.raises(fault):
        try:
            raise fault()
        finally:
            invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
            invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    assert invocation.snapshot().creation.process_created
    assert invocation.snapshot().phase is InvocationPhase.QUARANTINED


@pytest.mark.parametrize("invalid", [[], (Token(),), (None, Token()), (1, Token())])
def test_invalid_creation_payloads_cannot_erase_native_success(setup, invalid):
    _, _, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    with pytest.raises((TypeError, TerminalLifecycleError)):
        invocation.creation.record_success(invalid)
    assert invocation.creation.snapshot().process_created
    assert not invocation.creation.snapshot().adoption_complete


def test_duplicate_creation_payload_or_existing_owner_is_rejected(setup):
    _, _, invocation = setup
    token = Token()
    own(invocation, "existing", token)
    assert invocation.commit_creation() and invocation.creation.begin_call()
    for pair in ((token, token), (token, Token())):
        with pytest.raises(TerminalLifecycleError):
            invocation.creation.record_success(pair)
    assert invocation.creation.snapshot().process_created
    assert not invocation.creation.snapshot().adoption_complete


def test_success_can_never_be_overwritten_or_marked_failed(setup):
    _, _, invocation = setup
    created(invocation)
    for action in (invocation.creation.record_failed,
                   lambda: invocation.creation.record_success((Token(), Token()))):
        with pytest.raises(TerminalLifecycleError):
            action()
    assert invocation.creation.snapshot().classification is CreationClassification.PROCESS_CREATED


@pytest.mark.parametrize("count", range(5))
def test_partial_construction_accounts_for_every_acquired_token(setup, count):
    _, domain, invocation = setup
    resources = [own(invocation, f"resource-{index}") for index in range(count)]
    invocation.resources.reserve("next-acquisition-failed")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    if resources:
        with pytest.raises(TerminalLifecycleError):
            invocation.finish_cleanup()
    for resource in reversed(resources):
        release(invocation, resource)
    invocation.finish_cleanup()
    assert invocation.creation.snapshot().call_count == 0
    assert domain.snapshot().state is LaunchAdmissionState.OPEN
    assert all(item.state in {OwnershipState.RESERVED, OwnershipState.RELEASED}
               for item in invocation.snapshot().resources)


def test_foreign_resources_and_names_cannot_replace_owners(setup):
    _, _, invocation = setup
    owner = own(invocation, "stdio")
    other = LaunchAdmissionDomain().admit()
    for action in (lambda: other.resources.borrow(owner),
                   lambda: other.resources.adopt(owner, Token()),
                   lambda: other.resources.begin_release(owner),
                   lambda: other.resources.confirm_released(owner)):
        with pytest.raises(TerminalLifecycleError):
            action()
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.reserve("stdio")
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.adopt(owner, Token())


@pytest.mark.parametrize("bad", [None, "", "  ", "x\x00y", "x" * 129])
def test_invalid_resource_names_fail_before_ownership(setup, bad):
    _, _, invocation = setup
    with pytest.raises(ValueError):
        invocation.resources.reserve(bad)


def test_one_payload_has_one_owner_even_after_confirmed_release(setup):
    _, _, invocation = setup
    token = Token()
    owner = own(invocation, "first", token)
    another = invocation.resources.reserve("second")
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.adopt(another, token)
    release(invocation, owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.adopt(another, token)
    invocation.resources.adopt(another, Token())


def test_borrow_blocks_release_and_ended_borrow_cannot_access_payload(setup):
    _, _, invocation = setup
    owner = own(invocation, "stdio")
    borrowed = invocation.resources.borrow(owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_release(owner)
    assert owner.snapshot().borrowers == 1
    borrowed.close()
    borrowed.close()
    assert owner.snapshot().borrowers == 0
    with pytest.raises(TerminalLifecycleError):
        _ = borrowed.payload
    release(invocation, owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.borrow(owner)


@pytest.mark.parametrize("payload", [True, 1, 1.0, "handle", b"memory", bytearray(1), {}, set(), (1,)])
def test_scalar_and_container_payloads_are_not_opaque_ownership_tokens(setup, payload):
    _, _, invocation = setup
    owner = invocation.resources.reserve("opaque")
    with pytest.raises(TypeError):
        invocation.resources.adopt(owner, payload)
    assert owner.snapshot().state is OwnershipState.RESERVED


def test_scalar_subclass_is_also_rejected_as_an_ownership_token(setup):
    class RawValue(int):
        pass

    _, _, invocation = setup
    owner = invocation.resources.reserve("opaque")
    with pytest.raises(TypeError):
        invocation.resources.adopt(owner, RawValue(1))


def test_release_marker_returns_payload_and_prohibits_other_users(setup):
    _, _, invocation = setup
    token = Token()
    owner = own(invocation, "external-release", token)
    assert invocation.resources.begin_release(owner) is token
    assert owner.snapshot().state is OwnershipState.RELEASING
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.borrow(owner)
    invocation.resources.confirm_released(owner)


def test_context_borrow_ends_on_exception(setup):
    _, _, invocation = setup
    owner = own(invocation, "memory")
    with pytest.raises(RuntimeError):
        with invocation.resources.borrow(owner):
            raise RuntimeError("synthetic")
    assert owner.snapshot().borrowers == 0
    release(invocation, owner)


def test_pending_operation_retains_all_dependencies_until_confirmed_completion(setup):
    _, _, invocation = setup
    dependencies = [own(invocation, name) for name in ("reader", "event", "buffer")]
    operation = invocation.resources.begin_operation(*dependencies)
    operation.request_cancel()
    operation.request_cancel()
    assert operation.cancellation_requested
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    for resource in dependencies:
        assert resource.snapshot().pending_operations == 1
        with pytest.raises(TerminalLifecycleError):
            invocation.resources.begin_release(resource)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    operation.confirm_completed()
    with pytest.raises(TerminalLifecycleError):
        operation.confirm_completed()
    for resource in dependencies:
        release(invocation, resource)
    invocation.finish_cleanup()


def test_operation_construction_validation_is_atomic(setup):
    _, _, invocation = setup
    owner = own(invocation, "reader")
    other = LaunchAdmissionDomain().admit()
    foreign = own(other, "foreign")
    for resources in ((), (owner, owner), (owner, foreign)):
        with pytest.raises(TerminalLifecycleError):
            invocation.resources.begin_operation(*resources)
    assert owner.snapshot().pending_operations == 0
    assert invocation.snapshot().pending_operations == 0
    operation = invocation.resources.begin_operation(owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_operation(owner)
    operation.confirm_completed()


def test_release_requires_one_attempt_and_confirmed_evidence(setup):
    _, _, invocation = setup
    owner = own(invocation, "stdio")
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.confirm_released(owner)
    invocation.resources.begin_release(owner)
    assert owner.snapshot().state is OwnershipState.RELEASING
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.borrow(owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_release(owner)
    invocation.resources.confirm_released(owner)
    for action in (lambda: invocation.resources.begin_release(owner),
                   lambda: invocation.resources.confirm_released(owner),
                   lambda: invocation.resources.mark_release_uncertain(owner)):
        with pytest.raises(TerminalLifecycleError):
            action()


def test_uncertain_release_cannot_be_blindly_retried(setup):
    _, domain, invocation = setup
    owner = own(invocation, "stdio")
    invocation.resources.begin_release(owner)
    invocation.resources.mark_release_uncertain(owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_release(owner)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.borrow(owner)
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.RELEASE_UNCERTAIN)
    assert owner.snapshot().state is OwnershipState.RELEASE_UNCERTAIN
    domain.quarantine_owner.confirm_released(owner)  # Independent proof, no release call.
    domain.quarantine_owner.confirm_resolved()
    assert domain.snapshot().quarantine_resolved
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_domain_retains_active_and_quarantined_owners_after_caller_gc():
    domain = LaunchAdmissionDomain()
    invocation = domain.admit()
    token = Token()
    owner = own(invocation, "buffer", token)
    invocation_ref, token_ref = weakref.ref(invocation), weakref.ref(token)
    del token, owner, invocation
    gc.collect()
    assert invocation_ref() is not None and token_ref() is not None
    invocation = invocation_ref()
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    del invocation
    gc.collect()
    assert domain.quarantined_invocation is invocation_ref()
    assert token_ref() is not None
    with pytest.raises(LaunchAdmissionError, match="poisoned"):
        domain.admit()
    retained = domain.quarantined_invocation
    release_all(retained)
    domain.quarantine_owner.confirm_resolved()
    assert retained.snapshot().phase is InvocationPhase.QUARANTINED
    assert domain.shutdown().state is LaunchAdmissionState.POISONED
    with pytest.raises(LaunchAdmissionError):
        domain.admit()


def test_pending_operation_can_complete_in_quarantine_without_reopening(setup):
    clock, domain, invocation = setup
    owner = own(invocation, "pending-memory")
    operation = invocation.resources.begin_operation(owner)
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    clock.now = 105
    invocation.quarantine(QuarantineReason.DEADLINE_EXPIRED)
    assert owner.snapshot().state is OwnershipState.QUARANTINED
    operation.request_cancel()
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_release(owner)
    operation.confirm_completed()
    release(invocation, owner)
    domain.quarantine_owner.confirm_resolved()
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    assert not domain.snapshot().closure_completed


@pytest.mark.parametrize("success", [True, False])
def test_late_creation_report_in_quarantine_preserves_phase_and_poison(setup, success):
    _, domain, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    invocation.confirm_job_empty()
    uncertain = invocation.cleanup_evidence()
    assert uncertain.job_empty and not uncertain.process_exited
    assert uncertain.creation.classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    if success:
        invocation.creation.record_success((Token(), Token()))
        assert not invocation.adopt_created()
        assert invocation.creation.snapshot().process_created
        assert all(item.state is OwnershipState.QUARANTINED
                   for item in invocation.snapshot().resources)
        invocation.confirm_process_exited()
        invocation.confirm_containment_empty()
        release_all(invocation)
    else:
        invocation.creation.record_failed()
    domain.quarantine_owner.confirm_resolved()
    assert invocation.snapshot().phase is InvocationPhase.QUARANTINED
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_p1_cleanup_requires_root_exit_and_empty_containment_even_after_release(setup):
    _, _, invocation = setup
    created(invocation)
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    release_all(invocation)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    with pytest.raises(TerminalLifecycleError):
        invocation.confirm_containment_empty()
    invocation.confirm_process_exited()
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    invocation.confirm_containment_empty()
    invocation.finish_cleanup()
    assert invocation.snapshot().creation.process_created


def test_timely_root_exit_is_not_changed_to_timeout_during_cleanup(setup):
    clock, _, invocation = setup
    running(invocation)
    clock.now = 129
    invocation.confirm_process_exited()
    assert invocation.snapshot().cleanup_reason is CleanupReason.ROOT_EXIT
    clock.now = 132
    invocation.begin_cleanup(CleanupReason.EXECUTION_TIMEOUT)
    assert invocation.snapshot().cleanup_reason is CleanupReason.ROOT_EXIT
    assert invocation.snapshot().cleanup_deadline == 134


def test_late_exit_observation_is_conservatively_timeout(setup):
    clock, _, invocation = setup
    running(invocation)
    clock.now = 130
    invocation.confirm_process_exited()
    assert invocation.snapshot().cleanup_reason is CleanupReason.EXECUTION_TIMEOUT
    assert invocation.snapshot().creation.process_created


@pytest.mark.parametrize("point", ["preflight", "commit", "created", "resume"])
def test_cancellation_before_launch_or_resume_preserves_correct_classification(setup, point):
    _, domain, invocation = setup
    if point == "commit":
        assert invocation.commit_creation()
    elif point == "created":
        created(invocation, adopt=False)
    elif point == "resume":
        created(invocation)
    domain.cancel_active()
    if point == "preflight":
        assert not invocation.commit_creation()
    elif point == "commit":
        assert not invocation.creation.begin_call()
    elif point == "created":
        assert not invocation.adopt_created()
    else:
        assert not invocation.commit_resume()
    expected = CreationClassification.PROCESS_CREATED if point in {"created", "resume"} else CreationClassification.PRE_CREATE
    assert invocation.snapshot().creation.classification is expected
    assert invocation.snapshot().cleanup_reason is CleanupReason.CANCELLED


def test_cannot_skip_creation_success_or_resume_gate(setup):
    _, _, invocation = setup
    for action in (invocation.adopt_created, invocation.commit_resume, invocation.record_resumed,
                   invocation.confirm_process_exited, invocation.confirm_containment_empty,
                   invocation.creation.begin_call, invocation.creation.record_failed):
        with pytest.raises(TerminalLifecycleError):
            action()
    created(invocation)
    with pytest.raises(TerminalLifecycleError):
        invocation.record_resumed()
    assert invocation.commit_resume()
    with pytest.raises(TerminalLifecycleError):
        invocation.commit_resume()
    invocation.record_resumed()
    for action in (invocation.commit_creation, invocation.adopt_created,
                   invocation.commit_resume, invocation.record_resumed):
        with pytest.raises(TerminalLifecycleError):
            action()
    assert invocation.snapshot().phase is InvocationPhase.RUNNING


@pytest.mark.parametrize("reason", [CleanupReason.CREATE_CALL_FAILED, CleanupReason.POST_CREATE_FAILURE,
                                   CleanupReason.CREATION_UNCERTAIN, CleanupReason.CANCELLED,
                                   CleanupReason.EXECUTION_TIMEOUT, CleanupReason.ROOT_EXIT])
def test_cleanup_cause_requires_evidence_before_first_transition(setup, reason):
    _, _, invocation = setup
    with pytest.raises(TerminalLifecycleError):
        invocation.begin_cleanup(reason)
    assert invocation.snapshot().phase is InvocationPhase.PREFLIGHT
    assert invocation.snapshot().cleanup_deadline is None


def test_shutdown_during_inflight_creation_keeps_success_without_resume(setup):
    _, domain, invocation = setup
    entered, report = Event(), Event()

    def model_call():
        assert invocation.commit_creation() and invocation.creation.begin_call()
        entered.set()
        assert report.wait(5)
        invocation.creation.record_success((Token(), Token()))
        return invocation.adopt_created()

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(model_call)
        assert entered.wait(5)
        snap = domain.shutdown()
        report.set()
        assert not result.result(timeout=5)
    assert snap.state is LaunchAdmissionState.RUNNING and snap.admission_closed
    assert not snap.closure_completed
    assert invocation.snapshot().creation.process_created
    assert not invocation.snapshot().process_resumed
    assert invocation.snapshot().cleanup_reason is CleanupReason.CANCELLED
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    release_all(invocation)
    invocation.finish_cleanup()
    assert domain.snapshot().state is LaunchAdmissionState.CLOSED


def test_shutdown_during_resume_keeps_late_resume_evidence(setup):
    _, domain, invocation = setup
    created(invocation)
    assert invocation.commit_resume()
    domain.shutdown()
    invocation.begin_cleanup(CleanupReason.CANCELLED)
    invocation.record_resumed()
    assert invocation.snapshot().process_resumed
    assert invocation.snapshot().phase is InvocationPhase.CLEANING
    assert invocation.snapshot().creation.process_created


def test_cancel_does_not_close_admission_but_shutdown_does(setup):
    _, domain, invocation = setup
    assert domain.cancel_active() and domain.cancel_active()
    assert not domain.snapshot().admission_closed
    assert not invocation.commit_creation()
    invocation.finish_cleanup()
    assert not domain.cancel_active()
    replacement = domain.admit()
    assert domain.shutdown().admission_closed
    assert domain.shutdown().state is LaunchAdmissionState.RUNNING
    assert replacement.snapshot().cancellation_reason is CancellationReason.SHUTDOWN
    finish_preflight(replacement)
    assert domain.shutdown().state is LaunchAdmissionState.CLOSED
    assert domain.shutdown().closure_completed
    assert not domain.cancel_active()
    with pytest.raises(LaunchAdmissionError, match="closed"):
        domain.admit()


def test_idle_shutdown_is_immediate_permanent_and_idempotent():
    domain = LaunchAdmissionDomain()
    first = domain.shutdown()
    assert first.state is LaunchAdmissionState.CLOSED and first.closure_completed
    assert domain.shutdown() == first
    with pytest.raises(LaunchAdmissionError):
        domain.admit()


def test_finished_invocation_cannot_corrupt_a_new_active_owner(setup):
    _, domain, invocation = setup
    finish_preflight(invocation)
    replacement = domain.admit()
    for action in (invocation.commit_creation, invocation.adopt_created,
                   invocation.commit_resume, invocation.record_resumed,
                   invocation.confirm_process_exited, invocation.confirm_containment_empty,
                   invocation.finish_cleanup,
                   lambda: invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE),
                   lambda: invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP),
                   lambda: invocation.resources.reserve("late")):
        with pytest.raises(TerminalLifecycleError):
            action()
    assert invocation.snapshot().phase is InvocationPhase.FINISHED
    assert domain.snapshot().active
    finish_preflight(replacement)


def test_quarantined_invocation_cannot_become_finished_or_acquire_new_resources(setup):
    _, _, invocation = setup
    owner = own(invocation, "remaining")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    for action in (invocation.commit_creation, invocation.finish_cleanup,
                   lambda: invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE),
                   lambda: invocation.resources.reserve("late"),
                   lambda: invocation.resources.begin_operation(owner)):
        with pytest.raises(TerminalLifecycleError):
            action()
    assert invocation.snapshot().phase is InvocationPhase.QUARANTINED


def test_quarantine_reasons_require_matching_uncertainty(setup):
    _, _, invocation = setup
    own(invocation, "remaining")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    for reason in (QuarantineReason.DEADLINE_EXPIRED, QuarantineReason.RELEASE_UNCERTAIN,
                   QuarantineReason.CREATION_UNCERTAIN):
        with pytest.raises(TerminalLifecycleError):
            invocation.quarantine(reason)
    release_all(invocation)
    with pytest.raises(TerminalLifecycleError):
        invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)


def test_receipt_and_ledger_cannot_be_replaced_or_directly_constructed(setup):
    _, domain, invocation = setup
    with pytest.raises(AttributeError):
        invocation.creation = object()
    with pytest.raises(AttributeError):
        invocation.resources = object()
    for constructor, args in ((lifecycle.CreationReceipt, (invocation,)),
                              (lifecycle.ResourceLedger, (invocation,)),
                              (lifecycle.TerminalInvocation, (domain, 100, 30, 5))):
        with pytest.raises(TypeError):
            constructor(*args)


def test_snapshots_are_detached_frozen_and_never_render_payloads(setup):
    _, _, invocation = setup

    class HostileToken:
        def __repr__(self):
            raise AssertionError("opaque payload rendered")

    owner = own(invocation, "opaque", HostileToken())
    before = invocation.snapshot()
    assert "opaque" in repr(before)
    with pytest.raises(FrozenInstanceError):
        before.phase = InvocationPhase.FINISHED
    with pytest.raises(FrozenInstanceError):
        before.resources[-1].state = OwnershipState.RELEASED
    release(invocation, owner)
    assert before.resources[-1].state is OwnershipState.OWNED
    assert invocation.snapshot().resources[-1].state is OwnershipState.RELEASED


def test_new_lifecycle_module_has_no_native_or_execution_dependencies():
    root = Path(__file__).parents[1]
    tree = ast.parse((root / "capabilities/terminal_runtime.py").read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports <= {"__future__", "dataclasses", "enum", "math", "threading", "time", "typing", "capabilities.terminal"}
    # No registration, handler, native binding, or launch primitive is reachable.
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "register" for node in ast.walk(tree))
    assert not (root / "capabilities/terminal_win32.py").exists()
    assert not (root / "capabilities/terminal_handler.py").exists()
    assert "terminal" not in (root / "core/bootstrap.py").read_text(encoding="utf-8-sig")


def test_receipt_is_preallocated_without_creation_or_attached_resources(setup):
    _, _, invocation = setup
    evidence = invocation.creation.snapshot()
    assert evidence.classification is CreationClassification.PRE_CREATE
    assert evidence.call_count == 0 and not evidence.process_created
    assert not evidence.process_attached and not evidence.thread_attached
    assert {item.name for item in invocation.resources.snapshot()} == {"root-process", "primary-thread"}


@pytest.mark.parametrize("interruption_point", ["success", "process", "thread", "adoption"])
def test_interrupted_successful_adoption_always_retains_receipt_and_owner(setup, interruption_point):
    _, domain, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.creation.record_success()
    if interruption_point != "success":
        invocation.creation.attach_process(Token())
    if interruption_point in {"thread", "adoption"}:
        invocation.creation.attach_thread(Token())
    if interruption_point == "adoption":
        assert invocation.adopt_created()
    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("synthetic interruption after native success")
        finally:
            invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
            invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    before = invocation.creation.snapshot()
    assert before.process_created and before.call_count == 1
    assert before.process_attached == (interruption_point != "success")
    assert before.thread_attached == (interruption_point in {"thread", "adoption"})
    assert domain.quarantine_owner.creation is invocation.creation
    assert not invocation.cleanup_evidence().cleanup_completed
    # Late adoption repairs missing ownership facts, never creation history.
    if not before.process_attached:
        domain.quarantine_owner.creation.attach_process(Token())
    if not before.thread_attached:
        domain.quarantine_owner.creation.attach_thread(Token())
    assert domain.quarantine_owner.creation.snapshot().adoption_complete
    assert all(item.state is OwnershipState.QUARANTINED for item in domain.quarantine_owner.snapshot())
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    release_all(invocation)
    domain.quarantine_owner.confirm_resolved()
    assert invocation.creation.snapshot().process_created
    assert not invocation.outcome().cleanup_completed  # Transfer is permanently material.
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_incomplete_success_prevents_phase_adoption_and_cleanup_even_after_exit(setup):
    _, domain, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.creation.record_success()
    invocation.creation.attach_process(Token())
    with pytest.raises(TerminalLifecycleError, match="incomplete"):
        invocation.adopt_created()
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    release_all(invocation)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    assert domain.quarantine_owner.creation.snapshot().process_created
    assert not domain.quarantine_owner.creation.snapshot().adoption_complete


@pytest.mark.parametrize("slot", ["process", "thread"])
def test_creation_slots_attach_once_and_reject_duplicate_ownership(setup, slot):
    _, _, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.creation.record_success()
    attach = getattr(invocation.creation, f"attach_{slot}")
    token = Token()
    attach(token)
    with pytest.raises(TerminalLifecycleError):
        attach(Token())
    other = invocation.creation.attach_thread if slot == "process" else invocation.creation.attach_process
    with pytest.raises(TerminalLifecycleError):
        other(token)
    other(Token())
    assert invocation.creation.snapshot().adoption_complete


@pytest.mark.parametrize("called", [False, True])
def test_attachment_without_native_success_is_rejected(setup, called):
    _, _, invocation = setup
    if called:
        assert invocation.commit_creation() and invocation.creation.begin_call()
        invocation.creation.record_failed()
    for attach in (invocation.creation.attach_process, invocation.creation.attach_thread):
        with pytest.raises(TerminalLifecycleError):
            attach(Token())
    assert not invocation.creation.snapshot().process_created


def test_quarantine_revokes_all_old_ledger_mutation_rights(setup):
    _, domain, invocation = setup
    owner = own(invocation, "owned")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    before = invocation.resources.snapshot()
    for action in (
        lambda: invocation.resources.reserve("new"),
        lambda: invocation.resources.adopt(owner, Token()),
        lambda: invocation.resources.borrow(owner),
        lambda: invocation.resources.begin_operation(owner),
        lambda: invocation.resources.begin_release(owner),
        lambda: invocation.resources.confirm_released(owner),
        lambda: invocation.resources.mark_release_uncertain(owner),
        lambda: invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP),
        invocation.confirm_quarantine_resolved,
    ):
        with pytest.raises(TerminalLifecycleError):
            action()
        assert invocation.resources.snapshot() == before
    assert domain.quarantine_owner.resource("owned") is owner
    domain.quarantine_owner.begin_release(owner)
    domain.quarantine_owner.confirm_released(owner)
    domain.quarantine_owner.confirm_resolved()
    assert owner.snapshot().state is OwnershipState.RELEASED


def test_pretransfer_borrow_obligation_can_end_but_cannot_release_quarantine(setup):
    _, domain, invocation = setup
    owner = own(invocation, "borrowed")
    borrow = invocation.resources.borrow(owner)
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.begin_release(owner)
    borrow.close()  # Explicit completion of a retained obligation, not ownership release.
    domain.quarantine_owner.begin_release(owner)
    domain.quarantine_owner.confirm_released(owner)
    assert not invocation.cleanup_evidence().cleanup_completed


def test_release_in_flight_transfers_with_its_single_attempt(setup):
    _, domain, invocation = setup
    owner = own(invocation, "closing")
    invocation.resources.begin_release(owner)
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.begin_release(owner)
    domain.quarantine_owner.mark_release_uncertain(owner)
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.begin_release(owner)
    assert owner.snapshot().state is OwnershipState.RELEASE_UNCERTAIN
    assert not invocation.outcome().cleanup_completed
    domain.quarantine_owner.confirm_released(owner)  # Independent proof, not a close retry.
    domain.quarantine_owner.confirm_resolved()
    assert not invocation.outcome().cleanup_completed


def test_quarantine_owner_cannot_release_another_domains_resource(setup):
    _, domain, invocation = setup
    owner = own(invocation, "unresolved")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    other = LaunchAdmissionDomain().admit()
    foreign = own(other, "foreign")
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.begin_release(foreign)
    assert owner.snapshot().state is OwnershipState.QUARANTINED
    assert foreign.snapshot().state is OwnershipState.OWNED


def test_generations_ignore_reused_backend_values_and_are_domain_scoped(setup):
    _, domain, invocation = setup

    class ReusedValue:
        value = 7

    first = own(invocation, "old-value", ReusedValue())
    release(invocation, first)
    second = own(invocation, "new-value", ReusedValue())
    first_id, second_id = first.snapshot().identity, second.snapshot().identity
    assert first_id != second_id
    assert first_id.domain is second_id.domain
    assert first_id.generation < second_id.generation
    assert second_id.kind == "new-value"
    finish_preflight(invocation)
    next_invocation = domain.admit()
    later = own(next_invocation, "new-value", ReusedValue())
    assert later.snapshot().identity.generation > second_id.generation
    independent = LaunchAdmissionDomain().admit()
    same_name = own(independent, "new-value", ReusedValue())
    assert same_name.snapshot().identity.domain is not second_id.domain


@pytest.mark.parametrize("classification", list(CreationClassification))
def test_p0_c1_p1_cu_have_distinct_evidence_and_no_retry(setup, classification):
    _, domain, invocation = setup
    preflight = own(invocation, "preflight-resource")
    if classification is not CreationClassification.PRE_CREATE:
        assert invocation.commit_creation() and invocation.creation.begin_call()
        if classification is CreationClassification.CREATE_CALL_FAILED:
            invocation.creation.record_failed()
        elif classification is CreationClassification.PROCESS_CREATED:
            invocation.creation.record_success()
            invocation.creation.attach_process(Token())
            invocation.creation.attach_thread(Token())
    receipt = invocation.creation.snapshot()
    assert receipt.classification is classification
    assert receipt.call_count == (0 if classification is CreationClassification.PRE_CREATE else 1)
    assert receipt.process_created == (classification is CreationClassification.PROCESS_CREATED)
    reason = {
        CreationClassification.PRE_CREATE: CleanupReason.PREFLIGHT_FAILURE,
        CreationClassification.CREATE_CALL_FAILED: CleanupReason.CREATE_CALL_FAILED,
        CreationClassification.PROCESS_CREATED: CleanupReason.POST_CREATE_FAILURE,
        CreationClassification.CREATE_OUTCOME_UNCERTAIN: CleanupReason.CREATION_UNCERTAIN,
    }[classification]
    invocation.begin_cleanup(reason)
    for action in (invocation.commit_creation, invocation.creation.begin_call):
        with pytest.raises(TerminalLifecycleError):
            action()
    if receipt.process_created:
        invocation.confirm_process_exited()
        invocation.confirm_containment_empty()
    release_all(invocation)
    uncertain = classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    if uncertain:
        invocation.confirm_job_empty()
        with pytest.raises(TerminalLifecycleError):
            invocation.finish_cleanup()
        invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    else:
        invocation.finish_cleanup()
    assert preflight.snapshot().state is OwnershipState.RELEASED
    assert invocation.creation.snapshot() == receipt
    assert invocation.outcome().creation_classification is classification
    assert invocation.outcome().cleanup_completed is not uncertain
    assert invocation.outcome().disposition is RuntimeDisposition.FAILED
    assert domain.snapshot().state is (LaunchAdmissionState.POISONED if uncertain
                                       else LaunchAdmissionState.OPEN)


def test_partial_construction_mixed_release_has_exact_accounting(setup):
    _, domain, invocation = setup
    first, second = own(invocation, "A"), own(invocation, "B")
    invocation.resources.reserve("C-never-acquired")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    release(invocation, first)
    invocation.resources.begin_release(second)
    invocation.resources.mark_release_uncertain(second)
    invocation.quarantine(QuarantineReason.RELEASE_UNCERTAIN)
    states = {item.name: item.state for item in domain.quarantine_owner.snapshot()}
    assert states == {"root-process": OwnershipState.RESERVED, "primary-thread": OwnershipState.RESERVED,
                      "A": OwnershipState.RELEASED, "B": OwnershipState.RELEASE_UNCERTAIN,
                      "C-never-acquired": OwnershipState.RESERVED}
    assert not invocation.outcome().cleanup_completed
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_completed_lifecycle_outcome_is_frozen_and_has_no_fabricated_process_output(setup):
    _, domain, invocation = setup
    running(invocation)
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    release_all(invocation)
    assert not invocation.cleanup_evidence().cleanup_completed  # Not finished yet.
    invocation.finish_cleanup()
    outcome = invocation.outcome()
    assert outcome.disposition is RuntimeDisposition.COMPLETED
    assert outcome.process_created and outcome.process_resumed and outcome.process_exited
    assert outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.OPEN
    assert not hasattr(outcome, "stdout") and not hasattr(outcome, "exit_code")
    with pytest.raises(FrozenInstanceError):
        outcome.process_resumed = False


@pytest.mark.parametrize("trigger", [CleanupReason.POST_CREATE_FAILURE, CleanupReason.EXECUTION_TIMEOUT,
                                    CleanupReason.CANCELLED])
def test_failure_timeout_or_cancel_cannot_be_restored_to_success_by_exit(setup, trigger):
    clock, domain, invocation = setup
    running(invocation)
    if trigger is CleanupReason.EXECUTION_TIMEOUT:
        clock.now = 130
    elif trigger is CleanupReason.CANCELLED:
        assert domain.cancel_active()
    invocation.begin_cleanup(trigger)
    invocation.confirm_process_exited()
    invocation.confirm_containment_empty()
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    release_all(invocation)
    invocation.finish_cleanup()
    outcome = invocation.outcome()
    assert outcome.terminal_trigger is trigger
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert outcome.cleanup_completed and outcome.process_exited


def test_root_exit_then_cleanup_failure_preserves_exit_trigger_but_fails_result(setup):
    _, _, invocation = setup
    running(invocation)
    invocation.confirm_process_exited()
    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    invocation.confirm_containment_empty()
    release_all(invocation)
    invocation.finish_cleanup()
    outcome = invocation.outcome()
    assert outcome.terminal_trigger is CleanupReason.ROOT_EXIT
    assert outcome.cleanup_issues == (CleanupIssue.FAILURE,)
    assert outcome.disposition is RuntimeDisposition.FAILED


def test_root_exit_then_quarantine_never_claims_cleanup_complete(setup):
    _, domain, invocation = setup
    running(invocation)
    invocation.confirm_process_exited()
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    initial = invocation.outcome()
    assert initial.terminal_trigger is CleanupReason.ROOT_EXIT
    assert initial.disposition is RuntimeDisposition.FAILED
    invocation.confirm_containment_empty()
    release_all(invocation)
    domain.quarantine_owner.confirm_resolved()
    assert not initial.cleanup_completed and not invocation.outcome().cleanup_completed
    assert domain.shutdown().state is LaunchAdmissionState.POISONED


def test_outcome_rejects_resume_or_exit_without_creation(setup):
    _, _, invocation = setup
    finish_preflight(invocation)
    outcome = invocation.outcome()
    with pytest.raises(ValueError):
        replace(outcome, process_resumed=True)
    with pytest.raises(ValueError):
        replace(outcome.cleanup, process_exited=True)
    with pytest.raises(TypeError):
        replace(outcome, cleanup_issues=[])
    assert outcome.cleanup_completed


def test_shutdown_admission_race_has_one_linearized_outcome():
    domain, barrier = LaunchAdmissionDomain(), Barrier(3)

    def admit():
        barrier.wait(timeout=5)
        try:
            return domain.admit()
        except LaunchAdmissionError:
            return None

    def shutdown():
        barrier.wait(timeout=5)
        return domain.shutdown()

    with ThreadPoolExecutor(max_workers=2) as pool:
        admission, closure = pool.submit(admit), pool.submit(shutdown)
        barrier.wait(timeout=5)
        invocation, closure_snapshot = admission.result(timeout=5), closure.result(timeout=5)
    assert closure_snapshot.admission_closed
    if invocation is None:
        assert domain.snapshot().state is LaunchAdmissionState.CLOSED
    else:
        assert invocation.snapshot().cancellation_reason is CancellationReason.SHUTDOWN
        assert not invocation.commit_creation()
        invocation.finish_cleanup()
        assert domain.snapshot().state is LaunchAdmissionState.CLOSED
    with pytest.raises(LaunchAdmissionError):
        domain.admit()


def test_poison_admission_race_never_admits_over_active_or_quarantined_owner(setup):
    _, domain, invocation = setup
    own(invocation, "unresolved")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    barrier, poisoned = Barrier(3), Event()

    def poison():
        barrier.wait(timeout=5)
        invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
        poisoned.set()

    def contend():
        barrier.wait(timeout=5)
        with pytest.raises(LaunchAdmissionError):
            domain.admit()
        assert poisoned.wait(timeout=5)
        with pytest.raises(LaunchAdmissionError, match="poisoned"):
            domain.admit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = pool.submit(poison), pool.submit(contend)
        barrier.wait(timeout=5)
        for task in tasks:
            task.result(timeout=5)
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_concurrent_cancel_requests_are_idempotent_and_never_release_resources(setup):
    _, domain, invocation = setup
    owner = own(invocation, "owned")
    barrier = Barrier(3)

    def cancel():
        barrier.wait(timeout=5)
        return domain.cancel_active()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(cancel) for _ in range(2)]
        barrier.wait(timeout=5)
        assert all(task.result(timeout=5) for task in tasks)
    assert invocation.snapshot().cancellation_reason is CancellationReason.CANCEL_ACTIVE
    assert owner.snapshot().state is OwnershipState.OWNED
    assert invocation.creation.snapshot().call_count == 0


def test_invalid_creation_snapshot_cannot_claim_impossible_history(setup):
    _, _, invocation = setup
    snapshot = invocation.creation.snapshot()
    for changes in ({"process_created": True}, {"process_attached": True}, {"call_count": 2},
                    {"classification": CreationClassification.PROCESS_CREATED}, {"result_pending": True}):
        with pytest.raises(ValueError):
            replace(snapshot, **changes)


@pytest.mark.parametrize("quarantined", [False, True])
def test_explicit_phase_path_cannot_move_backward_or_resurrect(setup, quarantined):
    _, _, invocation = setup
    phases = [invocation.snapshot().phase]
    assert invocation.commit_creation()
    phases.append(invocation.snapshot().phase)
    assert invocation.creation.begin_call()
    invocation.creation.record_success((Token(), Token()))
    assert invocation.adopt_created()
    phases.append(invocation.snapshot().phase)
    assert invocation.commit_resume()
    invocation.record_resumed()
    phases.append(invocation.snapshot().phase)
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    phases.append(invocation.snapshot().phase)
    if quarantined:
        invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    else:
        invocation.confirm_process_exited()
        invocation.confirm_containment_empty()
        release_all(invocation)
        invocation.finish_cleanup()
    phases.append(invocation.snapshot().phase)
    assert phases == [InvocationPhase.PREFLIGHT, InvocationPhase.CREATE_COMMIT,
                      InvocationPhase.CREATED_SUSPENDED, InvocationPhase.RUNNING,
                      InvocationPhase.CLEANING,
                      InvocationPhase.QUARANTINED if quarantined else InvocationPhase.FINISHED]
    for method in (invocation.commit_creation, invocation.commit_resume, invocation.record_resumed,
                   invocation.finish_cleanup):
        with pytest.raises(TerminalLifecycleError):
            method()
    assert invocation.snapshot().phase is phases[-1]


def test_shutdown_during_p0_uncertain_cleanup_poison_precedes_closed(setup):
    _, domain, invocation = setup
    owner = own(invocation, "preflight-memory")
    assert domain.shutdown().admission_closed
    assert not invocation.commit_creation()
    invocation.resources.begin_release(owner)
    invocation.resources.mark_release_uncertain(owner)
    invocation.quarantine(QuarantineReason.RELEASE_UNCERTAIN)
    for _ in range(2):
        snapshot = domain.shutdown()
        assert snapshot.state is LaunchAdmissionState.POISONED
        assert snapshot.admission_closed and not snapshot.closure_completed
    assert invocation.creation.snapshot().classification is CreationClassification.PRE_CREATE
    assert not invocation.outcome().cleanup_completed


def test_c1_uncertain_cleanup_poison_is_not_limited_to_successful_creation(setup):
    _, domain, invocation = setup
    owner = own(invocation, "setup-resource")
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.creation.record_failed()
    invocation.begin_cleanup(CleanupReason.CREATE_CALL_FAILED)
    invocation.resources.begin_release(owner)
    invocation.resources.mark_release_uncertain(owner)
    invocation.quarantine(QuarantineReason.RELEASE_UNCERTAIN)
    assert invocation.outcome().cleanup.creation.classification is CreationClassification.CREATE_CALL_FAILED
    assert not invocation.outcome().process_created
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_policy_stages_native_evidence_after_pure_foundation_and_keeps_identity():
    root = Path(__file__).parents[1]
    policy = (root / "capabilities/terminal_runtime_policy.md").read_text(encoding="utf-8")
    for requirement in (
        "zero reader threads", "one pending read per stream", "synchronous child writers",
        "PROC_THREAD_ATTRIBUTE_JOB_LIST", "no delayed-assignment fallback", "no breakaway fallback",
        "Only D2 may perform native preflight", "unrelated", "isolated-import interpreter tests",
        "release uncertainty", "QuarantineOwner", "cleanup_completed=False",
    ):
        assert requirement.casefold() in policy.casefold()
    from capabilities.terminal import TERMINAL_TARGET_CONTRACT_VERSION
    assert TERMINAL_TARGET_CONTRACT_VERSION == "terminal-execution-target/v2"


def test_new_terminal_sources_have_no_launch_identifiers():
    root = Path(__file__).parents[1]
    forbidden = {
        "subprocess", "Popen", "system", "popen", "CreateProcess", "CreateProcessW",
        "ShellExecute", "WinExec", "ctypes", "WinDLL", "windll", "pywin32",
        "create_subprocess_exec", "create_subprocess_shell",
    }
    for relative in ("capabilities/terminal_runtime.py", "tests/test_terminal_runtime_lifecycle.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden
            elif isinstance(node, ast.Import):
                assert all(item.name.split(".")[0] not in forbidden for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden
            elif isinstance(node, ast.Call):
                assert not any(argument.arg == "shell" and isinstance(argument.value, ast.Constant)
                               and argument.value.value is True for argument in node.keywords)


def test_interruption_after_quarantine_transfer_cannot_restore_old_authority(setup, monkeypatch):
    _, domain, invocation = setup
    resource = own(invocation, "retained")
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)

    def interrupted_notification():
        raise KeyboardInterrupt("synthetic interruption after logical transfer")

    monkeypatch.setattr(domain._condition, "notify_all", interrupted_notification)
    with pytest.raises(KeyboardInterrupt):
        invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    assert domain.quarantine_owner.resource("retained") is resource
    assert invocation.snapshot().phase is InvocationPhase.QUARANTINED
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    assert not domain.snapshot().active and not invocation.outcome().cleanup_completed
    for action in (lambda: invocation.resources.begin_release(resource), invocation.finish_cleanup,
                   lambda: invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)):
        with pytest.raises(TerminalLifecycleError):
            action()
    domain.quarantine_owner.begin_release(resource)
    domain.quarantine_owner.confirm_released(resource)
    domain.quarantine_owner.confirm_resolved()


def test_definite_failure_to_release_preserves_owned_state_without_blind_retry(setup):
    _, domain, invocation = setup
    resource = own(invocation, "definitely-owned")
    invocation.resources.begin_release(resource)
    invocation.resources.mark_still_owned(resource)
    assert resource.snapshot().state is OwnershipState.OWNED
    assert resource.snapshot().release_attempted
    for action in (lambda: invocation.resources.begin_release(resource),
                   lambda: invocation.resources.confirm_released(resource),
                   lambda: invocation.resources.mark_release_uncertain(resource)):
        with pytest.raises(TerminalLifecycleError):
            action()
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    assert resource.snapshot().state is OwnershipState.QUARANTINED
    assert resource.snapshot().release_attempted
    assert not invocation.outcome().cleanup_completed
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.begin_release(resource)


# D1b1a refines evidence only: Tokens below are Python identities, not handles.

@pytest.mark.parametrize("classification,count,created,pending", [
    (CreationClassification.PRE_CREATE, 0, False, False),
    (CreationClassification.CREATE_CALL_FAILED, 1, False, False),
    (CreationClassification.PROCESS_CREATED, 1, True, False),
    (CreationClassification.CREATE_OUTCOME_UNCERTAIN, 1, False, True),
])
def test_creation_snapshot_classification_matches_exact_evidence(classification, count, created, pending):
    snapshot = lifecycle.CreationSnapshot(count, classification, created, pending, False, False)
    assert snapshot.classification is classification
    assert snapshot.call_count == count
    assert snapshot.process_created is created
    assert snapshot.result_pending is pending
    for other in (*CreationClassification, None, classification.value):
        if other is not classification:
            with pytest.raises(ValueError):
                replace(snapshot, classification=other)


@pytest.mark.parametrize("count,created,pending,process,thread", [
    (-1, False, False, False, False), (2, False, False, False, False),
    (True, False, False, False, False), (0, True, False, False, False),
    (0, False, True, False, False), (1, True, True, False, False),
    (0, False, False, True, False), (0, False, False, False, True),
    (1, False, True, True, False), (1, False, True, False, True),
    (1, False, False, True, False), (1, False, False, False, True),
])
def test_impossible_creation_evidence_cannot_be_given_a_classification(count, created, pending, process, thread):
    for classification in CreationClassification:
        with pytest.raises(ValueError):
            lifecycle.CreationSnapshot(count, classification, created, pending, process, thread)


@pytest.mark.parametrize("result", ["record_success", "record_failed"])
def test_known_result_commit_does_not_depend_on_snapshot_construction(setup, monkeypatch, result):
    _, _, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()

    def unavailable_snapshot(*args, **kwargs):
        raise MemoryError("evidence snapshot allocation unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "CreationSnapshot", unavailable_snapshot)
        getattr(invocation.creation, result)()
    expected = (CreationClassification.PROCESS_CREATED if result == "record_success"
                else CreationClassification.CREATE_CALL_FAILED)
    assert invocation.creation.snapshot().classification is expected


@pytest.mark.parametrize("fault", [MemoryError, KeyboardInterrupt, SystemExit])
def test_known_success_precedes_fallible_payload_adoption(setup, monkeypatch, fault):
    _, _, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()

    def unavailable_adoption(payload):
        raise fault("synthetic failure after success latch")

    monkeypatch.setattr(invocation.resources, "_unique_payload", unavailable_adoption)
    with pytest.raises(fault):
        invocation.creation.record_success((Token(), Token()))
    snapshot = invocation.creation.snapshot()
    assert snapshot.classification is CreationClassification.PROCESS_CREATED
    assert snapshot.process_created and not snapshot.adoption_complete
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    assert invocation.outcome().creation_classification is CreationClassification.PROCESS_CREATED
    assert not invocation.outcome().cleanup_completed


def test_creation_uses_preexisting_process_thread_owners_without_new_identities(setup, monkeypatch):
    _, domain, invocation = setup
    names = ("root-process", "primary-thread")
    owners = tuple(invocation.resources.resource(name) for name in names)
    identities = tuple(owner.snapshot().identity for owner in owners)
    assert all(owner.snapshot().state is OwnershipState.RESERVED for owner in owners)
    assert invocation.creation.snapshot().classification is CreationClassification.PRE_CREATE

    def forbidden_identity(kind):
        raise AssertionError("creation must reuse its preallocated identities")

    monkeypatch.setattr(domain, "_resource_identity_unlocked", forbidden_identity)
    assert invocation.commit_creation() and invocation.creation.begin_call()
    assert invocation.creation.snapshot().classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    invocation.creation.record_success()
    invocation.creation.attach_process(Token())
    invocation.creation.attach_thread(Token())
    assert tuple(invocation.resources.resource(name) for name in names) == owners
    assert tuple(owner.snapshot().identity for owner in owners) == identities
    assert all(owner.snapshot().state is OwnershipState.OWNED for owner in owners)
    assert invocation.creation.snapshot().adoption_complete


@pytest.mark.parametrize("quarantined", [False, True])
def test_cu_job_empty_without_root_exit_never_completes_cleanup(setup, quarantined):
    _, domain, invocation = setup
    own(invocation, "retained-creation-storage")
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    before = invocation.snapshot()
    if quarantined:
        invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    invocation.confirm_job_empty()
    invocation.confirm_job_empty()  # Repeated supplied evidence is idempotent.
    release_all(invocation)
    evidence = invocation.cleanup_evidence()
    assert evidence.job_empty and invocation.snapshot().job_empty
    assert not evidence.process_exited and not evidence.containment_empty
    assert evidence.creation.classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert evidence.creation.call_count == 1 and evidence.creation.result_pending
    assert not evidence.creation.process_created and not evidence.creation.adoption_complete
    assert not evidence.cleanup_completed
    assert invocation.snapshot().cleanup_reason is before.cleanup_reason
    assert invocation.snapshot().cleanup_deadline == before.cleanup_deadline
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    if not quarantined:
        invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    outcome = invocation.outcome()
    assert outcome.creation_classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert not outcome.process_created and not outcome.process_resumed and not outcome.process_exited
    assert not outcome.cleanup_completed
    with pytest.raises(TerminalLifecycleError):
        domain.quarantine_owner.confirm_resolved()
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    with pytest.raises(LaunchAdmissionError):
        domain.admit()


def test_cu_first_trigger_and_no_resume_retry_survive_later_events(setup):
    clock, domain, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    original = invocation.snapshot()
    assert domain.cancel_active()
    clock.now = original.execution_deadline
    invocation.begin_cleanup(CleanupReason.CANCELLED)
    invocation.begin_cleanup(CleanupReason.EXECUTION_TIMEOUT)
    invocation.begin_cleanup(CleanupReason.ROOT_EXIT)
    invocation.confirm_job_empty()
    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
    for action in (invocation.creation.begin_call, invocation.commit_creation,
                   invocation.adopt_created, invocation.commit_resume, invocation.record_resumed,
                   invocation.confirm_process_exited, invocation.confirm_containment_empty,
                   lambda: invocation.creation.attach_process(Token()),
                   lambda: invocation.creation.attach_thread(Token())):
        with pytest.raises(TerminalLifecycleError):
            action()
    snapshot = invocation.snapshot()
    assert snapshot.cleanup_reason is CleanupReason.CREATION_UNCERTAIN
    assert snapshot.cleanup_deadline == original.cleanup_deadline
    assert not snapshot.process_exited and not snapshot.process_resumed
    assert snapshot.creation == original.creation
    invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    assert invocation.outcome().terminal_trigger is CleanupReason.CREATION_UNCERTAIN
    assert invocation.outcome().disposition is RuntimeDisposition.FAILED
    assert not invocation.outcome().cleanup_completed


def test_cu_quarantine_retains_responsibility_and_revokes_old_ledger():
    domain = LaunchAdmissionDomain()
    invocation = domain.admit()
    token = Token()
    resource = own(invocation, "unresolved-storage", token)
    invocation_ref, token_ref = weakref.ref(invocation), weakref.ref(token)
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
    for action in (lambda: invocation.resources.borrow(resource),
                   lambda: invocation.resources.begin_release(resource),
                   lambda: invocation.resources.reserve("new-resource")):
        with pytest.raises(TerminalLifecycleError):
            action()
    del invocation, token, resource
    gc.collect()
    assert domain.quarantined_invocation is invocation_ref()
    assert token_ref() is not None
    assert domain.quarantine_owner.creation.snapshot().classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    domain.quarantined_invocation.confirm_job_empty()
    assert not domain.quarantined_invocation.outcome().cleanup_completed
    assert domain.shutdown().state is LaunchAdmissionState.POISONED
    with pytest.raises(LaunchAdmissionError):
        domain.admit()


def test_cu_quarantine_admission_race_never_reopens_domain(setup):
    _, domain, invocation = setup
    assert invocation.commit_creation() and invocation.creation.begin_call()
    invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
    barrier, transferred = Barrier(3), Event()

    def transfer():
        barrier.wait(timeout=5)
        invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
        transferred.set()

    def contend():
        barrier.wait(timeout=5)
        with pytest.raises(LaunchAdmissionError):
            domain.admit()
        assert transferred.wait(timeout=5)
        with pytest.raises(LaunchAdmissionError, match="poisoned"):
            domain.admit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = pool.submit(transfer), pool.submit(contend)
        barrier.wait(timeout=5)
        for task in tasks:
            task.result(timeout=5)
    invocation.confirm_job_empty()
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    assert invocation.outcome().creation_classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN


@pytest.mark.parametrize("classification", [CreationClassification.PRE_CREATE,
                                             CreationClassification.CREATE_CALL_FAILED,
                                             CreationClassification.CREATE_OUTCOME_UNCERTAIN,
                                             CreationClassification.PROCESS_CREATED])
def test_job_empty_requires_cleanup_and_p1_or_cu_evidence(setup, classification):
    _, _, invocation = setup
    if classification is not CreationClassification.PRE_CREATE:
        assert invocation.commit_creation() and invocation.creation.begin_call()
        if classification is CreationClassification.CREATE_CALL_FAILED:
            invocation.creation.record_failed()
        elif classification is CreationClassification.PROCESS_CREATED:
            invocation.creation.record_success((Token(), Token()))
    with pytest.raises(TerminalLifecycleError):
        invocation.confirm_job_empty()
    reason = {CreationClassification.PRE_CREATE: CleanupReason.PREFLIGHT_FAILURE,
              CreationClassification.CREATE_CALL_FAILED: CleanupReason.CREATE_CALL_FAILED,
              CreationClassification.CREATE_OUTCOME_UNCERTAIN: CleanupReason.CREATION_UNCERTAIN,
              CreationClassification.PROCESS_CREATED: CleanupReason.POST_CREATE_FAILURE}[classification]
    invocation.begin_cleanup(reason)
    if classification in {CreationClassification.PRE_CREATE, CreationClassification.CREATE_CALL_FAILED}:
        with pytest.raises(TerminalLifecycleError):
            invocation.confirm_job_empty()
    else:
        invocation.confirm_job_empty()
        assert invocation.cleanup_evidence().job_empty
        assert not invocation.cleanup_evidence().process_exited
        with pytest.raises(TerminalLifecycleError):
            invocation.finish_cleanup()


def test_p1_job_empty_does_not_replace_root_and_containment_confirmation(setup):
    _, _, invocation = setup
    running(invocation)
    invocation.begin_cleanup(CleanupReason.POST_CREATE_FAILURE)
    invocation.confirm_job_empty()
    release_all(invocation)
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    invocation.confirm_process_exited()
    with pytest.raises(TerminalLifecycleError):
        invocation.finish_cleanup()
    invocation.confirm_containment_empty()
    invocation.finish_cleanup()
    assert invocation.cleanup_evidence().job_empty
    assert invocation.cleanup_evidence().containment_empty


def test_cleanup_evidence_rejects_fabricated_job_empty_before_attempt(setup):
    _, _, invocation = setup
    finish_preflight(invocation)
    evidence = invocation.cleanup_evidence()
    with pytest.raises(ValueError):
        replace(evidence, job_empty=True)
    with pytest.raises(TypeError):
        replace(evidence, job_empty=1)


def test_c1_and_cu_outcomes_expose_distinct_derived_classifications():
    outcomes = []
    for known_failure in (True, False):
        domain = LaunchAdmissionDomain()
        invocation = domain.admit()
        assert invocation.commit_creation() and invocation.creation.begin_call()
        if known_failure:
            invocation.creation.record_failed()
            invocation.begin_cleanup(CleanupReason.CREATE_CALL_FAILED)
            invocation.finish_cleanup()
        else:
            invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
            invocation.confirm_job_empty()
            invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN)
        outcomes.append(invocation.outcome())
    failed, uncertain = outcomes
    assert not failed.process_created and not uncertain.process_created
    assert failed.creation_classification is CreationClassification.CREATE_CALL_FAILED
    assert uncertain.creation_classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert uncertain.cleanup.creation.result_pending and not failed.cleanup.creation.result_pending
    assert failed.cleanup_completed and not uncertain.cleanup_completed
    # The classification is derived, not an independently replaceable field.
    with pytest.raises(TypeError):
        replace(uncertain, creation_classification=CreationClassification.CREATE_CALL_FAILED)
    assert uncertain.creation_classification is CreationClassification.CREATE_OUTCOME_UNCERTAIN
