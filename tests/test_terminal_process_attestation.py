"""Non-root attestation: pure models and ctypes-shaped in-memory APIs only."""
from dataclasses import FrozenInstanceError, replace

import pytest

from capabilities import terminal_runtime as R, terminal_win32 as W
from terminal_runtime_fakes import FakeRuntimeBackend, FakeClock, run_fake, on_owner
from terminal_win32_fakes import FakeBindings


def attest(api=None, pids=(42,), *, previous=None, twice=False, cu=False, after=None):
    api = FakeBindings() if api is None else api
    def execute(owner):
        backend = W.Win32TerminalBackend(owner_thread=owner, bindings=api)
        domain = R.LaunchAdmissionDomain(clock=api.clock)
        invocation = domain.admit()
        scope = R.BackendResourceScope(invocation, checkpoint=lambda: None)
        job = backend.create_job(scope)
        scope.observation_handles = backend.prepare_observation_handles(scope)
        scope.observed_generations.update(previous or {})
        assert all(r.snapshot().state is R.OwnershipState.OWNED
                   for r in scope.observation_handles)
        assert api.counts["OpenProcess"] == 0
        assert invocation.commit_creation()
        assert invocation.creation.begin_call()
        if cu:
            invocation.begin_cleanup(R.CleanupReason.CREATION_UNCERTAIN)
        else:
            # Supplied lifecycle facts, no CreateProcess/Resume native or fake API call.
            invocation.creation.record_success((object(), object()))
            invocation.creation.record_root_pid(1)
            assert invocation.adopt_created()
            assert invocation.commit_resume()
            invocation.record_resumed()
            invocation.confirm_process_exited()
        runtime = R.WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=owner)
        rows = runtime._attest_members(invocation, scope, job, R.JobPidSnapshot(tuple(pids)))
        if twice:
            api.observed_processes[42] = {"creation_time": 0x01D0000000000002}
            rows += runtime._attest_members(invocation, scope, job, R.JobPidSnapshot((42,)))
        if after:
            after(backend, invocation, scope, job, runtime)
        for resource in reversed(scope.ordered):
            if resource is not job:
                runtime._release(invocation, resource)
        if not cu:
            for name in ("primary-thread", "root-process"):
                resource = invocation.resources.resource(name)
                invocation.resources.begin_release(resource)
                invocation.resources.confirm_released(resource)
            invocation.confirm_job_empty()
            invocation.confirm_containment_empty()
        runtime._release(invocation, job)
        try:
            invocation.finish_cleanup()
        except R.TerminalLifecycleError:
            invocation.quarantine(R.QuarantineReason.CREATION_UNCERTAIN if cu
                                  else R.QuarantineReason.INCOMPLETE_CLEANUP)
        return rows, api, invocation, domain
    return on_owner(execute)


def kinds(row):
    return {i.kind for i in row.issues}


def observation_handles(api):
    return {h: data for h, data in api.handles.items() if data["kind"] == "observation"}


def test_success_queries_same_owned_handle_and_releases_it_once():
    rows, api, invocation, domain = attest()
    row, = rows
    handle, = observation_handles(api)
    assert row.requested_pid == row.confirmed_pid == 42
    assert row.image_path == r"C:\Tools\member.exe"
    assert row.creation_time == 0x01D0000000000001 > 2**53
    assert type(row.creation_time) is int
    assert row.job_membership is True and row.liveness_state is R.ProcessLiveness.NOT_SIGNALED
    assert row.release_confirmed and row.handle_acquired
    assert row.classification is R.ProcessIdentityClassification.KNOWN_NON_ROOT_PROCESS
    assert row.snapshot_link == "unproven" and R.ProcessIdentityIssue.SNAPSHOT_LINK_UNPROVEN in kinds(row)
    for name in ("GetProcessId", "QueryFullProcessImageNameW", "GetProcessTimes", "WaitForSingleObject", "IsProcessInJob"):
        assert [args[0] for called, args in api.calls if called == name] == [handle]
    assert sum(name == "CloseHandle" and args[0] == handle for name, args in api.calls) == 1
    assert api.counts["CreateProcessW"] == api.counts["ResumeThread"] == api.counts["TerminateJobObject"] == 0
    assert invocation.cleanup_evidence().cleanup_completed and domain.snapshot().state is R.LaunchAdmissionState.OPEN
    assert not row.authorizes_continuation and row.parent_pid is None and row.parent_source == "deferred"


