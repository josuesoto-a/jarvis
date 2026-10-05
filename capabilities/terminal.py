"""Pure Windows terminal execution contracts and target preparation.

D1G-D1a deliberately stops before execution. This module validates a structured
process request and freezes the exact launch identity that a later approval and
runtime may consume. It contains no process-launching API.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from hashlib import sha256
import json
import os
import platform
from pathlib import Path, PureWindowsPath
import stat
import struct
import sys
from uuid import UUID

from core.approval import ApprovalSubject, PendingApprovalTarget
from core.contracts import ArgumentSource, ExecutionPlan, PermissionMode, RiskLevel


TERMINAL_TARGET_CONTRACT_VERSION = "terminal-execution-target/v2"
TERMINAL_ENVIRONMENT_POLICY_ID = "windows-minimal/v2"
TERMINAL_TIMEOUT_SECONDS = 30.0
TERMINAL_STDIN_MODE = "devnull"
TERMINAL_PLATFORM_CONTRACT = "windows10-11-x64-console/v1"
TERMINAL_SERIALIZATION_POLICY = "windows-crt-argv/v1"
TERMINAL_OUTPUT_POLICY = "windows-prefix-64k/v1"
TERMINAL_DECODING_POLICY = "utf8-replace/v1"
TERMINAL_COMMAND_LINE_UTF16_LIMIT = 30_000  # Includes terminating NUL.
TERMINAL_ENVIRONMENT_UTF16_LIMIT = 32_768  # Includes final double NUL.
TERMINAL_ARGV_LIMIT = 1_024
TERMINAL_PATH_UTF16_LIMIT = 259  # Ordinary Win32 paths, excluding NUL.
TERMINAL_PE_HEADER_BYTES = 64 * 1024
TERMINAL_EXECUTABLE_BYTES_LIMIT = 256 * 1024 * 1024
TERMINAL_STDOUT_BYTES_LIMIT = 64 * 1024
TERMINAL_STDERR_BYTES_LIMIT = 64 * 1024
TERMINAL_READ_CHUNK_BYTES = 16 * 1024
TERMINAL_CLEANUP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TerminalRuntimePolicy:
    """Immutable declarations, not implemented runtime controls.

    Every field is projected into approval identity. Job process/memory limits
    remain unresolved: a backend cannot activate under this policy until real
    validation selects limits and a new policy/target version binds them.
    """

    policy_id: str = "windows-bounded-runtime-contract/v1"
    executable_policy: str = "canonical-local-drive-native-console-pe/v1"
    supported_machines: tuple[int, ...] = (0x014C, 0x8664)
    identity_verification_policy: str = "held-restrictive-read-sha256/v1"
    serialization_policy: str = TERMINAL_SERIALIZATION_POLICY
    command_line_utf16_limit: int = TERMINAL_COMMAND_LINE_UTF16_LIMIT
    argv_limit: int = TERMINAL_ARGV_LIMIT
    path_utf16_limit: int = TERMINAL_PATH_UTF16_LIMIT
    pe_header_bytes: int = TERMINAL_PE_HEADER_BYTES
    executable_bytes_limit: int = TERMINAL_EXECUTABLE_BYTES_LIMIT
    environment_transport_policy: str = "unicode-sorted-exact/v1"
    environment_utf16_limit: int = TERMINAL_ENVIRONMENT_UTF16_LIMIT
    output_policy: str = TERMINAL_OUTPUT_POLICY
    stdout_bytes_limit: int = TERMINAL_STDOUT_BYTES_LIMIT
    stderr_bytes_limit: int = TERMINAL_STDERR_BYTES_LIMIT
    read_chunk_bytes: int = TERMINAL_READ_CHUNK_BYTES
    decoding_policy: str = TERMINAL_DECODING_POLICY
    cleanup_seconds: float = TERMINAL_CLEANUP_SECONDS
    containment_policy: str = "creation-time-job-kill-on-close-no-breakaway/v1"
    descendant_exit_policy: str = "terminate-and-fail-if-root-exits-first/v1"
    inherited_handles: tuple[str, ...] = ("stdin-nul", "stdout-writer", "stderr-writer")
    creation_flags: tuple[str, ...] = (
        "CREATE_SUSPENDED", "EXTENDED_STARTUPINFO_PRESENT",
        "CREATE_UNICODE_ENVIRONMENT", "CREATE_NO_WINDOW",
    )
    active_invocations_limit: int = 1
    parent_io_policy: str = "fixed-owned-handles-two-bounded-pending-reads/v1"
    job_resource_policy: str = "unresolved-no-activation/v1"
    result_policy: str = "structured-failure-created-evidence/v1"
    deadline_policy: str = "runtime-entry-preflight-included-single-cleanup-budget/v1"

    def validate(self) -> None:
        for descriptor in fields(self):
            actual = getattr(self, descriptor.name)
            expected = getattr(TERMINAL_RUNTIME_POLICY, descriptor.name)
            if type(actual) is not type(expected) or actual != expected:
                raise TerminalContractError("unsupported terminal runtime policy")
            if type(actual) is tuple and any(type(item) is not type(reference)
                                           for item, reference in zip(actual, expected)):
                raise TerminalContractError("unsupported terminal runtime policy")

    def approval_arguments(self) -> dict[str, object]:
        return {descriptor.name: (list(value) if type(value) is tuple else value)
                for descriptor in fields(self)
                for value in (getattr(self, descriptor.name),)}


TERMINAL_RUNTIME_POLICY = TerminalRuntimePolicy()

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
    _utf16_units(value)
    return value


def _utf16_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise TerminalContractError("unpaired Unicode surrogates are unsupported") from error


def _validate_local_path_form(value: str, *, canonical: bool = False) -> None:
    """Ordinary drive-absolute paths only; filesystem locality is checked later."""
    _require_plain_string(value, name="Windows path")
    path = PureWindowsPath(value)
    if (not path.is_absolute() or len(path.drive) != 2
            or not path.drive[0].isascii() or not path.drive[0].isalpha()
            or path.drive[1] != ":" or value.startswith(("\\\\", "//"))):
        raise TerminalContractError("path must be an absolute local Windows drive path")
    if _utf16_units(value) > TERMINAL_PATH_UTF16_LIMIT:
        raise TerminalContractError("Windows path exceeds the supported path limit")
    for part in path.parts[1:]:
        if part in {".", ".."} and not canonical:
            continue
        if (part in {".", ".."} or part.endswith((".", " "))
                or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
                or PureWindowsPath(part).is_reserved()):
            raise TerminalContractError("unsupported Windows path component or namespace")


def serialize_windows_command_line(executable_resolved: str,
                                   argv: list[str] | tuple[str, ...]) -> str:
    """Pure windows-crt-argv/v1; argv excludes the executable/argv[0].

    Guarantees supplied command-line text, not arbitrary programs' parsing.
    Space/tab quoting and backslash/quote rules are pinned independently of
    Python's implementation. No command string is accepted as argv.
    """
    _validate_executable_boundary(executable_resolved, resolved=True)
    if type(argv) not in {list, tuple}:
        raise TypeError("argv must be a list or tuple of plain strings")
    if len(argv) > TERMINAL_ARGV_LIMIT:
        raise TerminalContractError("argv exceeds the argument count limit")
    serialized = []
    units = 1  # Final NUL is counted although it is not returned.
    for index, argument in enumerate((executable_resolved, *argv)):
        _require_plain_string(argument, name=f"launch argument {index}", allow_empty=True)
        if _utf16_units(argument) > TERMINAL_COMMAND_LINE_UTF16_LIMIT:
            raise TerminalContractError("command line exceeds the UTF-16 limit")
        quoted = not argument or " " in argument or "\t" in argument
        pieces = ['"'] if quoted else []
        slashes = 0
        for character in argument:
            if character == "\\":
                slashes += 1
                continue
            if character == '"':
                pieces.append("\\" * (2 * slashes + 1))
            else:
                pieces.append("\\" * slashes)
            pieces.append(character)
            slashes = 0
        pieces.append("\\" * (slashes * 2 if quoted else slashes))
        if quoted:
            pieces.append('"')
        token = "".join(pieces)
        units += _utf16_units(token) + (1 if index else 0)
        if units > TERMINAL_COMMAND_LINE_UTF16_LIMIT:
            raise TerminalContractError("command line exceeds the UTF-16 limit")
        serialized.append(token)
    return " ".join(serialized)


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
        _validate_local_path_form(cwd_value)
        if len(argv_value) > TERMINAL_ARGV_LIMIT:
            raise TerminalContractError("argv exceeds the argument count limit")

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
    runtime_policy: TerminalRuntimePolicy = field(default=TERMINAL_RUNTIME_POLICY, init=False)

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
        """Validate a stored target without filesystem/ambient discovery."""
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
        if type(self.runtime_policy) is not TerminalRuntimePolicy:
            raise TerminalContractError("unsupported terminal runtime policy type")
        self.runtime_policy.validate()
        for name in (
            "executable_requested",
            "executable_resolved",
            "executable_identity",
            "cwd",
            "environment_identity",
        ):
            _require_plain_string(getattr(self, name), name=name)

        _validate_local_path_form(self.executable_resolved, canonical=True)
        _validate_local_path_form(self.cwd, canonical=True)

        if type(self.argv) is not tuple:
            raise TypeError("stored argv must be immutable")
        if len(self.argv) > TERMINAL_ARGV_LIMIT:
            raise TerminalContractError("argv exceeds the argument count limit")
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
        _validate_environment_mapping(dict(frozen_environment))
        build_windows_environment_block(dict(frozen_environment))
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
        serialize_windows_command_line(self.executable_resolved, self.argv)

    def environment_mapping(self) -> dict[str, str]:
        """Return a detached mapping for a future execution boundary."""

        return dict(self.environment)


def _freeze_windows_environment(
    source: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Select only the explicit v1 Windows environment policy."""

    casefolded: dict[str, str] = {}
    for key, value in source.items():
        if type(key) is not str or key.upper() not in _ENVIRONMENT_KEYS:
            continue
        canonical_key = key.upper()
        if canonical_key in casefolded:
            raise TerminalContractError("case-colliding allowlisted environment keys")
        _require_plain_string(value, name=f"environment value {canonical_key}", allow_empty=True)
        casefolded[canonical_key] = value

    _validate_environment_mapping(casefolded)
    build_windows_environment_block(casefolded)

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


