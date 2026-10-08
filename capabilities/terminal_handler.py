"""Explicitly injected terminal result adapter. Never registered by production."""
from collections.abc import Mapping

from capabilities.terminal_runtime import RuntimeDisposition, TerminalRuntimeOutcome
from core.contracts import ActionStatus
from core.executor import StepExecutionResult


def terminal_step_result(outcome: TerminalRuntimeOutcome, *, step_number: int) -> StepExecutionResult:
    if type(outcome) is not TerminalRuntimeOutcome or outcome.execution is None:
        raise TypeError("adapter requires actual runtime execution/capture evidence")
    evidence = outcome.execution
    data = {
        "exit_code": evidence.exit_code,
        "stdout": evidence.stdout.text, "stderr": evidence.stderr.text,
        "execution_started": outcome.process_created,
        "process_created": outcome.process_created,
        "process_resumed": outcome.process_resumed,
        "process_exited": outcome.process_exited,
        "creation_classification": outcome.creation_classification.value,
        "cleanup_completed": outcome.cleanup_completed,
        "job_empty": outcome.cleanup.job_empty,
        "timed_out": outcome.terminal_trigger.value == "execution_timeout",
        "termination_reason": outcome.terminal_trigger.value,
        "descendants_terminated": evidence.descendants_terminated,
        "verified_executable_identity": evidence.verified_executable_identity,
        "environment_identity": evidence.environment_identity,
        "duration_seconds": evidence.duration_seconds,
        "failure_stage": evidence.failure_stage, "reason_code": evidence.reason_code,
        "serialization_policy": "windows-crt-argv/v1",
        "decoding_policy": "utf8-replace/v1", "encoding": "utf-8", "errors": "replace",
    }
    for name, capture in (("stdout", evidence.stdout), ("stderr", evidence.stderr)):
        data.update({name + "_truncated": capture.truncated, name + "_observed_bytes": capture.observed,
                     name + "_retained_bytes": capture.retained, name + "_eof": capture.eof,
                     name + "_complete": capture.complete, name + "_saturated": capture.saturated})
    completed = outcome.disposition is RuntimeDisposition.COMPLETED
    return StepExecutionResult(step_number, "terminal",
                               ActionStatus.COMPLETED if completed else ActionStatus.FAILED,
                               data, None if completed else "Terminal execution failed.")


def create_terminal_handler(*, runtime, step_number: int = 1):
    """No default runtime/backend. Forward the exact approved object."""
    if runtime is None or not callable(getattr(runtime, "execute", None)):
        raise TypeError("explicit terminal runtime required")
    if type(step_number) is not int or step_number < 1:
        raise ValueError("positive step number required")

    def handler(arguments):
        if not isinstance(arguments, Mapping) or set(arguments) != {"target"}:
            return StepExecutionResult(step_number, "terminal", ActionStatus.FAILED,
                                       {"reason_code": "invalid_handler_arguments"},
                                       "Terminal handler requires exactly target.")
        return terminal_step_result(runtime.execute(arguments["target"]), step_number=step_number)

    return handler
