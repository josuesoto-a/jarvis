from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from capabilities.terminal import (
    TERMINAL_ENVIRONMENT_POLICY_ID,
    TERMINAL_PLATFORM_CONTRACT,
    TERMINAL_STDIN_MODE,
    TERMINAL_TARGET_CONTRACT_VERSION,
    TERMINAL_TIMEOUT_SECONDS,
    TerminalContractError,
    TerminalExecutionRequest,
    prepare_terminal_execution,
)
from core.contracts import (
    ActionRequest,
    ArgumentSource,
    CapabilitySpec,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.executor import Executor
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.planner import (
    Planner,
    PlannerArgumentDraft,
    PlannerDraft,
    PlannerStepDraft,
)
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry


class _FakeResponse:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed


class _FakeResponses:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed

    def parse(self, **_kwargs):
        return _FakeResponse(self.output_parsed)


class _FakeClient:
    def __init__(self, output_parsed):
        self.responses = _FakeResponses(output_parsed)


def _terminal_request(
    tmp_path: Path,
    *,
    executable: str | None = None,
    argv: list[str] | tuple[str, ...] = (),
) -> TerminalExecutionRequest:
    return TerminalExecutionRequest(
        executable=executable or sys.executable,
        argv=argv,
        cwd=str(tmp_path),
    )


def test_literal_string_sequence_is_ordered_and_immutable():
    source = ["first", "", "third value"]
    argument = ExecutionArgument.literal_string_sequence(source)
    source.append("mutated")

    assert argument.source == ArgumentSource.LITERAL
    assert argument.value == ("first", "", "third value")
    assert type(argument.value) is tuple

    with pytest.raises(FrozenInstanceError):
        argument.value = ()


def test_literal_string_sequence_rejects_malformed_values():
    with pytest.raises(TypeError):
        ExecutionArgument.literal_string_sequence("--version")
    with pytest.raises(TypeError):
        ExecutionArgument.literal_string_sequence(["ok", 1])
    with pytest.raises(ValueError, match="NUL"):
        ExecutionArgument.literal_string_sequence(["bad\x00argument"])

    with pytest.raises(ValidationError):
        PlannerArgumentDraft(
            name="argv",
            source=ArgumentSource.LITERAL,
            value=["ok", {"not": "a string"}],
            step_number=None,
            output_key=None,
        )


def test_scalar_and_previous_output_arguments_are_preserved():
    scalar = ExecutionArgument.literal("  query  ")
    previous = ExecutionArgument.step_output(1, "best_result_url")

    assert scalar.value == "query"
    assert previous.value is None
    assert previous.source == ArgumentSource.STEP_OUTPUT
    assert previous.step_number == 1
    assert previous.output_key == "best_result_url"


def test_planner_preserves_generic_string_sequence_order():
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            name="example",
            description="Accept generic structured values.",
        )
    )
    draft = PlannerDraft(
        steps=[
            PlannerStepDraft(
                description="Preserve ordered items",
                capability="example",
                arguments=[
                    PlannerArgumentDraft(
                        name="items",
                        source=ArgumentSource.LITERAL,
                        value=["first", "", "third value"],
                        step_number=None,
                        output_key=None,
                    )
                ],
                risk=RiskLevel.LOW,
                permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            )
        ]
    )
    planner = Planner(registry, client=_FakeClient(draft))
    plan = planner.plan(
        ActionRequest(goal="Preserve items", raw_input="preserve items")
    )

    assert plan.steps[0].arguments["items"].value == (
        "first",
        "",
        "third value",
    )


def test_planner_preserves_step_output_regardless_of_argument_name():
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            name="terminal",
            description="Prepare a structured terminal action.",
        )
    )
    draft = PlannerDraft(
        steps=[
            PlannerStepDraft(
                description="Produce a generic value",
                capability="terminal",
                arguments=[],
                risk=RiskLevel.LOW,
                permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            ),
            PlannerStepDraft(
                description="Use generated argv",
                capability="terminal",
                arguments=[
                    PlannerArgumentDraft(
                        name="argv",
                        source=ArgumentSource.STEP_OUTPUT,
                        value=None,
                        step_number=1,
                        output_key="argv",
                    )
                ],
                risk=RiskLevel.LOW,
                permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            )
        ]
    )
    planner = Planner(registry, client=_FakeClient(draft))

    plan = planner.plan(ActionRequest(goal="test", raw_input="test"))

    argument = plan.steps[1].arguments["argv"]
    assert argument.source == ArgumentSource.STEP_OUTPUT
    assert argument.value is None
    assert argument.step_number == 1
    assert argument.output_key == "argv"