def _validate_environment_mapping(environment: Mapping[str, str]) -> None:
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    seen = set()
    for key, value in environment.items():
        _require_plain_string(key, name="environment key")
        canonical_key = key.upper()
        if canonical_key not in _ENVIRONMENT_KEYS:
            raise TerminalContractError("environment key is outside the allowlist")
        if canonical_key in seen:
            raise TerminalContractError("case-colliding allowlisted environment keys")
        seen.add(canonical_key)
        if key != canonical_key:
            raise TerminalContractError("frozen environment keys must be canonical uppercase")
        _require_plain_string(value, name=f"environment value {key}", allow_empty=True)
    system_root = environment.get("SYSTEMROOT")
    if system_root is None:
        raise TerminalContractError("SYSTEMROOT is required before approval")
    _validate_local_path_form(system_root)


def build_windows_environment_block(environment: Mapping[str, str]) -> str:
    """Pure, exact Unicode transport; returned text includes final double NUL.

    Identity ordering stays the fixed allowlist order. Transport is sorted by
    canonical ASCII keys without introducing values, drive state or inheritance.
    """
    _validate_environment_mapping(environment)
    entries = []
    units = 1
    for key in sorted(environment):
        entry = key + "=" + environment[key] + "\x00"
        units += _utf16_units(entry)
        if units > TERMINAL_ENVIRONMENT_UTF16_LIMIT:
            raise TerminalContractError("environment block exceeds the UTF-16 limit")
        entries.append(entry)
    return "".join(entries) + "\x00"


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
    _validate_local_path_form(cwd)

    try:
        resolved = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TerminalContractError("cwd does not exist") from error

    result = str(resolved)
    _validate_local_path_form(result, canonical=True)
    if not resolved.is_dir():
        raise TerminalContractError("cwd must identify a directory")

    return result


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
        for directory in search_path.split(os.pathsep):
            if directory:
                _validate_local_path_form(directory)
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
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError) as error:
                raise TerminalContractError("executable resolution failed") from error
            _validate_executable_boundary(str(resolved), resolved=True)
            if not resolved.is_file():
                raise TerminalContractError("executable must identify a regular file")
            return resolved

    raise TerminalContractError("executable could not be resolved")


