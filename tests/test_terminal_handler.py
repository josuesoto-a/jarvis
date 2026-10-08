"""Unregistered adapter result projection, exact object forwarding and Executor."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from capabilities.terminal_handler import create_terminal_handler, terminal_step_result
from capabilities.terminal_runtime import CleanupReason, RuntimeDisposition
from core.contracts import ActionStatus, ExecutionStep
from core.executor import Executor, StepExecutionResult
from core.runtime import CapabilityRuntimeRegistry
from terminal_runtime_fakes import FakeClock, FakeRuntimeBackend, run_fake, target_for, forbid_live_loader


def test_adapter_requires_explicit_runtime():
    with pytest.raises(TypeError):
        create_terminal_handler()
    with pytest.raises(TypeError):
        create_terminal_handler(runtime=None)


def test_exact_same_target_object_is_forwarded_and_explicit_result_returned():
    outcome, _, _ = run_fake()
    seen = []
    runtime = SimpleNamespace(execute=lambda target: seen.append(target) or outcome)
    handler = create_terminal_handler(runtime=runtime, step_number=4)
    target = target_for()
    result = handler({"target": target})
    assert seen == [target] and seen[0] is target
    assert type(result) is StepExecutionResult
    assert result.step_number == 4 and result.capability == "terminal"
    assert result.status is ActionStatus.COMPLETED and result.error is None
    assert result.data["stdout"] == "out" and result.data["exit_code"] == 0
    assert result.data["creation_classification"] == "P1"
    assert "environment" not in result.data
    assert all("handle" not in key for key in result.data)


@pytest.mark.parametrize("arguments", [None, {}, {"target": object(), "extra": 1}, {"argv": ()}])
def test_bad_input_is_explicit_failed_and_runtime_not_called(arguments):
    calls = []
    handler = create_terminal_handler(runtime=SimpleNamespace(execute=lambda target: calls.append(target)))
    result = handler(arguments)
    assert result.status is ActionStatus.FAILED and calls == []
    assert result.data["reason_code"] == "invalid_handler_arguments"


@pytest.mark.parametrize("creation", ["C1", "CU", "P1"])
def test_runtime_failure_is_failed_to_existing_executor_not_ordinary_mapping(creation):
    backend = FakeRuntimeBackend(FakeClock(), creation=creation)
    backend.exit = 7
    outcome, _, _ = run_fake(backend)
    registry = CapabilityRuntimeRegistry()
    registry.register("terminal", create_terminal_handler(runtime=SimpleNamespace(execute=lambda target: outcome)))
    executor = Executor(validator=object(), permission_engine=object(), runtime_registry=registry)
    result = executor._run_handler(ExecutionStep(1, "fake", "terminal"), {"target": target_for()})
    assert type(result) is StepExecutionResult and result.status is ActionStatus.FAILED
    assert result.data["creation_classification"] == creation
    assert result.error == "Terminal execution failed."
    if creation == "CU":
        assert not result.data["process_created"] and not result.data["execution_started"]
        assert not result.data["cleanup_completed"] and result.data["job_empty"]
    if creation == "P1":
        assert result.data["exit_code"] == 7 and result.data["stdout"] == "out"


def test_timeout_projection_keeps_partial_capture_and_trigger():
    backend = FakeRuntimeBackend(FakeClock())
    backend.keep_running = True
    backend.hooks["wait"] = lambda: backend.clock.advance(31)
    outcome, _, _ = run_fake(backend)
    result = terminal_step_result(outcome, step_number=1)
    assert result.status is ActionStatus.FAILED and result.data["timed_out"]
    assert result.data["termination_reason"] == "execution_timeout"
    assert result.data["process_created"] and result.data["process_resumed"]


def test_lifecycle_only_result_cannot_be_misprojected_as_execution():
    outcome, _, _ = run_fake()
    with pytest.raises(TypeError):
        terminal_step_result(replace(outcome, execution=None), step_number=1)


@pytest.mark.parametrize("number", [0, -1, True, "1"])
def test_invalid_step_number_rejected(number):
    with pytest.raises(ValueError):
        create_terminal_handler(runtime=SimpleNamespace(execute=lambda target: None), step_number=number)
