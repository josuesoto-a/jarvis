"""R2: direct PID evidence, in-memory native marshaling and byte contracts only."""
from collections import deque
import builtins
import ctypes as C
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from capabilities import terminal_runtime as R, terminal_win32 as W
from terminal_runtime_fakes import FakeClock, FakeRuntimeBackend, on_owner, run_fake, forbid_live_loader
from terminal_win32_fakes import FakeBindings, pointer, run_native


def evidence_states(outcome):
    return [o.state for o in outcome.execution.post_root_job_observations]


def fake_rundown(counts, snapshots):
    backend = FakeRuntimeBackend(FakeClock())
    backend.active_counts = deque(counts)
    backend.pid_snapshots = deque(snapshots)
    return backend


def test_root_exit_accounting_zero_needs_no_identity_query_or_termination():
    outcome, backend, domain = run_fake()
    assert outcome.disposition is R.RuntimeDisposition.COMPLETED
    assert outcome.cleanup.job_empty and outcome.execution.stdout.eof and outcome.execution.stderr.eof
    assert R.PostRootJobState.JOB_EMPTY in evidence_states(outcome)
    assert not any(e[0] in {"job_pids", "terminate_job"} for e in backend.events)
    assert domain.snapshot().state is R.LaunchAdmissionState.OPEN


@pytest.mark.parametrize("pids", [(1,), ()])
def test_root_only_or_empty_identity_drains_without_false_membership(pids):
    backend = fake_rundown([1, 0], [R.JobPidSnapshot(pids)])
    outcome, backend, domain = run_fake(backend)
    observations = outcome.execution.post_root_job_observations
    assert observations[0].state is R.PostRootJobState.ROOT_EXITED_JOB_DRAINING
    assert observations[0].snapshot_disagreement is (not pids)
    assert outcome.disposition is R.RuntimeDisposition.COMPLETED
    assert not outcome.execution.non_root_job_member_observed
    assert not outcome.execution.descendants_terminated
    assert not outcome.execution.job_termination_requested
    assert not any(e[0] == "terminate_job" for e in backend.events)
    assert outcome.cleanup_completed and domain.snapshot().state is R.LaunchAdmissionState.OPEN


@pytest.mark.parametrize("pids", [(1, 2), (2,)])
def test_complete_non_root_snapshot_requests_known_job_containment(pids):
    backend = fake_rundown([1, 1, 0], [R.JobPidSnapshot(pids), R.JobPidSnapshot((2,))])
    outcome, backend, domain = run_fake(backend)
    assert outcome.execution.reason_code == "non_root_job_member_after_root_exit"
    assert outcome.execution.non_root_job_member_observed
    assert outcome.execution.job_termination_requested
    assert not outcome.execution.descendants_terminated  # Association is not liveness/ancestry.
    assert [e[0] for e in backend.events].count("terminate_job") == 1
    assert outcome.cleanup.job_empty and outcome.cleanup_completed
    assert domain.snapshot().state is R.LaunchAdmissionState.OPEN


@pytest.mark.parametrize("snapshot", [
    R.JobPidSnapshot(issue=issue) for issue in R.JobPidQueryIssue
] + [RuntimeError("unavailable identity"), object()])
def test_identity_failure_neither_fabricates_non_root_member_nor_empty(snapshot):
    backend = fake_rundown([1, 1, 0], [snapshot])
    outcome, backend, domain = run_fake(backend)
    first = outcome.execution.post_root_job_observations[0]
    assert first.state is R.PostRootJobState.JOB_IDENTITY_UNCERTAIN
    assert first.active_processes == 1
    assert outcome.execution.reason_code == "job_identity_uncertain"
    assert not outcome.execution.non_root_job_member_observed
    assert outcome.creation_classification is R.CreationClassification.PROCESS_CREATED
    assert outcome.cleanup.job_empty and outcome.cleanup_completed
    assert domain.snapshot().state is R.LaunchAdmissionState.OPEN


def test_non_root_identity_cannot_substitute_for_accounting_zero():
    backend = fake_rundown([1, 1], [R.JobPidSnapshot((2,))])
    backend.keep_job = True
    outcome, _, domain = run_fake(backend)
    assert outcome.execution.non_root_job_member_observed
    assert not outcome.cleanup.job_empty and not outcome.cleanup_completed
    assert domain.snapshot().state is R.LaunchAdmissionState.POISONED
    assert domain.quarantine_owner is not None