def test_executor_resolves_structured_argv_without_conversion():
    observed = []
    capabilities = CapabilityRegistry()
    capabilities.register(CapabilitySpec("example", "Test structured capability"))
    runtimes = CapabilityRuntimeRegistry()
    runtimes.register(
        "example",
        lambda arguments: observed.append(arguments["argv"]) or {"ok": True},
    )
    executor = Executor(
        validator=PlanValidator(CapabilityResolver(capabilities)),
        permission_engine=PermissionEngine(),
        runtime_registry=runtimes,
    )
    request = ActionRequest(goal="test", raw_input="test")
    plan = ExecutionPlan(
        request_id=request.request_id,
        steps=(
            ExecutionStep(
                1,
                "Test argv",
                "example",
                arguments={
                    "argv": ExecutionArgument.literal_string_sequence(
                        ["one", "two", "three"]
                    )
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.CONFIRM_BEFORE_EXECUTION,
            ),
        ),
        overall_risk=RiskLevel.LOW,
    )

    report = executor.execute(plan, confirmed_steps=frozenset({1}))

    assert report.completed
    assert observed == [("one", "two", "three")]


def test_terminal_request_accepts_empty_and_shell_looking_argv(tmp_path):
    empty = _terminal_request(tmp_path)
    literal = _terminal_request(
        tmp_path,
        argv=["&&", "|", ">", "$(not-expanded)", "%PATH%"],
    )

    assert empty.argv == ()
    assert literal.argv == (
        "&&",
        "|",
        ">",
        "$(not-expanded)",
        "%PATH%",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executable", ""),
        ("executable", "bad\x00.exe"),
        ("cwd", "bad\x00path"),
    ],
)
def test_terminal_request_rejects_empty_or_nul_fields(tmp_path, field, value):
    values = {
        "executable": sys.executable,
        "argv": [],
        "cwd": str(tmp_path),
    }
    values[field] = value

    with pytest.raises(TerminalContractError):
        TerminalExecutionRequest(**values)

    with pytest.raises(TerminalContractError, match="NUL"):
        _terminal_request(tmp_path, argv=["bad\x00argument"])


def test_terminal_request_is_immutable(tmp_path):
    request = _terminal_request(tmp_path, argv=["--version"])
    with pytest.raises(FrozenInstanceError):
        request.cwd = "C:\\elsewhere"


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_cwd_is_required_absolute_existing_directory(tmp_path):
    for invalid in ("relative", "C:drive-relative", "/c/git-bash"):
        with pytest.raises(TerminalContractError, match="absolute"):
            prepare_terminal_execution(
                TerminalExecutionRequest(
                    executable=sys.executable,
                    argv=[],
                    cwd=invalid,
                )
            )

    missing = tmp_path / "missing-directory"
    with pytest.raises(TerminalContractError, match="does not exist"):
        prepare_terminal_execution(
            _terminal_request(missing)
        )

    file_path = tmp_path / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(TerminalContractError, match="directory"):
        prepare_terminal_execution(
            _terminal_request(file_path)
        )


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_target_canonicalizes_cwd_and_resolves_absolute_executable(tmp_path):
    target = prepare_terminal_execution(
        _terminal_request(tmp_path, argv=["--version"])
    )

    assert target.cwd == str(tmp_path.resolve(strict=True))
    assert Path(target.executable_resolved).is_absolute()
    assert Path(target.executable_resolved).samefile(sys.executable)
    assert target.executable_identity.startswith("sha256:")
    assert len(target.executable_identity) == 71


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_bare_executable_resolution_uses_frozen_path(monkeypatch, tmp_path):
    executable = Path(sys.executable)
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setenv("PATHEXT", ".EXE")

    target = prepare_terminal_execution(
        _terminal_request(tmp_path, executable=executable.stem)
    )

    assert Path(target.executable_resolved).samefile(executable)


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_environment_is_small_frozen_versioned_and_excludes_secrets(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-escape")
    monkeypatch.setenv("PYTHONPATH", "must-not-escape")
    monkeypatch.setenv("PYTHONHOME", "must-not-escape")
    monkeypatch.setenv("VIRTUAL_ENV", "must-not-select-authority")
    target = prepare_terminal_execution(_terminal_request(tmp_path))
    environment = target.environment_mapping()

    assert target.environment_policy_id == TERMINAL_ENVIRONMENT_POLICY_ID
    assert type(target.environment) is tuple
    assert "OPENAI_API_KEY" not in environment
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert set(environment) <= {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "PATH",
        "PATHEXT",
    }

    environment["PATH"] = "mutated"
    assert target.environment_mapping().get("PATH") != "mutated"

    with pytest.raises(TypeError):
        prepare_terminal_execution(
            _terminal_request(tmp_path),
            environment={"PATH": "caller override"},
        )


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_environment_identity_and_target_equality_are_deterministic(tmp_path):
    request = _terminal_request(tmp_path, argv=["--version"])
    first = prepare_terminal_execution(request)
    second = prepare_terminal_execution(request)

    assert first == second
    assert first.environment_identity == second.environment_identity
    assert first.environment_identity.startswith("sha256:")

    changed = prepare_terminal_execution(
        _terminal_request(tmp_path, argv=["-I", "--version"])
    )
    assert changed != first

    with pytest.raises(FrozenInstanceError):
        first.argv = ()


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_target_has_fixed_noninteractive_windows_contract(tmp_path):
    target = prepare_terminal_execution(_terminal_request(tmp_path))

    assert target.contract_version == TERMINAL_TARGET_CONTRACT_VERSION
    assert target.platform_contract == TERMINAL_PLATFORM_CONTRACT
    assert target.stdin_mode == TERMINAL_STDIN_MODE == "devnull"
    assert target.timeout_seconds == TERMINAL_TIMEOUT_SECONDS
    assert 0 < target.timeout_seconds <= 60
    assert target.shell is False


@pytest.mark.skipif(os.name != "nt", reason="D1F-A target is Windows-native")
def test_shell_executables_are_prohibited(monkeypatch, tmp_path):
    system_root = Path(os.environ["SystemRoot"])
    cmd_directory = system_root / "System32"
    monkeypatch.setenv("PATH", str(cmd_directory))
    monkeypatch.setenv("PATHEXT", ".EXE")

    with pytest.raises(TerminalContractError, match="prohibited"):
        prepare_terminal_execution(
            _terminal_request(tmp_path, executable="cmd")
        )
