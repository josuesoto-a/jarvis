"""Pure Windows terminal execution contracts and target preparation.

D1G-A deliberately stops before execution. This module validates a structured
process request and freezes the exact launch identity that a later approval and
runtime may consume. It contains no process-launching API.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
from uuid import UUID

from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import ArgumentSource, ExecutionPlan, PermissionMode, RiskLevel


TERMINAL_TARGET_CONTRACT_VERSION = "terminal-execution-target/v1"
TERMINAL_ENVIRONMENT_POLICY_ID = "windows-minimal/v1"
TERMINAL_TIMEOUT_SECONDS = 30.0
TERMINAL_STDIN_MODE = "devnull"
TERMINAL_PLATFORM_CONTRACT = "windows-native/v1"

_ENVIRONMENT_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "PATH",
    "PATHEXT",
)

_NATIVE_EXECUTABLE_EXTENSIONS = (
    ".exe",
    ".com",
)

_PROHIBITED_EXECUTABLE_NAMES = frozenset(
    {
        "bash.exe",
        "cmd.exe",
        "git-bash.exe",
        "powershell.exe",
        "pwsh.exe",
        "sh.exe",
        "wsl.exe",
    }
)


class TerminalContractError(ValueError):
    """A terminal request cannot become an exact v1 launch target."""


def _require_plain_string(
    value: object,
    *,
    name: str,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a plain string")
    if "\x00" in value:
        raise TerminalContractError(f"{name} cannot contain NUL")
    if not allow_empty and not value.strip():
        raise TerminalContractError(f"{name} cannot be empty")
    return value


@dataclass(frozen=True, slots=True, init=False)
class TerminalExecutionRequest:
    """One structured, non-shell terminal process request."""

    executable: str
    argv: tuple[str, ...]
    cwd: str

    def __init__(
        self,
        *,
        executable: str,
        argv: list[str] | tuple[str, ...],
        cwd: str,
    ) -> None:
        executable_value = _require_plain_string(
            executable,
            name="executable",
        )
        cwd_value = _require_plain_string(
            cwd,
            name="cwd",
        )

        if type(argv) not in {list, tuple}:
            raise TypeError("argv must be a list or tuple of plain strings")

        argv_value = tuple(argv)
        for index, item in enumerate(argv_value):
            _require_plain_string(
                item,
                name=f"argv[{index}]",
                allow_empty=True,
            )

        _validate_executable_boundary(executable_value, resolved=False)
        if not PureWindowsPath(cwd_value).is_absolute():
            raise TerminalContractError("cwd must be an absolute native Windows path")

        object.__setattr__(self, "executable", executable_value)
        object.__setattr__(self, "argv", argv_value)
        object.__setattr__(self, "cwd", cwd_value)


@dataclass(frozen=True, slots=True)
class TerminalExecutionTarget:
    """Immutable prepared identity for one future native process launch."""

    executable_requested: str
    executable_resolved: str
    executable_identity: str
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]
    environment_identity: str

    contract_version: str = field(
        default=TERMINAL_TARGET_CONTRACT_VERSION,
        init=False,
    )
    environment_policy_id: str = field(
        default=TERMINAL_ENVIRONMENT_POLICY_ID,
        init=False,
    )
    timeout_seconds: float = field(
        default=TERMINAL_TIMEOUT_SECONDS,
        init=False,
    )
    stdin_mode: str = field(
        default=TERMINAL_STDIN_MODE,
        init=False,
    )
    platform_contract: str = field(
        default=TERMINAL_PLATFORM_CONTRACT,
        init=False,
    )
    shell: bool = field(
        default=False,
        init=False,
    )

    def __post_init__(self) -> None:
        # Detach caller containers once; subsequent validation is read-only.
        if type(self.argv) not in {list, tuple}:
            raise TypeError("argv must be a list or tuple of plain strings")
        if type(self.environment) not in {list, tuple}:
            raise TypeError("environment must be an ordered key/value sequence")
        environment = []
        for item in self.environment:
            if type(item) not in {list, tuple} or len(item) != 2:
                raise TypeError("environment entries must be key/value pairs")
            environment.append(tuple(item))
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", tuple(environment))
        self.validate()

    def validate(self) -> None:
        """Validate a stored v1 target without resolving or hashing it again."""
        if (
            self.contract_version != TERMINAL_TARGET_CONTRACT_VERSION
            or self.platform_contract != TERMINAL_PLATFORM_CONTRACT
            or self.environment_policy_id != TERMINAL_ENVIRONMENT_POLICY_ID
            or type(self.timeout_seconds) is not float
            or self.timeout_seconds != TERMINAL_TIMEOUT_SECONDS
            or self.stdin_mode != TERMINAL_STDIN_MODE
            or self.shell is not False
        ):
            raise TerminalContractError("unsupported terminal v1 execution policy")
        for name in (
            "executable_requested",
            "executable_resolved",
            "executable_identity",
            "cwd",
            "environment_identity",
        ):
            _require_plain_string(getattr(self, name), name=name)

        if not PureWindowsPath(self.executable_resolved).is_absolute():
            raise TerminalContractError(
                "executable_resolved must be an absolute Windows path"
            )
        if not PureWindowsPath(self.cwd).is_absolute():
            raise TerminalContractError(
                "cwd must be an absolute native Windows path"
            )

        if type(self.argv) is not tuple:
            raise TypeError("stored argv must be immutable")
        argv = self.argv
        for index, item in enumerate(argv):
            _require_plain_string(
                item,
                name=f"argv[{index}]",
                allow_empty=True,
            )

        if type(self.environment) is not tuple:
            raise TypeError("stored environment must be immutable")
        environment: list[tuple[str, str]] = []
        for item in self.environment:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError("environment entries must be key/value pairs")
            key = _require_plain_string(item[0], name="environment key")
            value = _require_plain_string(
                item[1],
                name=f"environment value {key}",
                allow_empty=True,
            )
            environment.append((key, value))
        frozen_environment = tuple(environment)
        keys = tuple(key for key, _value in frozen_environment)
        expected_keys = tuple(
            key for key in _ENVIRONMENT_KEYS if key in set(keys)
        )
        if keys != expected_keys:
            raise TerminalContractError(
                "environment does not match the ordered v1 allowlist"
            )
        if self.environment_identity != _environment_identity(
            frozen_environment
        ):
            raise TerminalContractError(
                "environment identity does not match frozen environment"
            )
        if (
            not self.executable_identity.startswith("sha256:")
            or len(self.executable_identity) != 71
            or any(character not in "0123456789abcdef"
                   for character in self.executable_identity[7:])
        ):
            raise TerminalContractError(
                "executable identity must be a SHA-256 identity"
            )
        _validate_executable_boundary(self.executable_requested, resolved=False)
        _validate_executable_boundary(self.executable_resolved, resolved=True)

    def environment_mapping(self) -> dict[str, str]:
        """Return a detached mapping for a future execution boundary."""

        return dict(self.environment)


def _freeze_windows_environment(
    source: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Select only the explicit v1 Windows environment policy."""

    casefolded: dict[str, str] = {}
    for key, value in source.items():
        if type(key) is not str or type(value) is not str:
            continue
        casefolded[key.upper()] = value

    frozen: list[tuple[str, str]] = []
    for key in _ENVIRONMENT_KEYS:
        value = casefolded.get(key)
        if value is None:
            continue
        if "\x00" in value:
            raise TerminalContractError(
                f"environment value {key} cannot contain NUL"
            )
        frozen.append((key, value))

    return tuple(frozen)