@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
def test_real_deadline_or_cancellation_preempts_root_only_rundown(interrupt):
    clock = FakeClock()
    domain = R.LaunchAdmissionDomain(clock=clock)
    backend = FakeRuntimeBackend(clock)
    backend.active_counts = deque([1])
    backend.pid_snapshot = R.JobPidSnapshot((backend.root_pid,))
    if interrupt == "timeout":
        backend.hooks["root_exited"] = lambda: clock.advance(28)
        backend.hooks["job_pids"] = lambda: clock.advance(2)
    else:
        backend.hooks["job_pids"] = domain.cancel_active
    outcome, backend, _ = run_fake(backend, domain=domain)
    assert [e[0] for e in backend.events].count("terminate_job") == 1
    assert not outcome.execution.non_root_job_member_observed
    assert outcome.creation_classification is R.CreationClassification.PROCESS_CREATED
    assert outcome.terminal_trigger is R.CleanupReason.ROOT_EXIT  # First trigger remains immutable.
    expected = "execution_timeout_during_job_drain" if interrupt == "timeout" else "cancelled_during_job_drain"
    assert outcome.execution.reason_code == expected


def test_root_only_never_drains_cleanup_failure_does_not_claim_descendant():
    backend = fake_rundown([1], [R.JobPidSnapshot((1,))])
    outcome, backend, domain = run_fake(backend)
    assert not outcome.execution.non_root_job_member_observed
    assert len(outcome.execution.post_root_job_observations) <= R.POST_ROOT_OBSERVATION_LIMIT
    assert [e[0] for e in backend.events].count("terminate_job") == 1
    assert not outcome.cleanup_completed
    assert domain.snapshot().state is R.LaunchAdmissionState.POISONED


@pytest.mark.parametrize("root_pid", [None, 0, -1, True, 2**32, "1"])
def test_missing_or_invalid_root_identity_preserves_p1_and_prevents_resume(root_pid):
    backend = FakeRuntimeBackend(FakeClock())
    backend.root_pid = root_pid
    outcome, backend, _ = run_fake(backend)
    assert outcome.creation_classification is R.CreationClassification.PROCESS_CREATED
    assert outcome.cleanup.creation.root_pid is None
    assert not outcome.process_resumed
    assert not any(e[0] in {"resume", "job_pids"} for e in backend.events)
    assert outcome.disposition is R.RuntimeDisposition.FAILED


def test_receipt_root_identity_is_observational_write_once_after_p1_and_adoption():
    invocation = R.LaunchAdmissionDomain(clock=FakeClock()).admit()
    receipt = invocation.creation
    with pytest.raises(R.TerminalLifecycleError):
        receipt.record_root_pid(1)
    assert invocation.commit_creation() and receipt.begin_call()
    with pytest.raises(R.TerminalLifecycleError):
        receipt.record_root_pid(1)  # CU, even when a caller supplies a plausible number.
    receipt.record_success()
    with pytest.raises(R.TerminalLifecycleError):
        receipt.record_root_pid(1)  # Ownership slots are still unattached.
    receipt.attach_process(object())
    receipt.attach_thread(object())
    before = receipt.snapshot()
    receipt.record_root_pid(123)
    after = receipt.snapshot()
    assert before.root_pid is None and after.root_pid == 123
    assert before.classification is after.classification is R.CreationClassification.PROCESS_CREATED
    with pytest.raises(R.TerminalLifecycleError):
        receipt.record_root_pid(123)
    with pytest.raises(R.TerminalLifecycleError):
        receipt.record_root_pid(456)
    with pytest.raises(FrozenInstanceError):
        after.root_pid = 456


def test_semantic_cu_has_no_root_identity_query_or_handle_access():
    backend = FakeRuntimeBackend(FakeClock(), creation="CU")
    outcome, backend, domain = run_fake(backend)
    assert outcome.cleanup.creation.root_pid is None
    assert not any(e[0] in {"job_pids", "membership", "root_exited", "exit_code", "resume"} for e in backend.events)
    assert outcome.creation_classification is R.CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert domain.quarantine_owner is not None


def test_native_cu_does_not_query_pid_list_or_observe_process_information():
    api = FakeBindings(creation="CU")
    outcome, api, _ = run_native(api)
    assert outcome.cleanup.creation.root_pid is None
    assert not any(name == "QueryInformationJobObject" and args[1] == W.JobObjectBasicProcessIdList
                   for name, args in api.calls)
    assert not any(name in {"ResumeThread", "GetExitCodeProcess", "IsProcessInJob"}
                   for name, args in api.calls)


