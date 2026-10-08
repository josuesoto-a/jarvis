"""D1b owner-thread lifecycle tests with semantic fakes only."""
from collections import deque
from dataclasses import FrozenInstanceError, replace
from threading import Thread, current_thread

import pytest

from capabilities.terminal_runtime import (
    CaptureAccumulator, ReadResult, CreationClassification as CC, CleanupReason,
    LaunchAdmissionDomain, LaunchAdmissionError, LaunchAdmissionState, InvocationPhase,
    OwnershipState, RuntimeDisposition, QuarantineReason, TerminalLifecycleError, WindowsTerminalRuntime,
)
from terminal_runtime_fakes import (
    FakeClock, FakeRuntimeBackend, run_fake, target_for, on_owner, forbid_live_loader,
)


def names(backend):
    return [event[0] for event in backend.events]


def test_normal_capture_exit_and_job_are_independent_and_complete():
    outcome, backend, domain = run_fake()
    assert outcome.disposition is RuntimeDisposition.COMPLETED
    assert outcome.creation_classification is CC.PROCESS_CREATED
    assert outcome.cleanup_completed and outcome.process_exited and outcome.cleanup.job_empty
    assert outcome.execution.stdout.text == "out" and outcome.execution.stderr.text == "err"
    assert outcome.execution.stdout.complete and outcome.execution.stderr.complete
    assert names(backend).count("create_process") == names(backend).count("resume") == 1
    assert backend.events[-1] == ("release", "job")
    assert domain.snapshot().state is LaunchAdmissionState.OPEN


@pytest.mark.parametrize("creation,classification", [("C1", CC.CREATE_CALL_FAILED), ("CU", CC.CREATE_OUTCOME_UNCERTAIN)])
def test_non_success_creation_never_resumes_or_retries(creation, classification):
    backend = FakeRuntimeBackend(FakeClock(), creation=creation)
    outcome, backend, domain = run_fake(backend)
    assert outcome.creation_classification is classification
    assert names(backend).count("create_process") == 1
    assert "resume" not in names(backend) and "membership" not in names(backend)
    assert not outcome.process_created and not outcome.process_exited
    assert outcome.disposition is RuntimeDisposition.FAILED
    if creation == "CU":
        assert not outcome.cleanup_completed and outcome.cleanup.job_empty
        assert backend.events.count(("terminate_job", "job")) == 1
        assert "root_exited" not in names(backend)
        assert not any(e[0] == "release" and e[1] in {"root-process", "primary-thread", "launch", "job"}
                       for e in backend.events)
        assert domain.snapshot().state is LaunchAdmissionState.POISONED
        assert domain.quarantine_owner is not None
        assert domain.quarantine_owner.creation.snapshot().classification is classification
        fresh = WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=Thread())
        with pytest.raises(LaunchAdmissionError, match="poisoned"):
            on_owner(lambda owner: WindowsTerminalRuntime(backend=backend, admission_domain=domain,
                                                        owner_thread=owner).execute(target_for()))
    else:
        assert outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.OPEN
        assert "terminate_job" not in names(backend)


@pytest.mark.parametrize("creation", ["P1", "CU"])
@pytest.mark.parametrize("interruption", ["cancel_active", "shutdown"])
def test_request_during_create_commits_known_p1_before_cancellation(creation, interruption):
    clock = FakeClock()
    domain = LaunchAdmissionDomain(clock=clock)
    backend = FakeRuntimeBackend(clock, creation=creation)
    backend.create_hook = getattr(domain, interruption)
    outcome, _, _ = run_fake(backend, domain=domain)
    assert outcome.creation_classification is (CC.PROCESS_CREATED if creation == "P1" else CC.CREATE_OUTCOME_UNCERTAIN)
    assert not outcome.process_resumed and "resume" not in names(backend)
    assert names(backend).count("create_process") == 1
    assert outcome.terminal_trigger is (CleanupReason.CANCELLED if creation == "P1" else CleanupReason.CREATION_UNCERTAIN)
    assert backend.events.count(("terminate_job", "job")) == 1