@pytest.mark.parametrize("error", [5, 87, 1168])
def test_open_failure_has_error_and_no_escalation_or_query(error):
    api = FakeBindings()
    api.failures[("OpenProcess", 1)] = (None, error)
    rows, api, _, _ = attest(api)
    row, = rows
    assert R.ProcessIdentityIssue.OPEN_PROCESS_FAILED in kinds(row)
    assert next(i.win32_error for i in row.issues if i.kind is R.ProcessIdentityIssue.OPEN_PROCESS_FAILED) == error
    assert row.confirmed_pid is row.image_path is row.creation_time is row.job_membership is None
    assert not row.handle_acquired and row.release_confirmed
    assert api.counts["OpenProcess"] == 1 and api.counts["GetProcessId"] == 0
    assert [args for name, args in api.calls if name == "OpenProcess"] == [(0x00101000, False, 42)]


def test_pid_mismatch_stops_all_identity_queries_and_closes():
    api = FakeBindings()
    api.observed_processes[42] = {"pid": 43}
    rows, api, _, _ = attest(api)
    row, = rows
    assert row.confirmed_pid == 43 and row.classification is R.ProcessIdentityClassification.PID_IDENTITY_RACE
    assert row.snapshot_link == "mismatch" and row.release_confirmed
    assert row.image_path is row.creation_time is row.job_membership is None
    assert api.counts["QueryFullProcessImageNameW"] == api.counts["GetProcessTimes"] == 0
    assert api.counts["WaitForSingleObject"] == api.counts["IsProcessInJob"] == 0


def test_reused_pid_generation_change_is_explicit_not_cached_identity():
    rows, api, _, _ = attest(twice=True)
    assert rows[0].creation_time != rows[1].creation_time
    assert rows[1].classification is R.ProcessIdentityClassification.PID_IDENTITY_RACE
    assert R.ProcessIdentityIssue.GENERATION_CHANGED in kinds(rows[1])
    assert rows[0].snapshot_link == "unproven" and rows[1].snapshot_link == "mismatch"
    assert len(observation_handles(api)) == 2 and all(h["closed"] for h in observation_handles(api).values())


@pytest.mark.parametrize("api_name,issue,absent", [
    ("GetProcessId", R.ProcessIdentityIssue.PID_QUERY_FAILED, "confirmed_pid"),
    ("QueryFullProcessImageNameW", R.ProcessIdentityIssue.IMAGE_QUERY_FAILED, "image_path"),
    ("GetProcessTimes", R.ProcessIdentityIssue.PROCESS_TIMES_FAILED, "creation_time"),
    ("IsProcessInJob", R.ProcessIdentityIssue.JOB_MEMBERSHIP_QUERY_FAILED, "job_membership"),
])
def test_query_failures_preserve_other_valid_facts(api_name, issue, absent):
    api = FakeBindings()
    api.failures[(api_name, 1)] = (0, 5)
    rows, api, _, _ = attest(api)
    row, = rows
    assert issue in kinds(row) and getattr(row, absent) is None and row.release_confirmed
    if api_name != "GetProcessId":
        assert row.confirmed_pid == 42 and row.liveness_state is R.ProcessLiveness.NOT_SIGNALED
    if api_name != "GetProcessId" and absent != "job_membership":
        assert row.job_membership is True
    assert row.classification is R.ProcessIdentityClassification.IDENTITY_QUERY_FAILED


@pytest.mark.parametrize("failure", ["insufficient", "full_size", "prefix"])
def test_image_buffer_failure_never_accepts_a_prefix(failure):
    import ctypes as C
    from terminal_win32_fakes import pointer
    api = FakeBindings()
    if failure == "insufficient":
        api.failures[("QueryFullProcessImageNameW", 1)] = (0, W.ERROR_INSUFFICIENT_BUFFER)
    elif failure == "full_size":
        def query(process, flags, output, size):
            pointer(size, W.DWORD)[0] = R.PROCESS_IMAGE_UTF16_CAPACITY
            return 1
        api._QueryFullProcessImageNameW = query
    else:
        original = api._QueryFullProcessImageNameW
        def query(process, flags, output, size):
            result = original(process, flags, output, size)
            pointer(size, W.DWORD)[0] = 3  # Buffer contains more characters than reported.
            return result
        api._QueryFullProcessImageNameW = query
    rows, _, _, _ = attest(api)
    assert rows[0].image_path is None and rows[0].creation_time is not None
    assert R.ProcessIdentityIssue.IMAGE_BUFFER_EXHAUSTED in kinds(rows[0]) or R.ProcessIdentityIssue.IMAGE_QUERY_FAILED in kinds(rows[0])