def inspect_windows_console_pe(header: bytes, *, file_size: int) -> int:
    """Bounded structural check of a prefix, not a loader or trust verifier.

    Accept only x86 PE32 / x64 PE32+ native console images, with standard
    optional headers and a bounded section table. No executable code is run.
    The caller owns regular-file checks and reading at most pe_header_bytes.
    """
    if type(header) is not bytes or type(file_size) is not int:
        raise TypeError("PE inspection requires bytes and an integer file size")
    if (len(header) > TERMINAL_PE_HEADER_BYTES or file_size < len(header)
            or file_size > TERMINAL_EXECUTABLE_BYTES_LIMIT or len(header) < 64
            or header[:2] != b"MZ"):
        raise TerminalContractError("unsupported or malformed PE header")
    offset = struct.unpack_from("<I", header, 0x3C)[0]
    if offset < 64 or offset + 24 > len(header) or header[offset:offset + 4] != b"PE\x00\x00":
        raise TerminalContractError("unsupported or malformed PE signature")
    machine, sections = struct.unpack_from("<HH", header, offset + 4)
    optional_size, characteristics = struct.unpack_from("<HH", header, offset + 20)
    if (machine not in TERMINAL_RUNTIME_POLICY.supported_machines
            or not 1 <= sections <= 96 or not characteristics & 0x0002
            or characteristics & 0x2000):
        raise TerminalContractError("unsupported PE machine, DLL or image characteristics")
    optional = offset + 24
    table = optional + optional_size
    table_end = table + sections * 40
    if table_end > len(header):
        raise TerminalContractError("truncated or oversized PE header table")
    expected_magic, expected_size, directories = (
        (0x10B, 224, 96) if machine == 0x014C else (0x20B, 240, 112)
    )
    if (optional_size != expected_size
            or struct.unpack_from("<H", header, optional)[0] != expected_magic):
        raise TerminalContractError("unsupported PE optional header shape")
    if struct.unpack_from("<H", header, optional + 68)[0] != 3:
        raise TerminalContractError("only native console PE subsystem is supported")
    entrypoint = struct.unpack_from("<I", header, optional + 16)[0]
    image_size, headers_size = struct.unpack_from("<II", header, optional + 56)
    if (not 0 < entrypoint < image_size or not table_end <= headers_size <= file_size
            or struct.unpack_from("<I", header, optional + directories - 4)[0] != 16
            or any(struct.unpack_from("<II", header, optional + directories + 14 * 8))):
        raise TerminalContractError("unsupported native PE image/header/managed shape")
    entrypoint_executable = False
    for index in range(sections):
        section = table + index * 40
        virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", header, section + 8)
        flags = struct.unpack_from("<I", header, section + 36)[0]
        extent = max(virtual_size, raw_size)
        if (address >= image_size or extent > image_size - address
                or (raw_size and (raw_offset < headers_size or raw_offset + raw_size > file_size))):
            raise TerminalContractError("malformed PE section bounds")
        if address <= entrypoint < address + extent and flags & 0x20000000:
            entrypoint_executable = True
    if not entrypoint_executable:
        raise TerminalContractError("PE entrypoint must be in an executable section")
    return machine