@pytest.mark.parametrize("hook", ["open_executable", "read_executable", "open_cwd", "configure_job", "make_stdio", "prepare_launch"])
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_preflight_requests_consume_entry_budget_and_keep_p0(hook, interruption):
    clock = FakeClock()
    domain = LaunchAdmissionDomain(clock=clock)
    backend = FakeRuntimeBackend(clock)
    backend.hooks[hook] = domain.cancel_active if interruption == "cancel" else lambda: clock.advance(31)
    outcome, _, _ = run_fake(backend, domain=domain)
    assert outcome.creation_classification is CC.PRE_CREATE
    assert "create_process" not in names(backend) and "resume" not in names(backend)
    assert outcome.terminal_trigger is (CleanupReason.CANCELLED if interruption == "cancel" else CleanupReason.EXECUTION_TIMEOUT)
    assert outcome.cleanup_completed


@pytest.mark.parametrize("hook", ["open_executable", "read_executable", "file_evidence", "open_cwd",
                                  "create_job", "configure_job", "make_stdio", "connect_pipe", "prepare_launch"])
def test_preflight_failures_never_attempt_creation(hook):
    backend = FakeRuntimeBackend(FakeClock())
    backend.fail.add(hook)
    outcome, _, _ = run_fake(backend)
    assert outcome.creation_classification is CC.PRE_CREATE
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert "create_process" not in names(backend)


@pytest.mark.parametrize("stage", ["after_success", "membership", "resume", "inspect_read", "exit_code"])
def test_post_success_failure_never_erases_p1(stage):
    backend = FakeRuntimeBackend(FakeClock())
    backend.fail.add(stage)
    outcome, _, _ = run_fake(backend)
    assert outcome.creation_classification is CC.PROCESS_CREATED and outcome.process_created
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert names(backend).count("create_process") == 1 and names(backend).count("resume") <= 1


@pytest.mark.parametrize("suspend", [0, 2, 3, 0xFFFFFFFF])
def test_resume_accepts_exactly_one_prior_suspend_and_never_loops(suspend):
    backend = FakeRuntimeBackend(FakeClock())
    backend.resume_count = suspend
    outcome, _, _ = run_fake(backend)
    assert outcome.process_created and not outcome.process_resumed
    assert names(backend).count("resume") == 1
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert "terminate_job" in names(backend)


def test_membership_and_child_only_closure_precede_resume():
    outcome, backend, _ = run_fake()
    resume_index = names(backend).index("resume")
    for child in ("stdin-child", "stdout-child", "stderr-child"):
        assert backend.events.index(("release", child)) < resume_index
    assert names(backend).index("membership") < resume_index
    assert outcome.process_resumed


def test_membership_failure_is_p1_with_no_resume():
    backend = FakeRuntimeBackend(FakeClock())
    backend.member = False
    outcome, _, _ = run_fake(backend)
    assert outcome.process_created and not outcome.process_resumed
    assert "resume" not in names(backend)


@pytest.mark.parametrize("uncertain", [False, True])
def test_child_duplicate_release_failure_blocks_resume_and_poison(uncertain):
    backend = FakeRuntimeBackend(FakeClock())
    (backend.release_uncertain if uncertain else backend.release_fail).add("stdout-child")
    outcome, _, domain = run_fake(backend)
    assert outcome.process_created and not outcome.process_resumed and not outcome.cleanup_completed
    assert backend.events.count(("release", "stdout-child")) == 1
    assert domain.snapshot().state is LaunchAdmissionState.POISONED


def test_wrong_thread_rejected_before_admission_or_acquisition():
    backend = FakeRuntimeBackend(FakeClock())
    domain = LaunchAdmissionDomain(clock=backend.clock)
    runtime = WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=Thread())
    with pytest.raises(TerminalLifecycleError, match="owner thread"):
        runtime.execute(target_for())
    assert backend.events == [] and domain.snapshot().state is LaunchAdmissionState.OPEN
    with pytest.raises(ValueError):
        WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=current_thread())


def test_explicit_backend_and_domain_are_required():
    with pytest.raises(TypeError):
        WindowsTerminalRuntime()
    with pytest.raises(TypeError):
        WindowsTerminalRuntime(backend=None, admission_domain=LaunchAdmissionDomain(), owner_thread=Thread())


