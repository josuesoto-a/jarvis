"""Pure validator regressions; no native acquisition or process control."""
import pytest

from capabilities import terminal_runtime as R


def complete_evidence(path):
    return R.NonRootProcessEvidence(
        42, 1, 100.0, 100.0, confirmed_pid=42, image_path=path,
        creation_time=1, liveness_state=R.ProcessLiveness.NOT_SIGNALED,
        job_membership=True, handle_acquired=True)


@pytest.mark.parametrize("state", [R.ProcessLiveness.NOT_SIGNALED, R.ProcessLiveness.SIGNALED])
def test_original_liveness_reproduction_rejects_unacquired_handle(state):
    with pytest.raises(ValueError, match="acquired observation handle"):
        R.NonRootProcessEvidence(42, 1, 100.0, 100.0, liveness_state=state)


def test_original_unc_reproduction_cannot_establish_known_identity():
    path = "\\\\server\\\\"
    assert not R.full_process_image_path(path)
    with pytest.raises(ValueError, match="full image path"):
        complete_evidence(path)


@pytest.mark.parametrize("state", list(R.ProcessLiveness))
def test_acquired_handle_preserves_each_liveness_state(state):
    row = R.NonRootProcessEvidence(42, 1, 100.0, 100.0,
                                  handle_acquired=True, liveness_state=state)
    assert row.liveness_state is state and not row.authorizes_continuation


@pytest.mark.parametrize("kind", [None, R.ProcessIdentityIssue.OPEN_PROCESS_FAILED,
                                  R.ProcessIdentityIssue.WAIT_FAILED])
def test_unknown_liveness_without_handle_remains_representable(kind):
    issues = () if kind is None else (R.ProcessObservationIssue(kind, "observation"),)
    row = R.NonRootProcessEvidence(42, 1, 100.0, 100.0, issues=issues)
    assert not row.handle_acquired and row.liveness_state is R.ProcessLiveness.UNKNOWN
    assert row.issues == issues and not row.authorizes_continuation


@pytest.mark.parametrize("state", [R.ProcessLiveness.NOT_SIGNALED, R.ProcessLiveness.SIGNALED])
def test_root_label_does_not_exempt_unacquired_liveness(state):
    with pytest.raises(ValueError, match="acquired observation handle"):
        R.NonRootProcessEvidence(42, 1, 100.0, 100.0, known_root=True,
                                  snapshot_link="root_receipt", liveness_state=state)


INVALID_PATHS = (
    "\\\\server", "\\\\server\\", "\\\\server\\\\", "\\\\server\\share",
    "\\\\server\\share\\", "\\\\\\share\\tool.exe", "\\\\server\\\\tool.exe",
    "\\\\server\\share\\\\tool.exe", "\\\\server\\share\\dir\\\\tool.exe",
    "\\\\server\\share\\dir\\", "\\\\server\\share\\tool.exe\\",
    "\\\\?\\UNC\\server", "\\\\?\\UNC\\server\\share",
    "\\\\?\\UNC\\server\\share\\", "\\\\?\\UNC\\\\share\\tool.exe",
    "\\\\?\\UNC\\server\\\\tool.exe", "\\\\?\\UNC\\server\\share\\\\tool.exe",
    "\\\\?\\unc\\server\\share", "\\\\?\\C:\\", "\\\\?\\C:relative\\tool.exe",
    "C:\\", "C:tool.exe", "tool.exe",
)


@pytest.mark.parametrize("path", INVALID_PATHS)
def test_missing_or_empty_path_components_cannot_establish_identity(path):
    assert not R.full_process_image_path(path)
    with pytest.raises(ValueError, match="full image path"):
        complete_evidence(path)


VALID_PATHS = (
    r"C:\Tools\member.exe", r"c:\Program Files\Tools\member.exe", "C:/Tools/member.exe",
    r"\\server\share\member.exe", r"\\server\share\directory\member.exe",
    r"\\?\C:\Tools\member.exe", r"\\?\UNC\server\share\member.exe",
    r"\\?\unc\server\share\directory\member.exe",
    r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\Tools\member.exe",
)


@pytest.mark.parametrize("path", VALID_PATHS)
def test_full_windows_image_paths_preserved_without_authority(path):
    assert R.full_process_image_path(path)
    row = complete_evidence(path)
    assert row.image_path == path
    assert row.classification is R.ProcessIdentityClassification.KNOWN_NON_ROOT_PROCESS
    assert not row.authorizes_continuation and row.snapshot_link == "unproven"


@pytest.mark.parametrize("path", [r"\\server\share\conhost.exe",
                                  r"\\?\UNC\server\share\OpenConsole.exe"])
def test_valid_console_image_remains_hypothesis_only(path):
    row = complete_evidence(path)
    assert row.classification is R.ProcessIdentityClassification.POSSIBLE_OS_COMPANION
    assert not row.authorizes_continuation


@pytest.mark.parametrize("path", [
    "C:\\Tools\\\\member.exe", "\\\\?\\C:\\Tools\\member.exe\\",
    "\\\\?\\Volume{}\\member.exe",
    "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\",
    "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\\\member.exe",
])
def test_empty_extended_or_local_executable_components_are_rejected(path):
    assert not R.full_process_image_path(path)
    with pytest.raises(ValueError):
        complete_evidence(path)


@pytest.mark.parametrize("path,valid", [
    ("\\\\server\\\\", False),
    ("\\\\?\\UNC\\server\\share\\", False),
    (r"\\server\share\member.exe", True),
    (r"\\?\UNC\server\share\member.exe", True),
])
def test_native_fake_image_validation_preserves_other_observations(path, valid):
    from test_terminal_process_attestation import attest, kinds
    from terminal_win32_fakes import FakeBindings
    api = FakeBindings()
    api.observed_processes[42] = {"path": path}
    rows, api, _, _ = attest(api)
    row, = rows
    assert row.confirmed_pid == 42 and row.creation_time is not None
    assert row.job_membership is True and row.release_confirmed
    assert row.liveness_state is R.ProcessLiveness.NOT_SIGNALED
    assert row.image_path == (path if valid else None)
    assert row.classification is (R.ProcessIdentityClassification.KNOWN_NON_ROOT_PROCESS
                                  if valid else R.ProcessIdentityClassification.IDENTITY_QUERY_FAILED)
    assert (R.ProcessIdentityIssue.IMAGE_QUERY_FAILED in kinds(row)) is not valid
    assert not row.authorizes_continuation and row.snapshot_link == "unproven"
    assert api.counts["CreateProcessW"] == api.counts["ResumeThread"] == api.counts["TerminateJobObject"] == 0