@pytest.mark.parametrize("state,expected", [(W.WAIT_OBJECT_0, R.ProcessLiveness.SIGNALED),
                                           (W.WAIT_TIMEOUT, R.ProcessLiveness.NOT_SIGNALED)])
def test_liveness_is_zero_time_observation_only(state, expected):
    api = FakeBindings()
    api.observed_processes[42] = {"wait": state}
    rows, api, _, _ = attest(api)
    assert rows[0].liveness_state is expected and not rows[0].authorizes_continuation
    assert [args[1] for name, args in api.calls if name == "WaitForSingleObject"] == [0]


def test_wait_failure_and_membership_false_are_separate():
    api = FakeBindings()
    api.failures[("WaitForSingleObject", 1)] = (W.WAIT_FAILED, 6)
    api.observed_processes[42] = {"member": False}
    rows, _, _, _ = attest(api)
    row, = rows
    assert row.liveness_state is R.ProcessLiveness.UNKNOWN and row.job_membership is False
    assert R.ProcessIdentityIssue.WAIT_FAILED in kinds(row)
    assert R.ProcessIdentityIssue.NOT_CURRENT_JOB_MEMBER in kinds(row)
    assert row.release_confirmed


def test_multiple_pids_are_independent_and_root_is_skipped():
    api = FakeBindings()
    api.failures[("OpenProcess", 1)] = (None, 5)
    rows, api, _, _ = attest(api, (1, 42, 43))
    assert [r.requested_pid for r in rows] == [42, 43]
    assert rows[0].confirmed_pid is None and rows[1].confirmed_pid == 43
    assert api.counts["OpenProcess"] == 2
    assert all(r.release_confirmed for r in rows)


@pytest.mark.parametrize("name", ["conhost.exe", "OpenConsole.exe"])
def test_console_image_is_candidate_only_even_in_untrusted_directory(name):
    api = FakeBindings()
    api.observed_processes[42] = {"path": "C:\\Untrusted\\" + name}
    rows, _, _, _ = attest(api)
    assert rows[0].classification is R.ProcessIdentityClassification.POSSIBLE_OS_COMPANION
    assert not rows[0].authorizes_continuation
    backend = FakeRuntimeBackend(FakeClock())
    backend.active_counts.extend([1, 0])
    backend.pid_snapshot = R.JobPidSnapshot((42,))
    backend.process_observations[42] = {"image_path": "C:\\Windows\\System32\\" + name}
    outcome, backend, _ = run_fake(backend)
    assert outcome.execution.reason_code == "non_root_job_member_after_root_exit"
    assert outcome.execution.job_termination_requested and outcome.execution.non_root_job_member_observed


def test_arbitrary_full_path_is_evidence_not_authority():
    rows, _, _, _ = attest()
    assert rows[0].classification is R.ProcessIdentityClassification.KNOWN_NON_ROOT_PROCESS
    assert not rows[0].authorizes_continuation


def test_capacity_exhaustion_is_explicit_not_a_job_limit():
    pids = tuple(range(100, 100 + R.NON_ROOT_OBSERVATION_CAPACITY + 2))
    rows, api, _, _ = attest(pids=pids)
    assert len(rows) == len(pids) and api.counts["OpenProcess"] == R.NON_ROOT_OBSERVATION_CAPACITY
    assert all(R.ProcessIdentityIssue.OBSERVATION_CAPACITY_EXHAUSTED in kinds(row) for row in rows[-2:])
    assert all(row.release_confirmed is None and not row.handle_acquired for row in rows[-2:])


def test_cu_never_queries_or_opens_process_identity():
    rows, api, invocation, domain = attest(cu=True)
    assert rows == () and invocation.creation.snapshot().classification is R.CreationClassification.CREATE_OUTCOME_UNCERTAIN
    assert api.counts["OpenProcess"] == api.counts["GetProcessId"] == api.counts["IsProcessInJob"] == 0
    assert domain.quarantine_owner is not None