def test_other_threads_only_request_cancellation_shutdown_and_wake():
    clock = FakeClock()
    domain = LaunchAdmissionDomain(clock=clock)
    backend = FakeRuntimeBackend(clock)
    runtime_box = []

    def during_create():
        runtime = runtime_box[0]
        events_before = tuple(backend.events)
        controls = Thread(target=lambda: (runtime.cancel_active(), runtime.signal_wake(), runtime.shutdown()))
        controls.start()
        controls.join(5)
        assert not controls.is_alive()
        assert tuple(backend.events) == events_before

    backend.create_hook = during_create

    def run(owner):
        runtime = WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=owner)
        runtime_box.append(runtime)
        return runtime.execute(target_for())

    outcome = on_owner(run)
    assert outcome.process_created and not outcome.process_resumed
    assert outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.CLOSED


def test_executable_mismatch_does_not_replace_target_or_attempt_creation():
    backend = FakeRuntimeBackend(FakeClock())
    target = replace(target_for(), executable_identity="sha256:" + "1" * 64)
    outcome, _, _ = run_fake(backend, target=target)
    assert outcome.creation_classification is CC.PRE_CREATE
    assert outcome.execution.verified_executable_identity is None
    assert "create_process" not in names(backend)


@pytest.mark.parametrize("change", ["executable_directory", "cwd_directory", "executable_path", "cwd_path", "disk"])
def test_native_object_verification_fails_closed(change):
    backend = FakeRuntimeBackend(FakeClock())
    setattr(backend, change, True if change == "executable_directory" else False if change.endswith("directory") or change == "disk" else r"C:\Other")
    outcome, _, _ = run_fake(backend)
    assert outcome.creation_classification is CC.PRE_CREATE


def test_root_exit_with_descendants_terminates_and_fails_even_exit_zero():
    backend = FakeRuntimeBackend(FakeClock())
    backend.descendants = True
    outcome, _, _ = run_fake(backend)
    assert outcome.terminal_trigger is CleanupReason.ROOT_EXIT
    assert outcome.execution.exit_code == 0 and outcome.execution.descendants_terminated
    assert "terminate_job" in names(backend)
    assert outcome.cleanup_completed and outcome.disposition is RuntimeDisposition.FAILED


def test_job_empty_does_not_prove_root_exit():
    backend = FakeRuntimeBackend(FakeClock())
    backend.keep_running = True
    backend.active = 0
    backend.fail.add("root_exited")
    outcome, _, domain = run_fake(backend)
    assert outcome.cleanup.job_empty and not outcome.process_exited
    assert not outcome.cleanup_completed and domain.snapshot().state is LaunchAdmissionState.POISONED


def test_timeout_terminates_and_preserves_first_trigger_after_exit_zero():
    backend = FakeRuntimeBackend(FakeClock())
    backend.keep_running = True
    backend.hooks["wait"] = lambda: backend.clock.advance(31)
    outcome, _, _ = run_fake(backend)
    assert outcome.terminal_trigger is CleanupReason.EXECUTION_TIMEOUT
    assert outcome.disposition is RuntimeDisposition.FAILED
    assert "terminate_job" in names(backend)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_independent_eof_and_capture_incomplete_fail_after_clean_resource_release(stream):
    backend = FakeRuntimeBackend(FakeClock())
    backend.outputs[stream] = deque([b"prefix"])
    outcome, _, _ = run_fake(backend)
    capture = getattr(outcome.execution, stream)
    assert capture.text == "prefix" and not capture.eof and not capture.complete
    assert getattr(outcome.execution, "stderr" if stream == "stdout" else "stdout").complete
    assert outcome.cleanup_completed and outcome.disposition is RuntimeDisposition.FAILED
    assert backend.events.count(("cancel_read", stream)) == 1


def test_pending_io_cannot_be_freed_when_cancellation_never_completes():
    backend = FakeRuntimeBackend(FakeClock())
    backend.outputs = {"stdout": deque(), "stderr": deque()}
    backend.cancel_unresolved = True
    outcome, _, domain = run_fake(backend)
    assert not outcome.cleanup_completed and outcome.cleanup.pending_operations == 2
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    for stream in ("stdout", "stderr"):
        for suffix in ("reader", "event", "memory"):
            assert ("release", stream + "-" + suffix) not in backend.events
            assert domain.quarantine_owner.resource(stream + "-" + suffix).snapshot().pending_operations == 1


