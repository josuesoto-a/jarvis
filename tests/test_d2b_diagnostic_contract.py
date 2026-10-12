"""D2b contract/instrumentation checks: no live diagnostic import or child."""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("d2b_contract", ROOT / "scripts/d2b_contract.py")
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def evidence():
    return json.loads((ROOT / "diagnostics/d1g_d2a_result.json").read_text(encoding="utf-8-sig"))


def test_frozen_target_preserves_exact_d2a_projection():
    from capabilities.terminal import terminal_approval_arguments
    source = evidence()
    before = deepcopy(source)
    target = CONTRACT.frozen_target(source)
    assert source == before
    assert terminal_approval_arguments(target) == source["proposed_d2b_target"]["approval_arguments"]
    assert target.argv == CONTRACT.ARGV


@pytest.mark.parametrize("field,value", [
    ("status", "STOP"), ("cleanup_completed", False), ("domain_state", "poisoned"),
    ("quarantined", True), ("poisoned", True), ("pending_io", 1),
    ("native_remaining", {"handles": 1}),
])
def test_d2a_failure_blocks_target(field, value):
    source = evidence()
    source[field] = value
    with pytest.raises(RuntimeError, match="D2a gate"):
        CONTRACT.frozen_target(source)


def test_changed_harmless_script_still_rejected():
    source = evidence()
    source["proposed_d2b_target"]["target"]["argv"] = ["--version"]
    with pytest.raises(RuntimeError, match="projection"):
        CONTRACT.frozen_target(source)


@pytest.mark.parametrize("change", [
    {"text": "D2b stdout\r\n"}, {"text": "extra\nD2b stdout\n"},
    {"eof": False}, {"complete": False}, {"truncated": True},
    {"saturated": True}, {"observed": 100}, {"retained": 0},
])
def test_capture_rejects_normalization_extra_output_and_incomplete_evidence(change):
    capture = dict(text="D2b stdout\n", observed=11, retained=11, eof=True,
                   complete=True, truncated=False, saturated=False)
    capture.update(change)
    with pytest.raises(RuntimeError, match="deterministic output"):
        CONTRACT.exact_capture(SimpleNamespace(**capture), "D2b stdout\n")


def test_capture_accepts_only_exact_frozen_bytes():
    CONTRACT.exact_capture(SimpleNamespace(text="D2b stdout\n", observed=11, retained=11,
        eof=True, complete=True, truncated=False, saturated=False), "D2b stdout\n")


def harness_tree():
    return ast.parse((ROOT / "scripts/live_d1g_d2b_first_child.py").read_text(encoding="utf-8-sig"))


def test_p1_instrumentation_first_statement_is_authoritative_commit():
    function = next(n for n in ast.walk(harness_tree()) if isinstance(n, ast.FunctionDef) and n.name == "committed_success")
    assert isinstance(function.body[0], ast.Expr)
    assert ast.unparse(function.body[0]) == "original_success()"


def test_native_dispatch_returns_directly_without_post_native_instrumentation():
    native_create = next(n for n in ast.walk(harness_tree()) if isinstance(n, ast.FunctionDef) and n.name == "native_create")
    dispatch = native_create.body[0]
    assert isinstance(dispatch.body[-1], ast.Return)
    assert ast.unparse(dispatch.body[-1]) == "return function(*arguments)"
    assert not any(isinstance(n, ast.Try) for n in ast.walk(dispatch))


def test_live_harness_requires_phase_ack_and_has_no_import_time_main_call():
    tree = harness_tree()
    assert isinstance(tree.body[-1], ast.If)
    assert ast.unparse(tree.body[-1].test) == "__name__ == '__main__'"
    assert "--acknowledge" in ast.unparse(tree)
    assert "D1G-D2B-ONE-CHILD" in ast.unparse(tree)
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and any(a.name.split(".")[0] == "subprocess" for a in n.names) for n in ast.walk(tree))