@pytest.mark.parametrize("uncertain", [False, True])
def test_observation_close_failure_has_no_retry_and_quarantines(uncertain):
    api = FakeBindings()
    # In this fixture the first CloseHandle is the observation handle.
    api.failures[("CloseHandle", 1)] = KeyboardInterrupt() if uncertain else (0, 6)
    rows, api, invocation, domain = attest(api)
    row, = rows
    assert row.release_confirmed is False and R.ProcessIdentityIssue.HANDLE_RELEASE_UNCERTAIN in kinds(row)
    handle, = observation_handles(api)
    assert sum(name == "CloseHandle" and args[0] == handle for name, args in api.calls) == 1
    assert domain.quarantine_owner is not None and domain.snapshot().state is R.LaunchAdmissionState.POISONED
    assert not invocation.cleanup_evidence().cleanup_completed


def test_unavailable_open_result_never_uses_handle_and_quarantines():
    api = FakeBindings()
    api.failures[("OpenProcess", 1)] = KeyboardInterrupt()
    rows, api, _, domain = attest(api)
    assert R.ProcessIdentityIssue.OPEN_OUTCOME_UNCERTAIN in kinds(rows[0])
    assert rows[0].release_confirmed is False and api.counts["GetProcessId"] == 0
    assert domain.quarantine_owner is not None


def test_prebound_slots_are_single_use_and_ledger_rule_is_unchanged():
    def verify(backend, invocation, scope, job, runtime):
        with pytest.raises(R.TerminalLifecycleError):
            invocation.resources.reserve("late-observation")
        owner = scope.observation_handles[0]
        assert owner.snapshot().state is R.OwnershipState.RELEASED
        with pytest.raises(R.TerminalLifecycleError):
            invocation.resources.borrow(owner)
        foreign = W._Handle(backend)
        with pytest.raises(R.TerminalLifecycleError):
            backend.observe_process(foreign, job._identity_payload_unlocked(), 43, 2, None,
                                    backend._api.clock, 105.0, lambda: True)
    rows, api, _, _ = attest(after=verify)
    assert api.counts["OpenProcess"] == 1


def test_deadline_does_not_reset_and_stops_later_native_queries():
    api = FakeBindings()
    original = api._GetProcessId
    def pid(handle):
        result = original(handle)
        api.clock.advance(5)
        return result
    api._GetProcessId = pid
    rows, api, _, _ = attest(api)
    assert rows[0].confirmed_pid == 42 and rows[0].image_path is None
    assert R.ProcessIdentityIssue.OBSERVATION_STOPPED in kinds(rows[0])
    assert api.counts["QueryFullProcessImageNameW"] == api.counts["GetProcessTimes"] == 0
    assert rows[0].release_confirmed


def test_immutable_partial_evidence_rejects_raw_handle_and_fake_facts():
    row = R.NonRootProcessEvidence(42, 1, 100.0, 100.0)
    assert row.classification is R.ProcessIdentityClassification.UNKNOWN_NON_ROOT
    with pytest.raises(FrozenInstanceError):
        row.image_path = "changed"
    with pytest.raises(ValueError):
        replace(row, confirmed_pid=42)
    with pytest.raises(TypeError):
        R.NonRootProcessEvidence(42, 1, 100.0, 100.0, raw_handle=123)
    assert not hasattr(R.ProcessIdentityClassification, "CONFIRMED_EXPECTED_OS_COMPANION")


def test_root_only_snapshot_does_not_open_an_observation_handle():
    rows, api, _, _ = attest(pids=(1,))
    assert rows == () and api.counts["OpenProcess"] == 0
    assert not observation_handles(api)
    root = R.NonRootProcessEvidence(1, 1, 100.0, 100.0, known_root=True,
                                    snapshot_link="root_receipt")
    assert root.classification is R.ProcessIdentityClassification.KNOWN_ROOT
    assert not root.authorizes_continuation


def test_parent_query_is_explicitly_deferred_without_fallback():
    rows, _, _, _ = attest()
    assert rows[0].parent_pid is None
    assert rows[0].parent_source == "deferred"
    assert rows[0].parent_issue == "parent_attestation_deferred"
    assert not {"CreateToolhelp32Snapshot", "NtQueryInformationProcess"}.intersection(W.API_SIGNATURES)


@pytest.mark.parametrize("path", ["conhost.exe", "relative\\tool.exe", "C:relative.exe"])
def test_non_full_image_output_is_unknown_and_other_facts_survive(path):
    api = FakeBindings()
    api.observed_processes[42] = {"path": path}
    rows, _, _, _ = attest(api)
    assert rows[0].image_path is None and rows[0].creation_time is not None
    assert R.ProcessIdentityIssue.IMAGE_QUERY_FAILED in kinds(rows[0])
    assert rows[0].release_confirmed