def test_native_root_pid_recording_follows_p1_and_handle_adoption(monkeypatch):
    api = FakeBindings()
    original = R.CreationReceipt.record_root_pid
    observed = []
    def record(receipt, pid):
        snapshot = receipt.snapshot()
        assert snapshot.process_created and snapshot.adoption_complete
        assert snapshot.root_pid is None
        observed.append(pid)
        original(receipt, pid)
    monkeypatch.setattr(R.CreationReceipt, "record_root_pid", record)
    outcome, api, _ = run_native(api)
    assert observed == [api.root_pid]
    assert outcome.cleanup.creation.root_pid == api.root_pid
    assert outcome.process_resumed
    assert api.counts["GetProcessId"] == 0  # Root identity still comes from post-P1 output.


def test_native_invalid_pid_does_not_erase_creation_success():
    api = FakeBindings()
    api.root_pid = 0
    outcome, api, _ = run_native(api)
    assert outcome.creation_classification is R.CreationClassification.PROCESS_CREATED
    assert not outcome.process_resumed and api.counts["ResumeThread"] == 0


def raw_pid_list(pids, width, capacity=4, assigned=None, count=None):
    assigned = len(pids) if assigned is None else assigned
    count = len(pids) if count is None else count
    raw = bytearray(8 + capacity * width)
    struct.pack_into("<II", raw, 0, assigned, count)
    for index, pid in enumerate(pids[:capacity]):
        struct.pack_into("<" + ("I" if width == 4 else "Q"), raw, 8 + index * width, pid)
    return bytes(raw)


@pytest.mark.parametrize("width", [4, 8])
@pytest.mark.parametrize("pids", [(), (1,), (1, 2, 0xFFFFFFFF)])
def test_pure_decoder_pointer_width_and_zero_one_multiple_entries(width, pids):
    raw = raw_pid_list(pids, width)
    result = W.decode_job_pid_list(raw, 8 + len(pids) * width, pointer_bytes=width, capacity=4)
    assert result.complete and result.pids == pids


@pytest.mark.parametrize("case,issue", [
    ("short_header", R.JobPidQueryIssue.MALFORMED),
    ("over_length", R.JobPidQueryIssue.MALFORMED),
    ("short_entries", R.JobPidQueryIssue.INCOMPLETE),
    ("count_capacity", R.JobPidQueryIssue.MALFORMED),
    ("assigned_capacity", R.JobPidQueryIssue.CAPACITY_EXHAUSTED),
    ("incomplete_counts", R.JobPidQueryIssue.INCOMPLETE),
    ("count_above_assigned", R.JobPidQueryIssue.MALFORMED),
    ("duplicate", R.JobPidQueryIssue.MALFORMED),
    ("zero_pid", R.JobPidQueryIssue.MALFORMED),
    ("above_dword", R.JobPidQueryIssue.MALFORMED),
])
def test_decoder_rejects_malformed_and_incomplete_responses(case, issue):
    pids, assigned, count, length = (1, 2), 2, 2, 24
    if case == "short_header": length = 7
    elif case == "over_length": length = 41
    elif case == "short_entries": length = 16
    elif case == "count_capacity": assigned = count = 5
    elif case == "assigned_capacity": assigned, count = 5, 4
    elif case == "incomplete_counts": assigned, count = 3, 2
    elif case == "count_above_assigned": assigned, count = 1, 2
    elif case == "duplicate": pids = (1, 1)
    elif case == "zero_pid": pids = (1, 0)
    elif case == "above_dword": pids = (1, 2**32)
    raw = raw_pid_list(pids, 8, assigned=assigned, count=count)
    result = W.decode_job_pid_list(raw, length, pointer_bytes=8, capacity=4)
    assert not result.complete and result.issue is issue and result.pids == ()


@pytest.mark.parametrize("pids", [(), (1,), (1, 2)])
def test_backend_pid_query_is_bounded_semantic_and_single_dispatch(pids):
    api = FakeBindings()
    api.pid_snapshot = pids
    def query(owner):
        backend = W.Win32TerminalBackend(owner_thread=owner, bindings=api)
        # Explicitly injected in-memory binding plus an opaque fake-owned Job.
        job = W._Handle(backend)
        job._value = api._new("job")
        job._resolved = job._acquired = True
        result = backend.job_pids(job)
        assert backend.release(job)
        return result
    result = on_owner(query)
    assert result.complete and result.pids == pids
    queries = [(args[1], args[3]) for name, args in api.calls if name == "QueryInformationJobObject"]
    assert queries == [(3, 8 + R.JOB_PID_OBSERVATION_CAPACITY * C.sizeof(W.ULONG_PTR))]
    assert not api.allocations and not api.locals