@pytest.mark.parametrize("creation", ["P1", "C1", "CU"])
def test_diagnostic_backend_and_dispatch_guards_with_in_memory_win32(monkeypatch, tmp_path, creation):
    # Extract definitions only. Never import/run main or load a native DLL.
    from collections import Counter, deque
    import ctypes as C
    from dataclasses import replace, asdict
    from hashlib import sha256
    import struct
    import capabilities.terminal as T
    import capabilities.terminal_runtime as R
    import capabilities.terminal_win32 as W
    from terminal_fixtures import console_pe_bytes
    from terminal_runtime_fakes import target_for, on_owner
    from terminal_win32_fakes import FakeBindings, pointer

    monkeypatch.setattr(W, "_load_live_bindings", lambda: pytest.fail("live loading forbidden"))
    api = FakeBindings(creation=creation)
    api.image = console_pe_bytes() + bytes(103192 - len(console_pe_bytes()))
    api.outputs = {"stdout": deque([b"D2b stdout\n", None]), "stderr": deque([b"D2b stderr\n", None])}
    target = replace(target_for(api.image), executable_requested=CONTRACT.PYTHON,
                     executable_resolved=CONTRACT.PYTHON, cwd=CONTRACT.CWD, argv=CONTRACT.ARGV)
    counts = dict(wrapper_attempts=0, native_dispatches=0, known_successes=0, known_failures=0, uncertain_outcomes=0)
    report = {"creation_counts": counts, "resume_counts": {"native_dispatches": 0, "result": None}}
    live = {k: set() for k in ("handles", "heap", "local", "attributes")}
    acquired, released, calls = Counter(), Counter(), Counter()
    namespace = dict(C=C, W=W, R=R, T=T, report=report, counts=counts, live=live,
                     acquired=acquired, released=released, calls=calls, target=target,
                     require=CONTRACT.require, sha256=sha256, struct=struct, json=json, asdict=asdict,
                     reservation=tmp_path / "one_attempt.json", os=__import__("os"),
                     PYTHON=CONTRACT.PYTHON, DIGEST=sha256(api.image).hexdigest(),
                     events=[], release_order=[])
    definitions = [n for n in ast.walk(harness_tree())
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                   and n.name in {"native_create", "native_resume", "value", "observe", "Backend"}]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "<D2b definitions only>", "exec"), namespace)
    original_query = api._QueryInformationJobObject
    def query(job, kind, info, size, length):
        if kind == W.JobObjectExtendedLimitInformation:
            C.memmove(info, api.job_policy, len(api.job_policy))
            pointer(length, W.DWORD)[0] = C.sizeof(W.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
            return 1
        return original_query(job, kind, info, size, length)
    api._QueryInformationJobObject = query
    api.GetHandleInformation = lambda handle, output: (pointer(output, W.DWORD).__setitem__(0, int(api.handles[handle]["inheritable"])) or 1)
    for name in W.API_SIGNATURES:
        function = getattr(api, name)
        if name == "CreateProcessW":
            function = namespace["native_create"](function)
        elif name == "ResumeThread":
            function = namespace["native_resume"](function)
        else:
            function = namespace["observe"](name, function)
        setattr(api, name, function)
    def execute(owner):
        backend = namespace["Backend"](owner_thread=owner, bindings=api)
        domain = R.LaunchAdmissionDomain(clock=api.clock)
        return R.WindowsTerminalRuntime(backend=backend, admission_domain=domain, owner_thread=owner).execute(target), domain
    outcome, domain = on_owner(execute)
    assert counts["wrapper_attempts"] == counts["native_dispatches"] == 1, (outcome.execution, report, dict(api.counts))
    assert outcome.creation_classification.value == creation
    if creation == "P1":
        assert outcome.disposition is R.RuntimeDisposition.COMPLETED
        assert counts["known_successes"] == 1 and counts["known_failures"] == 0
        assert report["resume_counts"] == {"native_dispatches": 1, "result": 1}
        assert acquired == released and not any(live.values())
        assert namespace["release_order"][-1] == "job"
        CONTRACT.exact_capture(outcome.execution.stdout, "D2b stdout\n")
        CONTRACT.exact_capture(outcome.execution.stderr, "D2b stderr\n")
    else:
        assert report["resume_counts"]["native_dispatches"] == 0
        assert counts["known_successes"] == 0
        if creation == "C1":
            assert counts["known_failures"] == 1 and acquired == released
        else:
            assert domain.quarantine_owner is not None and live["handles"]