def test_creation_time_zero_is_not_fabricated_generation_evidence():
    api = FakeBindings()
    api.observed_processes[42] = {"creation_time": 0}
    rows, _, _, _ = attest(api)
    assert rows[0].creation_time is None and R.ProcessIdentityIssue.PROCESS_TIMES_FAILED in kinds(rows[0])


def test_native_entry_rejects_cu_even_with_prebound_token():
    def verify(backend, invocation, scope, job, runtime):
        with invocation.resources.borrow(scope.observation_handles[0]) as token:
            with invocation.resources.borrow(job) as job_token:
                with pytest.raises(R.TerminalLifecycleError):
                    backend.observe_process(token.payload, job_token.payload, 42, 1, None,
                                            backend._api.clock, 105.0, lambda: True)
    rows, api, _, _ = attest(cu=True, after=verify)
    assert rows == () and api.counts["OpenProcess"] == 0


def test_full_image_spelling_and_lossless_maximum_time_are_preserved():
    api = FakeBindings()
    path = r"C:\WINDOWS\System32\ConHost.EXE"
    api.observed_processes[42] = {"path": path, "creation_time": 0xFFFFFFFFFFFFFFFF}
    rows, _, _, _ = attest(api)
    assert rows[0].image_path == path and rows[0].creation_time == 0xFFFFFFFFFFFFFFFF


def test_runtime_exports_attestation_without_relaxing_containment():
    backend = FakeRuntimeBackend(FakeClock())
    backend.active_counts.extend([1, 0])
    backend.pid_snapshot = R.JobPidSnapshot((1, 42, 43))
    outcome, backend, _ = run_fake(backend)
    first = outcome.execution.post_root_job_observations[0]
    assert first.state is R.PostRootJobState.NON_ROOT_JOB_MEMBER_AFTER_ROOT_EXIT
    assert [e.requested_pid for e in first.process_evidence] == [42, 43]
    assert all(e.release_confirmed and not e.authorizes_continuation for e in first.process_evidence)
    assert outcome.execution.job_termination_requested and outcome.execution.reason_code == "non_root_job_member_after_root_exit"
    assert outcome.cleanup_completed
    assert sum(e[0] == "prepare_observation_handles" for e in backend.events) == 1


def test_membership_false_is_not_misreported_as_native_query_failure_or_proven_reuse():
    api = FakeBindings()
    api.observed_processes[42] = {"member": False}
    rows, _, _, _ = attest(api)
    row, = rows
    assert row.job_membership is False and row.image_path is not None
    assert row.classification is R.ProcessIdentityClassification.UNKNOWN_NON_ROOT
    assert R.ProcessIdentityIssue.NOT_CURRENT_JOB_MEMBER in kinds(row)
    assert R.ProcessIdentityIssue.JOB_MEMBERSHIP_QUERY_FAILED not in kinds(row)
    assert R.ProcessIdentityIssue.PID_MISMATCH not in kinds(row)


def test_same_numeric_job_handle_in_foreign_wrapper_is_not_ledger_authority():
    def verify(backend, invocation, scope, job, runtime):
        with invocation.resources.borrow(scope.observation_handles[1]) as token:
            with invocation.resources.borrow(job) as original:
                foreign = W._Handle(backend)
                foreign._value = original.payload._raw()
                foreign._resolved = foreign._acquired = True
                with pytest.raises(R.TerminalLifecycleError):
                    backend.observe_process(token.payload, foreign, 43, 2, None,
                                            backend._api.clock, 105.0, lambda: True)
    rows, api, _, _ = attest(after=verify)
    assert api.counts["OpenProcess"] == 1


def test_cancellation_stops_acquisition_without_budget_reset():
    def verify(backend, invocation, scope, job, runtime):
        invocation._domain.cancel_active()
        rows = runtime._attest_members(invocation, scope, job, R.JobPidSnapshot((43,)))
        assert rows[0].confirmed_pid is None and rows[0].release_confirmed
        assert R.ProcessIdentityIssue.OBSERVATION_STOPPED in kinds(rows[0])
        assert invocation.snapshot().cleanup_deadline == 105.0
    rows, api, _, _ = attest(after=verify)
    assert api.counts["OpenProcess"] == 1


def test_none_query_output_is_an_explicit_issue():
    api = FakeBindings()
    api._GetProcessId = lambda handle: None
    rows, _, _, _ = attest(api)
    assert rows[0].confirmed_pid is None and R.ProcessIdentityIssue.PID_QUERY_FAILED in kinds(rows[0])