@pytest.mark.parametrize("failure,issue", [
    (5, R.JobPidQueryIssue.QUERY_FAILED),
    (W.ERROR_MORE_DATA, R.JobPidQueryIssue.CAPACITY_EXHAUSTED),
])
def test_backend_query_failure_has_no_partial_identity_or_retry(failure, issue):
    api = FakeBindings()
    api.pid_query_failure = failure
    def query(owner):
        backend = W.Win32TerminalBackend(owner_thread=owner, bindings=api)
        job = W._Handle(backend)
        job._value = api._new("job")
        job._resolved = job._acquired = True
        result = backend.job_pids(job)
        assert backend.release(job)
        return result
    result = on_owner(query)
    assert result.issue is issue and result.pids == ()
    assert api.counts["QueryInformationJobObject"] == 1


def test_native_fake_count_and_identity_sequences_are_independent():
    api = FakeBindings()
    api.active_counts = deque([1, 1, 0])
    api.pid_snapshots = deque([(1,), ()])
    outcome, api, _ = run_native(api)
    assert outcome.disposition is R.RuntimeDisposition.COMPLETED
    observations = outcome.execution.post_root_job_observations
    assert [o.active_processes for o in observations] == [1, 1, 0]
    assert observations[0].pid_snapshot.pids == (1,)
    assert observations[1].snapshot_disagreement
    assert api.counts["TerminateJobObject"] == 0


def test_termination_result_unavailable_is_not_retried_and_does_not_prove_empty():
    backend = fake_rundown([1], [R.JobPidSnapshot((2,))])
    backend.fail.add("terminate_job")
    outcome, backend, domain = run_fake(backend)
    assert [e[0] for e in backend.events].count("terminate_job") == 1
    assert not outcome.execution.job_termination_requested
    assert not outcome.cleanup.job_empty
    assert domain.quarantine_owner is not None


FUTURE_BINARY_PAYLOAD = (
    'import sys; sys.stdout.buffer.write(b"D2b stdout\\n"); '
    'sys.stderr.buffer.write(b"D2b stderr\\n"); '
    'sys.stdout.buffer.flush(); sys.stderr.buffer.flush()'
)


def assert_future_binary_capture(stdout, stderr):
    assert stdout == b"D2b stdout\n" and len(stdout) == 11
    assert stderr == b"D2b stderr\n" and len(stderr) == 11


def test_future_binary_payload_with_mock_standard_streams_preserves_exact_bytes(monkeypatch):
    stdout, stderr = BytesIO(), BytesIO()
    streams = SimpleNamespace(stdout=SimpleNamespace(buffer=stdout), stderr=SimpleNamespace(buffer=stderr))
    original = builtins.__import__
    def fake_import(name, *args, **kwargs):
        return streams if name == "sys" else original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    exec(compile(FUTURE_BINARY_PAYLOAD, "<future diagnostic bytes; no process>", "exec"), {})
    assert_future_binary_capture(stdout.getvalue(), stderr.getvalue())
    for raw in (stdout.getvalue(), stderr.getvalue()):
        capture = R.CaptureAccumulator()
        capture.accept(R.ReadResult(True, raw))
        capture.accept(R.ReadResult(True, eof=True))
        assert capture.finalize().text.encode("utf-8") == raw


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_future_binary_contract_rejects_crlf(stream):
    stdout, stderr = b"D2b stdout\n", b"D2b stderr\n"
    if stream == "stdout": stdout = b"D2b stdout\r\n"
    else: stderr = b"D2b stderr\r\n"
    with pytest.raises(AssertionError):
        assert_future_binary_capture(stdout, stderr)


def test_native_pid_list_layout_and_storage_bounds():
    assert W.JOBOBJECT_BASIC_PROCESS_ID_LIST.NumberOfAssignedProcesses.offset == 0
    assert W.JOBOBJECT_BASIC_PROCESS_ID_LIST.NumberOfProcessIdsInList.offset == 4
    assert W.JOBOBJECT_BASIC_PROCESS_ID_LIST.ProcessIdList.offset == 8
    assert C.sizeof(W.JOBOBJECT_BASIC_PROCESS_ID_LIST) == 8 + C.sizeof(W.ULONG_PTR)
    assert C.alignment(W.JOBOBJECT_BASIC_PROCESS_ID_LIST) == C.alignment(W.ULONG_PTR)
    assert C.sizeof(W._JobPidListStorage) == 8 + R.JOB_PID_OBSERVATION_CAPACITY * C.sizeof(W.ULONG_PTR)