def _environment_identity(
    environment: tuple[tuple[str, str], ...],
) -> str:
    encoded = json.dumps(
        {
            "policy": TERMINAL_ENVIRONMENT_POLICY_ID,
            "environment": [[key, value] for key, value in environment],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _canonical_windows_directory(cwd: str) -> str:
    windows_path = PureWindowsPath(cwd)
    if not windows_path.is_absolute():
        raise TerminalContractError(
            "cwd must be an absolute native Windows path"
        )

    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TerminalContractError("cwd does not exist") from error

    if not resolved.is_dir():
        raise TerminalContractError("cwd must identify a directory")

    return str(resolved)


def _candidate_extensions(
    requested: str,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    suffix = PureWindowsPath(requested).suffix.lower()
    if suffix:
        if suffix not in _NATIVE_EXECUTABLE_EXTENSIONS:
            raise TerminalContractError(
                "executable must be a native .exe or .com file"
            )
        return ("",)

    configured = environment.get("PATHEXT", "")
    extensions = tuple(
        extension.lower()
        for extension in configured.split(";")
        if extension.lower() in _NATIVE_EXECUTABLE_EXTENSIONS
    )
    return extensions or _NATIVE_EXECUTABLE_EXTENSIONS


def _resolve_windows_executable(
    requested: str,
    environment: Mapping[str, str],
) -> Path:
    windows_path = PureWindowsPath(requested)
    extensions = _candidate_extensions(requested, environment)

    base_candidates: tuple[Path, ...]
    if windows_path.is_absolute():
        base_candidates = (Path(requested),)
    else:
        if windows_path.drive or "\\" in requested or "/" in requested:
            raise TerminalContractError(
                "executable must be a bare name or absolute Windows path"
            )

        search_path = environment.get("PATH", "")
        base_candidates = tuple(
            Path(directory) / requested
            for directory in search_path.split(os.pathsep)
            if directory
        )

    for base in base_candidates:
        for extension in extensions:
            candidate = Path(str(base) + extension)
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_file():
                continue
            _validate_executable_boundary(str(resolved), resolved=True)
            return resolved

    raise TerminalContractError("executable could not be resolved")


def _digest_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as executable_file:
            while chunk := executable_file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise TerminalContractError(
            "executable could not be read for identity"
        ) from error
    return "sha256:" + digest.hexdigest()


def prepare_terminal_execution(
    request: TerminalExecutionRequest,
) -> TerminalExecutionTarget:
    """Purely prepare one exact v1 target without executing anything."""

    if not isinstance(request, TerminalExecutionRequest):
        raise TypeError("request must be a TerminalExecutionRequest")
    if os.name != "nt":
        raise TerminalContractError(
            "terminal execution target v1 supports Windows only"
        )

    environment = _freeze_windows_environment(os.environ)
    environment_mapping = dict(environment)
    cwd = _canonical_windows_directory(request.cwd)
    executable = _resolve_windows_executable(
        request.executable,
        environment_mapping,
    )

    return TerminalExecutionTarget(
        executable_requested=request.executable,
        executable_resolved=str(executable),
        executable_identity=_digest_file(executable),
        argv=request.argv,
        cwd=cwd,
        environment=environment,
        environment_identity=_environment_identity(environment),
    )


def _validate_executable_boundary(value: str, *, resolved: bool) -> None:
    """Deny known shell entrypoints, including the final resolved filename.

    This denylist is not a semantic sandbox for arbitrary native executables.
    """
    path = PureWindowsPath(value)
    name = path.name.lower()
    if name in _PROHIBITED_EXECUTABLE_NAMES or name + ".exe" in _PROHIBITED_EXECUTABLE_NAMES:
        raise TerminalContractError("shell and shell-like executables are prohibited in v1")
    if path.suffix.lower() not in (("",) + _NATIVE_EXECUTABLE_EXTENSIONS if not resolved
                                   else _NATIVE_EXECUTABLE_EXTENSIONS):
        raise TerminalContractError("executable must be a native .exe or .com file")


def terminal_request_from_plan(plan: ExecutionPlan) -> TerminalExecutionRequest:
    """Read only the single-step, literal terminal v1 shape; never prepare it."""
    if len(plan.steps) != 1 or plan.steps[0].capability != "terminal":
        raise TerminalContractError("terminal v1 requires exactly one terminal step")
    step = plan.steps[0]
    if set(step.arguments) != {"executable", "argv", "cwd"}:
        raise TerminalContractError("terminal v1 requires exactly executable, argv and cwd")
    if any(argument.source is not ArgumentSource.LITERAL
           for argument in step.arguments.values()):
        raise TerminalContractError("terminal v1 requires literal-only arguments")
    return TerminalExecutionRequest(
        executable=step.arguments["executable"].value,
        argv=step.arguments["argv"].value,
        cwd=step.arguments["cwd"].value,
    )


def terminal_approval_arguments(target: TerminalExecutionTarget) -> dict[str, object]:
    """The ONE complete JSON-compatible projection of process-creation inputs.

    Projection includes policy fields even if a future version changes them.
    Validation is separate so identity tests can detect every material change.
    """
    if type(target) is not TerminalExecutionTarget:
        raise TypeError("terminal approval requires a TerminalExecutionTarget")
    return {
        "contract_version": target.contract_version,
        "platform_contract": target.platform_contract,
        "executable_requested": target.executable_requested,
        "executable_resolved": target.executable_resolved,
        "executable_identity": target.executable_identity,
        "argv": list(target.argv),
        "cwd": target.cwd,
        "environment_policy_id": target.environment_policy_id,
        "environment": [[key, value] for key, value in target.environment],
        "environment_identity": target.environment_identity,
        "timeout_seconds": target.timeout_seconds,
        "stdin_mode": target.stdin_mode,
        "shell": target.shell,
    }


def build_terminal_approval_target(
    target: TerminalExecutionTarget,
    *,
    request_id: UUID,
    step_number: int,
    risk: RiskLevel,
    effective_permission: PermissionMode,
) -> PendingApprovalTarget:
    """Bind an already prepared target; the Executor owns when preparation runs."""
    subject = ApprovalSubject(
        request_id=request_id, step_number=step_number, capability="terminal",
        risk=risk, arguments=terminal_approval_arguments(target),
    )
    pending = PendingApprovalTarget(
        subject=subject, effective_permission=effective_permission, prepared_target=target,
    )
    validate_terminal_approval_target(
        pending, request_id=request_id, step_number=step_number, risk=risk,
    )
    return pending


def validate_terminal_approval_target(
    pending: PendingApprovalTarget,
    *,
    request_id: UUID,
    step_number: int,
    risk: RiskLevel,
) -> None:
    """Fail closed on metadata, v1 policy or subject/target contradictions."""
    if type(pending) is not PendingApprovalTarget:
        raise TerminalContractError("terminal continuation requires a pending approval target")
    subject = pending.subject
    if (
        subject.request_id != request_id
        or subject.step_number != step_number
        or subject.capability != "terminal"
        or subject.risk != risk
        or pending.effective_permission is not PermissionMode.CONFIRM_BEFORE_EXECUTION
        or type(pending.prepared_target) is not TerminalExecutionTarget
    ):
        raise TerminalContractError("terminal approval metadata does not match its step")
    target = pending.prepared_target
    target.validate()
    if subject.arguments != terminal_approval_arguments(target):
        raise TerminalContractError("terminal subject does not represent its prepared target")


def validate_terminal_target_for_request(
    target: TerminalExecutionTarget, request: TerminalExecutionRequest,
) -> None:
    """Check the stored target against the original plan without re-preparation.

    Directory resolution may fail closed if a link changed since preparation.
    Executable/environment are never selected again. Executable revalidation
    belongs to the future launch boundary documented in terminal_runtime_policy.md.
    """
    if (target.executable_requested != request.executable
            or target.argv != request.argv
            or target.cwd != _canonical_windows_directory(request.cwd)):
        raise TerminalContractError("terminal plan does not represent its prepared target")
