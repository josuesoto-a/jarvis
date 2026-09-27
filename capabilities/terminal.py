"""Pure Windows terminal execution contracts and target preparation.

D1F-A deliberately stops before execution. This module validates a structured
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

        if type(self.argv) not in {list, tuple}:
            raise TypeError("argv must be a list or tuple of plain strings")
        argv = tuple(self.argv)
        for index, item in enumerate(argv):
            _require_plain_string(
                item,
                name=f"argv[{index}]",
                allow_empty=True,
            )
        object.__setattr__(self, "argv", argv)

        if type(self.environment) not in {list, tuple}:
            raise TypeError("environment must be an ordered key/value sequence")
        environment: list[tuple[str, str]] = []
        for item in self.environment:
            if type(item) not in {list, tuple} or len(item) != 2:
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
        ):
            raise TerminalContractError(
                "executable identity must be a SHA-256 identity"
            )
        object.__setattr__(self, "environment", frozen_environment)

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
            "environment": environment,
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
            if resolved.name.lower() in _PROHIBITED_EXECUTABLE_NAMES:
                raise TerminalContractError(
                    "shell and shell-like executables are prohibited in v1"
                )
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