def _digest_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as executable_file:
            initial = os.fstat(executable_file.fileno())
            if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= TERMINAL_EXECUTABLE_BYTES_LIMIT:
                raise TerminalContractError("executable must be a bounded regular file")
            header = executable_file.read(TERMINAL_PE_HEADER_BYTES)
            inspect_windows_console_pe(header, file_size=initial.st_size)
            digest.update(header)
            total = len(header)
            while chunk := executable_file.read(1024 * 1024):
                total += len(chunk)
                if total > TERMINAL_EXECUTABLE_BYTES_LIMIT:
                    raise TerminalContractError("executable exceeds the byte limit")
                digest.update(chunk)
            final = os.fstat(executable_file.fileno())
            if (total != initial.st_size or final.st_size != initial.st_size
                    or final.st_mtime_ns != initial.st_mtime_ns):
                raise TerminalContractError("executable changed during preparation")
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
    _validate_windows_host()

    environment = _freeze_windows_environment(os.environ)
    environment_mapping = dict(environment)
    _canonical_windows_directory(environment_mapping["SYSTEMROOT"])
    cwd = _canonical_windows_directory(request.cwd)
    executable = _resolve_windows_executable(
        request.executable,
        environment_mapping,
    )
    # Derive/size-check transport before reading the complete executable.
    serialize_windows_command_line(str(executable), request.argv)
    return TerminalExecutionTarget(
        executable_requested=request.executable,
        executable_resolved=str(executable),
        executable_identity=_digest_file(executable),
        argv=request.argv,
        cwd=cwd,
        environment=environment,
        environment_identity=_environment_identity(environment),
    )


def _validate_windows_host() -> None:
    if os.name != "nt":
        raise TerminalContractError("terminal execution target supports Windows only")
    version = sys.getwindowsversion()
    if (version.major != 10 or version.build < 10240 or version.product_type != 1
            or platform.machine().upper() not in {"AMD64", "X86_64"}
            or sys.maxsize <= 2**32):
        raise TerminalContractError("terminal requires Windows 10/11 x64 workstation and 64-bit Python")


def _validate_executable_boundary(value: str, *, resolved: bool) -> None:
    """Deny known shell entrypoints, including the final resolved filename.

    This denylist is not a semantic sandbox for arbitrary native executables.
    """
    path = PureWindowsPath(value)
    _require_plain_string(value, name="executable")
    if resolved or path.is_absolute():
        _validate_local_path_form(value, canonical=resolved)
    elif (path.drive or "\\" in value or "/" in value
          or value.endswith((".", " ")) or path.is_reserved()
          or any(character in '<>:"|?*' or ord(character) < 32 for character in value)):
        raise TerminalContractError("executable must be a bare name or absolute local Windows path")
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
        "runtime_policy": target.runtime_policy.approval_arguments(),
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