@pytest.mark.parametrize("exit_code", [1, 2, 259, 0xFFFFFFFF])
def test_nonzero_exit_returns_failed_with_capture(exit_code):
    backend = FakeRuntimeBackend(FakeClock())
    backend.exit = exit_code
    outcome, _, _ = run_fake(backend)
    assert outcome.execution.exit_code == exit_code
    assert outcome.cleanup_completed and outcome.execution.stdout.text == "out"
    assert outcome.disposition is RuntimeDisposition.FAILED


def test_bounded_capture_continues_draining_and_decodes_once():
    backend = FakeRuntimeBackend(FakeClock(), chunks={
        "stdout": [b"x" * 16384] * 6 + [b"\xff", None],
        "stderr": [b"\xf0\x9f", b"\x8c\x8d", None]})
    outcome, _, _ = run_fake(backend)
    out, err = outcome.execution.stdout, outcome.execution.stderr
    assert out.observed == 98305 and out.retained == 65536 and out.truncated and out.complete
    assert len(out.text) == 65536 and err.text == "🌍" and err.complete
    assert outcome.disposition is RuntimeDisposition.COMPLETED


@pytest.mark.parametrize("data", [b"", b"abc", b"\xff", b"\xe2\x82"])
def test_capture_snapshot_decoding_and_finalization_are_frozen(data):
    capture = CaptureAccumulator()
    capture.accept(ReadResult(True, data))
    capture.accept(ReadResult(True, eof=True))
    final = capture.finalize()
    assert final.text == data.decode("utf-8", errors="replace")
    assert final.retained == final.observed == len(data) and final.complete
    assert capture.finalize() is final
    with pytest.raises(FrozenInstanceError):
        final.text = "changed"
    with pytest.raises(TerminalLifecycleError):
        capture.accept(ReadResult(True, b"extra"))


def test_capture_counter_saturation_is_explicit():
    capture = CaptureAccumulator()
    capture._observed = (1 << 64) - 2
    capture.accept(ReadResult(True, b"abcd"))
    result = capture.finalize()
    assert result.observed == (1 << 64) - 1 and result.saturated and result.truncated


@pytest.mark.parametrize("kwargs", [{"completed": False, "data": b"x"}, {"completed": False, "eof": True},
                                    {"completed": True, "data": b"x" * 16385}, {"completed": 1}])
def test_bad_backend_read_evidence_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ReadResult(**kwargs)



def test_cleanup_wait_failure_still_requests_read_cancel_and_quarantines():
    backend = FakeRuntimeBackend(FakeClock())
    backend.outputs = {"stdout": deque(), "stderr": deque()}
    backend.fail.add("wait")
    outcome, _, domain = run_fake(backend)
    assert outcome.process_created and not outcome.cleanup_completed
    assert domain.snapshot().state is LaunchAdmissionState.POISONED
    assert backend.events.count(("cancel_read", "stdout")) == 1
    assert backend.events.count(("cancel_read", "stderr")) == 1


def test_cleanup_rearm_reuses_dependencies_and_is_revoked_after_quarantine():
    domain = LaunchAdmissionDomain()
    invocation = domain.admit()
    resources = []
    for name in ("reader", "event", "memory"):
        resource = invocation.resources.reserve(name)
        invocation.resources.adopt(resource, object())
        resources.append(resource)
    operation = invocation.resources.begin_operation(*resources)
    operation.confirm_completed()
    invocation.begin_cleanup(CleanupReason.PREFLIGHT_FAILURE)
    operation.rearm_for_drain()
    assert invocation.cleanup_evidence().pending_operations == 1
    assert all(r.snapshot().pending_operations == 1 for r in resources)
    with pytest.raises(TerminalLifecycleError):
        invocation.resources.begin_operation(*resources)
    operation.request_cancel()
    assert not operation.completed
    operation.confirm_completed()
    invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
    with pytest.raises(TerminalLifecycleError):
        operation.rearm_for_drain()
