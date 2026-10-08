"""D1G-D1b: lifecycle evidence and explicitly injected terminal orchestration.

The evidence foundation records supplied facts; the injected runtime orchestrates
semantic backend primitives without importing native bindings. Admission is
not authorization. All owners that need process-wide admission MUST share one
LaunchAdmissionDomain. There is deliberately no default/global domain here.

Snapshots exclude opaque payloads. No destructor performs cleanup. A domain
retains its active invocation and any quarantine, even if callers discard them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from threading import Condition, Thread, current_thread, main_thread
from time import monotonic
from typing import Callable, Protocol

from hashlib import sha256

from capabilities.terminal import (
    TERMINAL_CLEANUP_SECONDS, TERMINAL_TIMEOUT_SECONDS,
    TerminalExecutionTarget, TerminalContractError, build_windows_environment_block,
    serialize_windows_command_line, inspect_windows_console_pe,
    _validate_local_path_form, TERMINAL_EXECUTABLE_BYTES_LIMIT,
    TERMINAL_PE_HEADER_BYTES, TERMINAL_READ_CHUNK_BYTES, TERMINAL_STDOUT_BYTES_LIMIT,
)


# Constructor guard only; this is not an admission domain or authorization.
_ISSUED = object()


class TerminalLifecycleError(RuntimeError):
    """An ownership, admission or evidence transition is inconsistent."""


class LaunchAdmissionError(TerminalLifecycleError):
    """The shared domain cannot admit another invocation."""


class LaunchAdmissionState(str, Enum):
    OPEN = "open"
    RUNNING = "running"
    POISONED = "poisoned"
    CLOSED = "closed"


class InvocationPhase(str, Enum):
    PREFLIGHT = "preflight"
    CREATE_COMMIT = "create_commit"
    CREATED_SUSPENDED = "created_suspended"
    RUNNING = "running"
    CLEANING = "cleaning"
    FINISHED = "finished"
    QUARANTINED = "quarantined"


class CreationClassification(str, Enum):
    PRE_CREATE = "P0"
    CREATE_CALL_FAILED = "C1"
    PROCESS_CREATED = "P1"
    CREATE_OUTCOME_UNCERTAIN = "CU"


class OwnershipState(str, Enum):
    RESERVED = "reserved"
    OWNED = "owned"
    RELEASING = "releasing"
    RELEASE_UNCERTAIN = "release_uncertain"
    RELEASED = "released"
    QUARANTINED = "quarantined"


class CancellationReason(str, Enum):
    CANCEL_ACTIVE = "cancel_active"
    SHUTDOWN = "shutdown"


class CleanupReason(str, Enum):
    PREFLIGHT_FAILURE = "preflight_failure"
    CREATE_CALL_FAILED = "create_call_failed"
    POST_CREATE_FAILURE = "post_create_failure"
    CREATION_UNCERTAIN = "creation_uncertain"
    EXECUTION_TIMEOUT = "execution_timeout"
    CANCELLED = "cancelled"
    ROOT_EXIT = "root_exit"


class QuarantineReason(str, Enum):
    DEADLINE_EXPIRED = "deadline_expired"
    RELEASE_UNCERTAIN = "release_uncertain"
    CREATION_UNCERTAIN = "creation_uncertain"
    INCOMPLETE_CLEANUP = "incomplete_cleanup"


class CleanupIssue(str, Enum):
    FAILURE = "failure"
    RELEASE_UNCERTAIN = "release_uncertain"
    QUARANTINE = "quarantine"


class RuntimeDisposition(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Domain-scoped generation and kind; never a native resource value."""

    domain: object
    generation: int
    kind: str


@dataclass(frozen=True, slots=True)
class CreationSnapshot:
    """Derived creation evidence, not a claim of native-call atomicity.

    process_created means successful creation has been confirmed. False in CU
    does not exclude process existence; result_pending distinguishes CU from C1.
    """

    call_count: int
    classification: CreationClassification
    process_created: bool
    result_pending: bool
    process_attached: bool
    thread_attached: bool

    def __post_init__(self) -> None:
        if type(self.call_count) is not int or self.call_count not in {0, 1}:
            raise ValueError("creation has at most one call")
        if any(type(value) is not bool for value in (
            self.process_created, self.result_pending, self.process_attached, self.thread_attached,
        )):
            raise TypeError("creation evidence flags must be Boolean")
        expected = (CreationClassification.PROCESS_CREATED if self.process_created else
                    CreationClassification.CREATE_OUTCOME_UNCERTAIN if self.result_pending else
                    CreationClassification.CREATE_CALL_FAILED if self.call_count else
                    CreationClassification.PRE_CREATE)
        if self.classification is not expected:
            raise ValueError("classification contradicts creation evidence")
        if ((self.process_created or self.result_pending) and self.call_count != 1
                or self.process_created and self.result_pending
                or (self.process_attached or self.thread_attached) and not self.process_created):
            raise ValueError("creation evidence is inconsistent")

    @property
    def adoption_complete(self) -> bool:
        return self.process_created and self.process_attached and self.thread_attached


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    name: str
    state: OwnershipState
    borrowers: int
    pending_operations: int
    identity: ResourceIdentity
    release_attempted: bool


@dataclass(frozen=True, slots=True)
class CleanupEvidence:
    """Completion is derived, never an independently supplied Boolean."""

    phase: InvocationPhase
    creation: CreationSnapshot
    resources: tuple[ResourceSnapshot, ...]
    pending_operations: int
    process_exited: bool
    containment_empty: bool
    quarantine_transferred: bool
    job_empty: bool = False

    def __post_init__(self) -> None:
        if type(self.job_empty) is not bool:
            raise TypeError("Job-empty evidence must be Boolean")
        if self.job_empty and not self.creation.call_count:
            raise ValueError("Job-empty evidence requires a creation attempt")
        if not self.creation.process_created and (self.process_exited or self.containment_empty):
            raise ValueError("exit/containment evidence requires successful creation")
        if self.containment_empty and not self.process_exited:
            raise ValueError("containment evidence requires root exit")

    @property
    def cleanup_completed(self) -> bool:
        return (
            self.phase is InvocationPhase.FINISHED
            and not self.quarantine_transferred
            and not self.creation.result_pending
            and not self.pending_operations
            and all(item.state in {OwnershipState.RESERVED, OwnershipState.RELEASED}
                    and not item.borrowers and not item.pending_operations
                    for item in self.resources)
            and (not self.creation.process_created or
                 (self.creation.adoption_complete and self.process_exited and self.containment_empty))
        )


@dataclass(frozen=True, slots=True)
class TerminalRuntimeOutcome:
    """Frozen lifecycle result with optional immutable execution/capture evidence.

    Pure lifecycle callers retain their original semantics. The runtime ALWAYS
    supplies execution evidence; the handler refuses a lifecycle-only result.
    """

    cleanup: CleanupEvidence
    process_resumed: bool
    terminal_trigger: CleanupReason
    cleanup_issues: tuple[CleanupIssue, ...]
    execution: TerminalExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.execution is not None and type(self.execution) is not TerminalExecutionEvidence:
            raise TypeError("execution evidence must be immutable and typed")
        if self.execution is not None and self.execution.exit_code is not None and not self.cleanup.process_exited:
            raise ValueError("observed exit code requires confirmed root exit")
        if type(self.cleanup) is not CleanupEvidence or type(self.cleanup_issues) is not tuple:
            raise TypeError("outcome requires immutable cleanup evidence/issues")
        if self.cleanup.phase not in {InvocationPhase.FINISHED, InvocationPhase.QUARANTINED}:
            raise ValueError("outcome requires a terminal invocation")
        if not self.cleanup.creation.process_created and (self.process_resumed or self.cleanup.process_exited):
            raise ValueError("resume/exit requires successful creation")
        if self.process_resumed and not self.cleanup.creation.adoption_complete:
            raise ValueError("resume requires complete creation adoption")
        if type(self.terminal_trigger) is not CleanupReason or any(
            type(issue) is not CleanupIssue for issue in self.cleanup_issues
        ):
            raise TypeError("outcome requires typed trigger/issues")

    @property
    def process_created(self) -> bool:
        """Confirmed success only; inspect creation_classification for CU."""
        return self.cleanup.creation.process_created

    @property
    def creation_classification(self) -> CreationClassification:
        """Expose the receipt's classification without an independent status."""
        return self.cleanup.creation.classification

    @property
    def process_exited(self) -> bool:
        return self.cleanup.process_exited

    @property
    def cleanup_completed(self) -> bool:
        return self.cleanup.cleanup_completed

    @property
    def disposition(self) -> RuntimeDisposition:
        if (self.cleanup_completed and self.process_resumed
                and self.terminal_trigger is CleanupReason.ROOT_EXIT and not self.cleanup_issues
                and (self.execution is None or self.execution.acceptable)):
            return RuntimeDisposition.COMPLETED
        return RuntimeDisposition.FAILED


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    phase: InvocationPhase
    creation: CreationSnapshot
    resources: tuple[ResourceSnapshot, ...]
    pending_operations: int
    cancellation_reason: CancellationReason | None
    cleanup_reason: CleanupReason | None
    execution_deadline: float
    cleanup_deadline: float | None
    process_resumed: bool
    process_exited: bool
    containment_empty: bool
    quarantine_reason: QuarantineReason | None
    quarantine_resolved: bool
    job_empty: bool = False


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    state: LaunchAdmissionState
    admission_closed: bool
    active: bool
    quarantined: bool
    quarantine_resolved: bool
    closure_completed: bool


def _duration(value: float, name: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{name} must be finite and positive")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite and positive") from error
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _name(value: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 128 or "\x00" in value:
        raise ValueError("resource name must be a bounded nonempty plain string")


def _payload(value: object) -> None:
    if value is None or isinstance(value, (
        bool, int, float, complex, str, bytes, bytearray, memoryview,
        tuple, list, dict, set, frozenset,
    )):
        raise TypeError("resource payload must be an opaque object")


def _issued(key: object) -> None:
    if key is not _ISSUED:
        raise TypeError("lifecycle objects must be issued by their owner")


def _deadline(started: float, duration: float) -> float:
    result = started + duration
    if not isfinite(result) or result <= started:
        raise TerminalLifecycleError("deadline cannot be represented")
    return result


class OwnedResource:
    """Ledger-issued identity; ownership is never represented by a raw value."""

    def __init__(self, ledger: ResourceLedger, name: str, *, _key: object,
                 creation_slot: int | None = None):
        _issued(_key)
        self._ledger = ledger
        self._name = name
        self._identity = ledger._invocation._domain._resource_identity_unlocked(name)
        self._creation_slot = creation_slot
        self._payload: object | None = None
        self._state = OwnershipState.RESERVED
        self._release_attempted = False
        self._borrowers = 0
        self._pending = 0

    def _payload_unlocked(self) -> object | None:
        if self._state is OwnershipState.RELEASED:
            return None
        return self._identity_payload_unlocked()

    def _identity_payload_unlocked(self) -> object | None:
        return self._payload

    def _state_unlocked(self) -> OwnershipState:
        state = self._state
        if state is OwnershipState.RESERVED and self._payload_unlocked() is not None:
            state = OwnershipState.OWNED
        if state is OwnershipState.OWNED and self._ledger._quarantine_owner is not None:
            return OwnershipState.QUARANTINED
        return state

    def snapshot(self) -> ResourceSnapshot:
        with self._ledger._condition:
            return ResourceSnapshot(self._name, self._state_unlocked(), self._borrowers, self._pending,
                                    self._identity, self._release_attempted)


class BorrowedResource:
    """An explicit lease, not another owner. Release is blocked until it ends."""

    def __init__(self, resource: OwnedResource, *, _key: object):
        _issued(_key)
        self._resource = resource
        self._active = True

    @property
    def payload(self) -> object:
        with self._resource._ledger._condition:
            if not self._active:
                raise TerminalLifecycleError("borrow has ended")
            value = self._resource._payload_unlocked()
            if value is None:
                raise TerminalLifecycleError("borrowed resource has no payload")
            return value

    def close(self) -> None:
        with self._resource._ledger._condition:
            if self._active:
                self._resource._borrowers -= 1
                self._active = False

    def __enter__(self) -> BorrowedResource:
        if not self._active:
            raise TerminalLifecycleError("borrow has ended")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PendingOperation:
    """Retains all dependencies until completion, including after cancellation."""

    def __init__(self, ledger: ResourceLedger, resources: tuple[OwnedResource, ...], *, _key: object):
        _issued(_key)
        self._ledger = ledger
        self._resources = resources
        self._completed = False
        self._cancellation_requested = False

    def rearm_for_drain(self) -> None:
        """Reuse the SAME bounded read dependencies after known completion.

        Cleanup may continue draining existing capture resources; it may not
        introduce new dependencies or acquire another operation slot.
        """
        with self._ledger._condition:
            self._ledger._authorize_unlocked()
            if not self._completed or self._ledger._invocation._phase not in {
                InvocationPhase.PREFLIGHT, InvocationPhase.CREATED_SUSPENDED,
                InvocationPhase.RUNNING, InvocationPhase.CLEANING,
            }:
                raise TerminalLifecycleError("read operation cannot be rearmed")
            for resource in self._resources:
                if resource._state_unlocked() is not OwnershipState.OWNED or resource._pending:
                    raise TerminalLifecycleError("read dependency unavailable")
            self._ledger._pending.add(self)
            self._completed = False
            self._cancellation_requested = False
            for resource in self._resources:
                resource._pending += 1

    @property
    def completed(self) -> bool:
        with self._ledger._condition:
            return self._completed

    @property
    def cancellation_requested(self) -> bool:
        with self._ledger._condition:
            return self._cancellation_requested

    def request_cancel(self) -> None:
        with self._ledger._condition:
            if not self._completed:
                self._cancellation_requested = True

    def confirm_completed(self) -> None:
        with self._ledger._condition:
            if self._completed:
                raise TerminalLifecycleError("operation completion already recorded")
            for resource in self._resources:
                resource._pending -= 1
            self._ledger._pending.remove(self)
            self._completed = True


class ResourceLedger:
    """Reserve before acquisition, adopt immediately, release only with evidence.

    Methods only record supplied facts. They never call a release callback.
    Borrowed leases and pending operations prohibit release. A release whose
    result is uncertain cannot be attempted again through begin_release().
    """

    def __init__(self, invocation: TerminalInvocation, *, _key: object):
        _issued(_key)
        self._invocation = invocation
        self._condition = invocation._domain._condition
        self._resources: dict[str, OwnedResource] = {}
        self._pending: set[PendingOperation] = set()
        for slot, name in enumerate(("root-process", "primary-thread")):
            self._resources[name] = OwnedResource(self, name, _key=_ISSUED, creation_slot=slot)

    @property
    def _quarantine_owner(self) -> QuarantineOwner | None:
        owner = self._invocation._domain._quarantine
        return owner if owner is not None and owner._invocation is self._invocation else None

    def _check(self, resource: OwnedResource) -> None:
        if type(resource) is not OwnedResource or self._resources.get(resource._name) is not resource:
            raise TerminalLifecycleError("resource does not belong to this ledger")

    def _authorize_unlocked(self, owner: QuarantineOwner | None = None) -> None:
        if owner is not self._quarantine_owner:
            raise TerminalLifecycleError("ledger ownership transferred to quarantine")

    def snapshot(self) -> tuple[ResourceSnapshot, ...]:
        with self._condition:
            return tuple(item.snapshot() for item in self._resources.values())

    def _unique_payload(self, payload: object) -> None:
        _payload(payload)
        if any(item._identity_payload_unlocked() is payload for item in self._resources.values()):
            raise TerminalLifecycleError("payload already has an owner")

    def resource(self, name: str) -> OwnedResource:
        """Retrieve the existing owner, including the preallocated creation slots."""
        _name(name)
        with self._condition:
            if name not in self._resources:
                raise TerminalLifecycleError("resource has not been reserved")
            return self._resources[name]

    def reserve(self, name: str) -> OwnedResource:
        _name(name)
        with self._condition:
            self._authorize_unlocked()
            if self._invocation._phase is not InvocationPhase.PREFLIGHT:
                raise TerminalLifecycleError("resources must be reserved during preflight")
            if name in self._resources:
                raise TerminalLifecycleError("resource name already reserved")
            resource = OwnedResource(self, name, _key=_ISSUED)
            self._resources[name] = resource
            return resource

    def adopt(self, resource: OwnedResource, payload: object) -> None:
        with self._condition:
            self._authorize_unlocked()
            self._check(resource)
            if (self._invocation._phase is not InvocationPhase.PREFLIGHT
                    or resource._creation_slot is not None
                    or resource._state is not OwnershipState.RESERVED):
                raise TerminalLifecycleError("resource cannot be adopted")
            self._unique_payload(payload)
            resource._payload = payload
            resource._state = OwnershipState.OWNED

    def borrow(self, resource: OwnedResource) -> BorrowedResource:
        with self._condition:
            self._authorize_unlocked()
            self._check(resource)
            if resource._state_unlocked() not in {OwnershipState.OWNED, OwnershipState.QUARANTINED}:
                raise TerminalLifecycleError("resource cannot be borrowed")
            lease = BorrowedResource(resource, _key=_ISSUED)
            resource._borrowers += 1
            return lease

    def begin_operation(self, *resources: OwnedResource) -> PendingOperation:
        with self._condition:
            self._authorize_unlocked()
            if self._invocation._phase in {
                InvocationPhase.CLEANING, InvocationPhase.FINISHED, InvocationPhase.QUARANTINED,
            }:
                raise TerminalLifecycleError("cannot start an operation during cleanup")
            if not resources or len({id(item) for item in resources}) != len(resources):
                raise TerminalLifecycleError("operation requires distinct owned resources")
            for resource in resources:
                self._check(resource)
                if resource._state_unlocked() is not OwnershipState.OWNED or resource._pending:
                    raise TerminalLifecycleError("resource is unavailable for a pending operation")
            operation = PendingOperation(self, resources, _key=_ISSUED)
            self._pending.add(operation)
            for resource in resources:
                resource._pending += 1
            return operation

    def begin_release(self, resource: OwnedResource) -> object:
        """Reserve the ONE attempt and return its opaque payload for external release."""
        return self._begin_release(resource, None)

    def _begin_release(self, resource: OwnedResource, owner: QuarantineOwner | None) -> object:
        with self._condition:
            self._authorize_unlocked(owner)
            self._check(resource)
            if (resource._state_unlocked() not in {OwnershipState.OWNED, OwnershipState.QUARANTINED}
                    or resource._release_attempted or resource._borrowers or resource._pending):
                raise TerminalLifecycleError("resource cannot be released")
            payload = resource._payload_unlocked()
            resource._release_attempted = True
            resource._state = OwnershipState.RELEASING
            return payload

    def confirm_released(self, resource: OwnedResource) -> None:
        """Record confirmed release, or external proof resolving an uncertain result.

        This does not retry release. The payload is unavailable afterward.
        """
        self._confirm_released(resource, None)

    def _confirm_released(self, resource: OwnedResource, owner: QuarantineOwner | None) -> None:
        with self._condition:
            self._authorize_unlocked(owner)
            self._check(resource)
            if resource._state not in {OwnershipState.RELEASING, OwnershipState.RELEASE_UNCERTAIN}:
                raise TerminalLifecycleError("release was not attempted")
            resource._state = OwnershipState.RELEASED
            # Keep the opaque identity until the ledger itself is discarded, so
            # the same closed ownership token cannot be adopted as a new token.

    def mark_release_uncertain(self, resource: OwnedResource) -> None:
        self._mark_release_uncertain(resource, None)

    def _mark_release_uncertain(self, resource: OwnedResource, owner: QuarantineOwner | None) -> None:
        with self._condition:
            self._authorize_unlocked(owner)
            self._check(resource)
            if resource._state is not OwnershipState.RELEASING:
                raise TerminalLifecycleError("release was not attempted")
            resource._state = OwnershipState.RELEASE_UNCERTAIN
            self._invocation._record_cleanup_issue_unlocked(CleanupIssue.RELEASE_UNCERTAIN)

    def mark_still_owned(self, resource: OwnedResource) -> None:
        """Record definite failure to release, without inventing uncertainty/retry."""
        self._mark_still_owned(resource, None)

    def _mark_still_owned(self, resource: OwnedResource, owner: QuarantineOwner | None) -> None:
        with self._condition:
            self._authorize_unlocked(owner)
            self._check(resource)
            if resource._state is not OwnershipState.RELEASING:
                raise TerminalLifecycleError("release was not attempted")
            resource._state = OwnershipState.OWNED
            self._invocation._record_cleanup_issue_unlocked(CleanupIssue.FAILURE)

    def _clear_unlocked(self) -> bool:
        return not self._pending and all(
            item._state_unlocked() in {OwnershipState.RESERVED, OwnershipState.RELEASED}
            and not item._borrowers and not item._pending
            for item in self._resources.values()
        )


class CreationReceipt:
    """Preallocated ownership slots separate native success from phase adoption.

    Success is latched before returned resources are individually attached to
    preallocated ledger slots. Incomplete adoption prevents healthy completion.
    A call with no reported result is CU, not C1. Late reports may supply facts to
    the quarantine owner; they never reopen admission or change terminal phase.

    This is a Python evidence contract, not proof of exception-safe native
    marshaling. That proof remains a future backend requirement.
    """

    def __init__(self, invocation: TerminalInvocation, *, _key: object):
        _issued(_key)
        self._invocation = invocation
        self._attempted = False
        self._failed = False
        self._success = False

    def _snapshot_unlocked(self) -> CreationSnapshot:
        created = self._success
        pending = self._attempted and not self._failed and not created
        classification = (CreationClassification.PROCESS_CREATED if created else
                          CreationClassification.CREATE_CALL_FAILED if self._failed else
                          CreationClassification.CREATE_OUTCOME_UNCERTAIN if pending else
                          CreationClassification.PRE_CREATE)
        resources = self._invocation.resources._resources
        return CreationSnapshot(int(self._attempted), classification, created, pending,
                                resources["root-process"]._payload is not None,
                                resources["primary-thread"]._payload is not None)

    def snapshot(self) -> CreationSnapshot:
        with self._invocation._domain._condition:
            return self._snapshot_unlocked()

    def begin_call(self) -> bool:
        """Record the one call attempt after a final cancellation/deadline check.

        A future backend must couple this marker to its call boundary. No call
        is made here; admission/commit alone do not increment this counter.
        """
        invocation = self._invocation
        with invocation._domain._condition:
            if invocation._phase is not InvocationPhase.CREATE_COMMIT or self._attempted:
                raise TerminalLifecycleError("creation call cannot begin")
            if not invocation._launch_allowed_unlocked():
                return False
            self._attempted = True
            return True

    def _check_pending(self) -> None:
        # Scalar guard: do not construct evidence snapshots before latching a
        # known result. This narrows the Python boundary, not native atomicity.
        if not self._attempted or self._failed or self._success:
            raise TerminalLifecycleError("creation result is not pending")

    def record_failed(self) -> None:
        with self._invocation._domain._condition:
            self._check_pending()
            self._failed = True

    def record_success(self, payloads: tuple[object, object] | None = None) -> None:
        """Latch native success; optionally attach a supplied pair.

        The future bridge must call the no-argument form immediately on success,
        before any allocation/validation of returned resource wrappers.
        """
        with self._invocation._domain._condition:
            self._check_pending()
            self._success = True  # Never reset, including pair/attachment failure.
            if payloads is not None:
                if type(payloads) is not tuple or len(payloads) != 2 or payloads[0] is payloads[1]:
                    raise TerminalLifecycleError("creation requires distinct process/thread payloads")
                for payload in payloads:
                    self._invocation.resources._unique_payload(payload)
            if payloads is not None:
                self._attach_unlocked("root-process", payloads[0])
                self._attach_unlocked("primary-thread", payloads[1])

    def _attach_unlocked(self, name: str, payload: object) -> None:
        ledger = self._invocation.resources
        resource = ledger._resources[name]
        if not self._success or resource._payload is not None:
            raise TerminalLifecycleError("creation resource cannot be attached")
        ledger._unique_payload(payload)
        resource._payload = payload  # The registered slot owns it immediately.

    def attach_process(self, payload: object) -> None:
        with self._invocation._domain._condition:
            self._attach_unlocked("root-process", payload)

    def attach_thread(self, payload: object) -> None:
        with self._invocation._domain._condition:
            self._attach_unlocked("primary-thread", payload)


class QuarantineOwner:
    """Durable successor owner; old ledger inspection remains, mutation is revoked.

    Late receipt reports and existing borrow/I/O completion obligations remain
    valid evidence sources. Only this owner can authorize resource release.
    """

    def __init__(self, invocation: TerminalInvocation, reason: QuarantineReason, *, _key: object):
        _issued(_key)
        self._invocation = invocation
        self._ledger = invocation.resources
        self._reason = reason

    @property
    def creation(self) -> CreationReceipt:
        return self._invocation.creation

    def snapshot(self) -> tuple[ResourceSnapshot, ...]:
        return self._ledger.snapshot()

    def resource(self, name: str) -> OwnedResource:
        return self._ledger.resource(name)

    def begin_release(self, resource: OwnedResource) -> object:
        return self._ledger._begin_release(resource, self)

    def confirm_released(self, resource: OwnedResource) -> None:
        self._ledger._confirm_released(resource, self)

    def mark_release_uncertain(self, resource: OwnedResource) -> None:
        self._ledger._mark_release_uncertain(resource, self)

    def mark_still_owned(self, resource: OwnedResource) -> None:
        self._ledger._mark_still_owned(resource, self)

    def confirm_resolved(self) -> None:
        invocation = self._invocation
        with invocation._domain._condition:
            if (self._ledger._quarantine_owner is not self
                    or not invocation._cleanup_confirmed_unlocked()):
                raise TerminalLifecycleError("quarantine remediation is not confirmed")
            invocation._quarantine_resolved = True


class TerminalInvocation:
    """Domain-issued lifecycle lease, not an executable handler or approval."""

    def __init__(self, domain: LaunchAdmissionDomain, started: float,
                 execution_seconds: float, cleanup_seconds: float, *, _key: object):
        _issued(_key)
        self._domain = domain
        self._phase = InvocationPhase.PREFLIGHT
        self._execution_deadline = _deadline(started, execution_seconds)
        self._cleanup_seconds = cleanup_seconds
        self._cleanup_deadline: float | None = None
        self._cleanup_reason: CleanupReason | None = None
        self._cleanup_issues: tuple[CleanupIssue, ...] = ()
        self._cancellation_reason: CancellationReason | None = None
        self._resumed = False
        self._resume_committed = False
        self._exited = False
        self._containment_empty = False
        self._job_empty = False
        self._quarantine_resolved = False
        self._creation = CreationReceipt(self, _key=_ISSUED)
        self._resources = ResourceLedger(self, _key=_ISSUED)

    @property
    def _phase(self) -> InvocationPhase:
        owner = self._domain._quarantine
        if owner is not None and owner._invocation is self:
            return InvocationPhase.QUARANTINED
        return self._ordinary_phase

    @_phase.setter
    def _phase(self, phase: InvocationPhase) -> None:
        self._ordinary_phase = phase

    @property
    def _quarantine_reason(self) -> QuarantineReason | None:
        owner = self.resources._quarantine_owner
        return None if owner is None else owner._reason

    @property
    def creation(self) -> CreationReceipt:
        return self._creation

    @property
    def resources(self) -> ResourceLedger:
        return self._resources

    def _begin_cleanup_unlocked(self, reason: CleanupReason) -> None:
        if self._phase is InvocationPhase.CLEANING:
            return
        if self._phase in {InvocationPhase.FINISHED, InvocationPhase.QUARANTINED}:
            raise TerminalLifecycleError("terminal invocation cannot transition")
        deadline = _deadline(self._domain._now_unlocked(), self._cleanup_seconds)
        self._cleanup_reason = reason
        self._cleanup_deadline = deadline
        self._phase = InvocationPhase.CLEANING

    def _launch_allowed_unlocked(self) -> bool:
        if self._cancellation_reason is not None:
            self._begin_cleanup_unlocked(CleanupReason.CANCELLED)
            return False
        if self._domain._now_unlocked() >= self._execution_deadline:
            self._begin_cleanup_unlocked(CleanupReason.EXECUTION_TIMEOUT)
            return False
        return True

    def commit_creation(self) -> bool:
        with self._domain._condition:
            if self._phase is not InvocationPhase.PREFLIGHT:
                raise TerminalLifecycleError("creation can only be committed from preflight")
            if not self._launch_allowed_unlocked():
                return False
            self._phase = InvocationPhase.CREATE_COMMIT
            return True

    def adopt_created(self) -> bool:
        with self._domain._condition:
            if not self.creation._snapshot_unlocked().adoption_complete:
                raise TerminalLifecycleError("successful creation adoption is incomplete")
            if self._phase in {InvocationPhase.CLEANING, InvocationPhase.QUARANTINED}:
                return False
            if self._phase is not InvocationPhase.CREATE_COMMIT:
                raise TerminalLifecycleError("created phase cannot be adopted")
            if not self._launch_allowed_unlocked():
                return False
            self._phase = InvocationPhase.CREATED_SUSPENDED
            return True

    def commit_resume(self) -> bool:
        """Final model gate before one future resume attempt; performs no resume."""
        with self._domain._condition:
            if self._phase is not InvocationPhase.CREATED_SUSPENDED or self._resume_committed:
                raise TerminalLifecycleError("resume cannot be committed")
            if not self._launch_allowed_unlocked():
                return False
            self._resume_committed = True
            return True

    def record_resumed(self) -> None:
        """Record supplied success, even when cancellation arrived during resume."""
        with self._domain._condition:
            if not self._resume_committed or self._resumed or self._phase not in {
                InvocationPhase.CREATED_SUSPENDED, InvocationPhase.CLEANING, InvocationPhase.QUARANTINED,
            }:
                raise TerminalLifecycleError("resume success cannot be recorded")
            self._resumed = True
            if self._phase is InvocationPhase.CREATED_SUSPENDED and self._launch_allowed_unlocked():
                self._phase = InvocationPhase.RUNNING

    def confirm_process_exited(self) -> None:
        with self._domain._condition:
            if not self.creation._success or self._phase is InvocationPhase.FINISHED:
                raise TerminalLifecycleError("root exit cannot be confirmed")
            self._exited = True
            if self._phase not in {InvocationPhase.CLEANING, InvocationPhase.QUARANTINED}:
                reason = (CleanupReason.CANCELLED if self._cancellation_reason is not None else
                          CleanupReason.EXECUTION_TIMEOUT
                          if self._domain._now_unlocked() >= self._execution_deadline else CleanupReason.ROOT_EXIT)
                self._begin_cleanup_unlocked(reason)

    def confirm_containment_empty(self) -> None:
        with self._domain._condition:
            if not self._exited or self._phase not in {InvocationPhase.CLEANING, InvocationPhase.QUARANTINED}:
                raise TerminalLifecycleError("containment cannot be confirmed empty")
            self._containment_empty = True
            self._job_empty = True

    def confirm_job_empty(self) -> None:
        """Record supplied Job evidence without inferring creation or root exit.

        CU recovery can use an already-owned Job while root evidence remains
        unavailable. This performs no query and cannot resolve the receipt.
        P1 still requires explicit root-exit and containment confirmations.
        """
        with self._domain._condition:
            if (self._phase not in {InvocationPhase.CLEANING, InvocationPhase.QUARANTINED}
                    or not self.creation._attempted or self.creation._failed):
                raise TerminalLifecycleError("Job emptiness requires P1 or CU cleanup")
            self._job_empty = True

    def begin_cleanup(self, reason: CleanupReason) -> None:
        if type(reason) is not CleanupReason:
            raise TypeError("reason must be a CleanupReason")
        with self._domain._condition:
            receipt = self.creation._snapshot_unlocked()
            if self._phase is InvocationPhase.CLEANING:
                if reason in {CleanupReason.PREFLIGHT_FAILURE, CleanupReason.CREATE_CALL_FAILED,
                              CleanupReason.POST_CREATE_FAILURE, CleanupReason.CREATION_UNCERTAIN}:
                    self._record_cleanup_issue_unlocked(CleanupIssue.FAILURE)
                return  # First trigger and the one cleanup deadline are immutable.
            valid = {
                CleanupReason.PREFLIGHT_FAILURE: receipt.call_count == 0,
                CleanupReason.CREATE_CALL_FAILED: receipt.classification is CreationClassification.CREATE_CALL_FAILED,
                CleanupReason.POST_CREATE_FAILURE: receipt.process_created,
                CleanupReason.CREATION_UNCERTAIN: receipt.result_pending,
                CleanupReason.ROOT_EXIT: self._exited,
                CleanupReason.CANCELLED: self._cancellation_reason is not None,
                CleanupReason.EXECUTION_TIMEOUT: False,
            }
            if reason is CleanupReason.EXECUTION_TIMEOUT:
                valid[reason] = self._domain._now_unlocked() >= self._execution_deadline
            if not valid[reason]:
                raise TerminalLifecycleError("cleanup reason contradicts recorded evidence")
            self._begin_cleanup_unlocked(reason)

    def _cleanup_confirmed_unlocked(self) -> bool:
        receipt = self.creation._snapshot_unlocked()
        return (not receipt.result_pending and self.resources._clear_unlocked()
                and (not receipt.process_created or
                     (receipt.adoption_complete and self._exited and self._containment_empty)))

    def _record_cleanup_issue_unlocked(self, issue: CleanupIssue) -> None:
        if issue not in self._cleanup_issues:
            self._cleanup_issues += (issue,)  # Bounded by the finite issue enum.

    def record_cleanup_issue(self, issue: CleanupIssue) -> None:
        if type(issue) is not CleanupIssue:
            raise TypeError("issue must be a CleanupIssue")
        with self._domain._condition:
            if self._phase not in {InvocationPhase.CLEANING, InvocationPhase.QUARANTINED}:
                raise TerminalLifecycleError("cleanup issue requires cleanup")
            self._record_cleanup_issue_unlocked(issue)

    def cleanup_evidence(self) -> CleanupEvidence:
        with self._domain._condition:
            return CleanupEvidence(
                self._phase, self.creation._snapshot_unlocked(), self.resources.snapshot(),
                len(self.resources._pending), self._exited, self._containment_empty,
                self.resources._quarantine_owner is not None,
                self._job_empty,
            )

    def outcome(self) -> TerminalRuntimeOutcome:
        with self._domain._condition:
            if self._phase not in {InvocationPhase.FINISHED, InvocationPhase.QUARANTINED}:
                raise TerminalLifecycleError("outcome requires a terminal invocation")
            return TerminalRuntimeOutcome(self.cleanup_evidence(), self._resumed,
                                          self._cleanup_reason, self._cleanup_issues)

    def finish_cleanup(self) -> None:
        with self._domain._condition:
            if self._phase is not InvocationPhase.CLEANING or not self._cleanup_confirmed_unlocked():
                raise TerminalLifecycleError("cleanup is not confirmed")
            if self.resources._quarantine_owner is not None or self._domain._active is not self:
                raise TerminalLifecycleError("invocation lease is no longer active")
            self._phase = InvocationPhase.FINISHED
            self._domain._active = None
            self._domain._condition.notify_all()

    def quarantine(self, reason: QuarantineReason) -> None:
        if type(reason) is not QuarantineReason:
            raise TypeError("reason must be a QuarantineReason")
        with self._domain._condition:
            if self._phase is not InvocationPhase.CLEANING:
                raise TerminalLifecycleError("only cleanup can enter quarantine")
            if self._cleanup_confirmed_unlocked():
                raise TerminalLifecycleError("confirmed cleanup does not require quarantine")
            if (reason is QuarantineReason.DEADLINE_EXPIRED
                    and self._domain._now_unlocked() < self._cleanup_deadline):
                raise TerminalLifecycleError("cleanup deadline has not expired")
            if (reason is QuarantineReason.CREATION_UNCERTAIN
                    and not (self.creation._snapshot_unlocked().result_pending
                             or (self.creation._success
                                 and not self.creation._snapshot_unlocked().adoption_complete))):
                raise TerminalLifecycleError("creation result/adoption is not uncertain")
            if (reason is QuarantineReason.RELEASE_UNCERTAIN
                    and not any(item._state is OwnershipState.RELEASE_UNCERTAIN
                                for item in self.resources._resources.values())):
                raise TerminalLifecycleError("no release is uncertain")
            owner = QuarantineOwner(self, reason, _key=_ISSUED)
            issues = (*self._cleanup_issues, CleanupIssue.QUARANTINE)
            # All allocation precedes the single transfer pointer. Its assignment
            # retains the owner, revokes the old ledger and defines terminal phase.
            self._domain._poisoned = True
            self._domain._quarantine = owner
            self._cleanup_issues = issues
            self._domain._active = None
            self._domain._condition.notify_all()

    def confirm_quarantine_resolved(self) -> None:
        raise TerminalLifecycleError("remediation belongs to the quarantine owner")

    def snapshot(self) -> InvocationSnapshot:
        with self._domain._condition:
            return InvocationSnapshot(
                self._phase, self.creation._snapshot_unlocked(),
                tuple(item.snapshot() for item in self.resources._resources.values()),
                len(self.resources._pending), self._cancellation_reason, self._cleanup_reason,
                self._execution_deadline, self._cleanup_deadline, self._resumed, self._exited,
                self._containment_empty, self._quarantine_reason, self._quarantine_resolved,
                self._job_empty,
            )


class LaunchAdmissionDomain:
    """One explicitly shared, process-local admission and quarantine owner.

    Cancellation/shutdown are nonblocking requests, never cleanup claims. One
    condition serializes evidence changes; no backend callback runs under it.
    A future real-runtime factory must supply one durable shared domain rather
    than constructing a domain for each runtime. Isolated tests supply their own.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic):
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._condition = Condition()
        self._active: TerminalInvocation | None = None
        self._quarantine: QuarantineOwner | None = None
        self._shutdown_requested = False
        self._poisoned = False
        self._last_time: float | None = None
        self._identity = object()
        self._generation = 0

    def _resource_identity_unlocked(self, kind: str) -> ResourceIdentity:
        self._generation += 1
        return ResourceIdentity(self._identity, self._generation, kind)

    def _now_unlocked(self) -> float:
        value = self._clock()
        if type(value) not in {int, float}:
            raise TerminalLifecycleError("clock must return finite monotonic time")
        try:
            value = float(value)
        except OverflowError as error:
            raise TerminalLifecycleError("clock must return finite monotonic time") from error
        if not isfinite(value):
            raise TerminalLifecycleError("clock must return finite monotonic time")
        if self._last_time is not None and value < self._last_time:
            raise TerminalLifecycleError("clock moved backward")
        self._last_time = value
        return value

    def _snapshot_unlocked(self) -> AdmissionSnapshot:
        active = self._active is not None and self._active._phase is not InvocationPhase.QUARANTINED
        state = (LaunchAdmissionState.POISONED if self._poisoned else
                 LaunchAdmissionState.RUNNING if active else
                 LaunchAdmissionState.CLOSED if self._shutdown_requested else LaunchAdmissionState.OPEN)
        return AdmissionSnapshot(
            state, self._shutdown_requested or self._poisoned, active,
            self._quarantine is not None,
            self._quarantine is not None and self._quarantine._invocation._quarantine_resolved,
            state is LaunchAdmissionState.CLOSED,
        )

    def snapshot(self) -> AdmissionSnapshot:
        with self._condition:
            return self._snapshot_unlocked()

    @property
    def quarantined_invocation(self) -> TerminalInvocation | None:
        """Retained invocation evidence; release authority belongs to quarantine."""
        with self._condition:
            return None if self._quarantine is None else self._quarantine._invocation

    @property
    def quarantine_owner(self) -> QuarantineOwner | None:
        with self._condition:
            return self._quarantine

    def admit(self, *, execution_seconds: float = TERMINAL_TIMEOUT_SECONDS,
              cleanup_seconds: float = TERMINAL_CLEANUP_SECONDS) -> TerminalInvocation:
        """Start budget before preflight; this issues no authorization or OS call."""
        with self._condition:
            started = self._now_unlocked()  # Entry budget precedes policy validation.
            execution_seconds = _duration(execution_seconds, "execution_seconds")
            cleanup_seconds = _duration(cleanup_seconds, "cleanup_seconds")
            state = self._snapshot_unlocked().state
            if state is not LaunchAdmissionState.OPEN:
                raise LaunchAdmissionError(f"launch admission is {state.value}")
            invocation = TerminalInvocation(
                self, started, execution_seconds, cleanup_seconds, _key=_ISSUED,
            )
            self._active = invocation
            return invocation

    def _cancel_unlocked(self, reason: CancellationReason) -> bool:
        if self._active is None or self._active._phase is InvocationPhase.QUARANTINED:
            return False
        if self._active._cancellation_reason is None:
            self._active._cancellation_reason = reason
        self._condition.notify_all()
        return True

    def cancel_active(self) -> bool:
        with self._condition:
            return self._cancel_unlocked(CancellationReason.CANCEL_ACTIVE)

    def shutdown(self) -> AdmissionSnapshot:
        """Permanently close admission and request cancellation; never release resources."""
        with self._condition:
            self._shutdown_requested = True
            self._cancel_unlocked(CancellationReason.SHUTDOWN)
            return self._snapshot_unlocked()


# D1b orchestration depends only on this semantic protocol, never native values.


@dataclass(frozen=True, slots=True)
class FileEvidence:
    final_path: str
    size: int
    directory: bool
    disk: bool
    stamp: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReadResult:
    completed: bool
    data: bytes = b""
    eof: bool = False
    error: str | None = None

    def __post_init__(self):
        if (type(self.completed) is not bool or type(self.eof) is not bool
                or type(self.data) is not bytes or len(self.data) > TERMINAL_READ_CHUNK_BYTES
                or self.error is not None and (type(self.error) is not str or not self.error
                                              or len(self.error) > 64)
                or not self.completed and (self.data or self.eof or self.error)):
            raise ValueError("invalid bounded read evidence")


@dataclass(frozen=True, slots=True)
class CaptureSnapshot:
    text: str
    observed: int
    retained: int
    truncated: bool
    eof: bool
    complete: bool
    saturated: bool


class CaptureAccumulator:
    """One bounded byte prefix; decoding happens only on finalization."""
    def __init__(self):
        self._prefix = bytearray()
        self._observed = 0
        self._eof = False
        self._incomplete = False
        self._saturated = False
        self._final: CaptureSnapshot | None = None

    def accept(self, result: ReadResult):
        if self._final is not None or self._eof:
            raise TerminalLifecycleError("capture already ended")
        if not result.completed:
            return
        # Explicit saturation prevents counters growing without bound.
        maximum = (1 << 64) - 1
        if len(result.data) > maximum - self._observed:
            self._saturated = True
            self._observed = maximum
        else:
            self._observed += len(result.data)
        remaining = TERMINAL_STDOUT_BYTES_LIMIT - len(self._prefix)
        self._prefix.extend(result.data[:remaining])
        self._eof = result.eof
        self._incomplete |= result.error is not None

    def finalize(self, *, pending: bool = False) -> CaptureSnapshot:
        if self._final is None:
            retained = len(self._prefix)
            self._final = CaptureSnapshot(
                bytes(self._prefix).decode("utf-8", errors="replace"),
                self._observed, retained, self._saturated or self._observed > retained,
                self._eof, self._eof and not self._incomplete and not pending,
                self._saturated,
            )
        return self._final


@dataclass(frozen=True, slots=True)
class TerminalExecutionEvidence:
    exit_code: int | None
    stdout: CaptureSnapshot
    stderr: CaptureSnapshot
    verified_executable_identity: str | None
    environment_identity: str | None
    duration_seconds: float
    failure_stage: str | None
    reason_code: str | None
    descendants_terminated: bool

    def __post_init__(self):
        if (self.exit_code is not None and
                (type(self.exit_code) is not int or not 0 <= self.exit_code <= 0xFFFFFFFF)):
            raise ValueError("exit code must be observed unsigned DWORD or None")
        if type(self.stdout) is not CaptureSnapshot or type(self.stderr) is not CaptureSnapshot:
            raise TypeError("immutable capture snapshots required")
        if (type(self.duration_seconds) is not float or not isfinite(self.duration_seconds)
                or self.duration_seconds < 0 or type(self.descendants_terminated) is not bool):
            raise ValueError("invalid runtime execution evidence")

    @property
    def acceptable(self) -> bool:
        return (self.exit_code == 0 and self.stdout.complete and self.stderr.complete
                and not self.descendants_terminated and self.reason_code is None)


@dataclass(slots=True)
class StreamResources:
    name: str
    reader: OwnedResource
    event: OwnedResource
    memory: OwnedResource
    operation: PendingOperation | None = None
    capture: CaptureAccumulator = field(default_factory=CaptureAccumulator)
    eof: bool = False
    stopped: bool = False
    cancel_sent: bool = False


@dataclass(frozen=True, slots=True)
class StdioResources:
    children: tuple[OwnedResource, OwnedResource, OwnedResource]


class BackendResourceScope:
    """Publish opaque storage BEFORE its native acquisition attempt.

    Compound primitives publish every subresource here; partial setup remains
    visible even if the primitive does not return. No release callback or raw
    handle lives in the runtime. The ledger remains the sole ownership model.
    """
    def __init__(self, invocation: TerminalInvocation, *, checkpoint: Callable[[], None]):
        self.invocation = invocation
        self.checkpoint = checkpoint
        self.ordered: list[OwnedResource] = []
        self.creation_only: list[OwnedResource] = []
        self.streams: list[StreamResources] = []

    def own(self, name: str, payload: object, *, creation_only=False) -> OwnedResource:
        self.checkpoint()
        resource = self.invocation.resources.reserve(name)
        self.ordered.append(resource)
        self.invocation.resources.adopt(resource, payload)
        if creation_only:
            self.creation_only.append(resource)
        return resource


class TerminalBackend(Protocol):
    """Narrow borrowed-resource primitives; no admission or lifecycle policy."""
    def open_executable(self, scope: BackendResourceScope, path: str) -> OwnedResource: ...
    def open_cwd(self, scope: BackendResourceScope, path: str) -> OwnedResource: ...
    def file_evidence(self, resource: object) -> FileEvidence: ...
    def read_executable(self, resource: object, size: int) -> bytes: ...
    def create_job(self, scope: BackendResourceScope) -> OwnedResource: ...
    def configure_job(self, job: object) -> None: ...
    def make_stdio(self, scope: BackendResourceScope) -> StdioResources: ...
    def connect_pipe(self, memory: object) -> ReadResult: ...
    def prepare_launch(self, scope: BackendResourceScope, target: TerminalExecutionTarget,
                       command_line: str, environment: str, job: object,
                       children: tuple[object, object, object]) -> OwnedResource: ...
    def create_process(self, receipt: CreationReceipt, launch: object) -> None: ...
    def membership(self, process: object, job: object) -> bool: ...
    def resume(self, thread: object) -> int: ...
    def submit_read(self, memory: object) -> ReadResult: ...
    def inspect_read(self, memory: object) -> ReadResult: ...
    def cancel_read(self, memory: object) -> None: ...
    def root_exited(self, process: object) -> bool: ...
    def exit_code(self, process: object) -> int: ...
    def job_active(self, job: object) -> int: ...
    def terminate_job(self, job: object) -> None: ...
    def wait(self, process: object | None, memories: tuple[object, ...], seconds: float) -> None: ...
    def release(self, resource: object) -> bool: ...


def _same_local_final_path(final: str, approved: str) -> bool:
    """Drive-letter normalization only; every other character stays exact."""
    return final[0].upper() + final[1:] == approved[0].upper() + approved[1:]


class _RuntimeStop(Exception):
    pass


class WindowsTerminalRuntime:
    """Explicitly injected, unregistered, single non-main owner runtime.

    Admission is not authorization. Only a future separately reviewed wiring
    may connect this adapter to the ActionWorker's designated Thread object.
    """
    def __init__(self, *, backend: TerminalBackend, admission_domain: LaunchAdmissionDomain,
                 owner_thread: Thread):
        if backend is None or type(admission_domain) is not LaunchAdmissionDomain:
            raise TypeError("explicit backend and shared admission domain required")
        if not isinstance(owner_thread, Thread) or owner_thread is main_thread():
            raise ValueError("owner must be an explicitly designated non-main thread")
        self._backend = backend
        self._domain = admission_domain
        self._owner = owner_thread

    def cancel_active(self) -> bool:
        return self._domain.cancel_active()

    def shutdown(self) -> AdmissionSnapshot:
        return self._domain.shutdown()

    def signal_wake(self) -> None:
        with self._domain._condition:
            self._domain._condition.notify_all()

    def _now(self) -> float:
        with self._domain._condition:
            return self._domain._now_unlocked()

    def _checkpoint(self, invocation):
        snap = invocation.snapshot()
        if snap.phase is InvocationPhase.CLEANING:
            raise _RuntimeStop()
        if snap.cancellation_reason is not None:
            invocation.begin_cleanup(CleanupReason.CANCELLED)
            raise _RuntimeStop()
        if self._now() >= snap.execution_deadline:
            invocation.begin_cleanup(CleanupReason.EXECUTION_TIMEOUT)
            raise _RuntimeStop()

    def _call(self, invocation, method, *resources, extra=()):
        leases = []
        try:
            for resource in resources:
                leases.append(invocation.resources.borrow(resource))
            return method(*(lease.payload for lease in leases), *extra)
        finally:
            for lease in reversed(leases):
                lease.close()

    def _release(self, invocation, resource):
        state = resource.snapshot()
        if state.state is OwnershipState.RESERVED or state.state is OwnershipState.RELEASED:
            return True
        if state.release_attempted or state.pending_operations or state.borrowers:
            return False
        payload = invocation.resources.begin_release(resource)
        try:
            released = self._backend.release(payload)
        except BaseException:
            invocation.resources.mark_release_uncertain(resource)
            return False
        if released is True:
            invocation.resources.confirm_released(resource)
            return True
        invocation.resources.mark_still_owned(resource)
        return False

    def _io(self, invocation, stream, *, connect=False, submit=False):
        if stream.operation is None:
            stream.operation = invocation.resources.begin_operation(
                stream.reader, stream.event, stream.memory)
        elif submit:
            stream.operation.rearm_for_drain()
        method = (self._backend.connect_pipe if connect else
                  self._backend.submit_read if submit else self._backend.inspect_read)
        result = self._call(invocation, method, stream.memory)
        if type(result) is not ReadResult:
            raise TerminalLifecycleError("backend did not supply read evidence")
        if result.completed:
            stream.operation.confirm_completed()
            if not connect:
                stream.capture.accept(result)
                stream.eof = result.eof
                stream.stopped = result.eof or result.error is not None
            if result.error is not None:
                raise TerminalLifecycleError("native I/O failed")
        return result

    def execute(self, target: TerminalExecutionTarget) -> TerminalRuntimeOutcome:
        # Thread OBJECT identity also prevents recycled thread IDs admitting work.
        if current_thread() is not self._owner or current_thread() is main_thread():
            raise TerminalLifecycleError("terminal execution requires its non-main owner thread")
        invocation = self._domain.admit()
        started = invocation.snapshot().execution_deadline - TERMINAL_TIMEOUT_SECONDS
        scope = BackendResourceScope(invocation, checkpoint=lambda: self._checkpoint(invocation))
        job = None
        stage = "target_validation"
        reason = None
        verified = None
        environment_identity = None
        exit_code = None
        descendants = False
        try:
            if type(target) is not TerminalExecutionTarget:
                raise TerminalContractError("exact terminal target required")
            target.validate()
            environment_identity = target.environment_identity
            command = serialize_windows_command_line(target.executable_resolved, target.argv)
            environment = build_windows_environment_block(target.environment_mapping())
            self._checkpoint(invocation)
            stage = "executable_verification"
            executable = self._backend.open_executable(scope, target.executable_resolved)
            initial = self._call(invocation, self._backend.file_evidence, executable)
            _validate_local_path_form(initial.final_path, canonical=True)
            if (not _same_local_final_path(initial.final_path, target.executable_resolved) or not initial.disk
                    or initial.directory or not 0 < initial.size <= TERMINAL_EXECUTABLE_BYTES_LIMIT):
                raise TerminalContractError("executable object differs from approved target")
            digest = sha256()
            header = bytearray()
            total = 0
            while True:
                self._checkpoint(invocation)
                chunk = self._call(invocation, self._backend.read_executable, executable,
                                   extra=(TERMINAL_PE_HEADER_BYTES,))
                if type(chunk) is not bytes or len(chunk) > TERMINAL_PE_HEADER_BYTES:
                    raise TerminalContractError("invalid bounded executable read")
                if not chunk:
                    break
                total += len(chunk)
                if total > initial.size:
                    raise TerminalContractError("executable changed during verification")
                header.extend(chunk[:max(0, TERMINAL_PE_HEADER_BYTES - len(header))])
                digest.update(chunk)
            inspect_windows_console_pe(bytes(header), file_size=total)
            final = self._call(invocation, self._backend.file_evidence, executable)
            identity = "sha256:" + digest.hexdigest()
            if total != initial.size or initial != final or identity != target.executable_identity:
                raise TerminalContractError("executable identity mismatch")
            verified = identity
            self._checkpoint(invocation)
            stage = "cwd_verification"
            cwd = self._backend.open_cwd(scope, target.cwd)
            cwd_info = self._call(invocation, self._backend.file_evidence, cwd)
            _validate_local_path_form(cwd_info.final_path, canonical=True)
            if (not _same_local_final_path(cwd_info.final_path, target.cwd)
                    or not cwd_info.disk or not cwd_info.directory):
                raise TerminalContractError("cwd object differs from approved target")
            self._checkpoint(invocation)
            stage = "job_setup"
            job = self._backend.create_job(scope)
            self._call(invocation, self._backend.configure_job, job)
            self._checkpoint(invocation)
            stage = "stdio_setup"
            stdio = self._backend.make_stdio(scope)
            for stream in scope.streams:
                self._checkpoint(invocation)
                connected = self._io(invocation, stream, connect=True)
                if not connected.completed:
                    raise TerminalLifecycleError("unexpected pending pipe connection")
                self._io(invocation, stream, submit=True)
            self._checkpoint(invocation)
            stage = "launch_marshaling"
            leases = [invocation.resources.borrow(r) for r in (job, *stdio.children)]
            try:
                launch = self._backend.prepare_launch(
                    scope, target, command, environment, leases[0].payload,
                    tuple(lease.payload for lease in leases[1:]))
            finally:
                for lease in reversed(leases):
                    lease.close()
            self._checkpoint(invocation)
            stage = "create_process"
            if not invocation.commit_creation():
                raise _RuntimeStop()
            self._create(invocation, launch)
            receipt = invocation.creation.snapshot()
            if receipt.result_pending:
                invocation.begin_cleanup(CleanupReason.CREATION_UNCERTAIN)
                reason = "creation_uncertain"
                raise _RuntimeStop()
            if not receipt.process_created:
                if receipt.call_count:
                    invocation.begin_cleanup(CleanupReason.CREATE_CALL_FAILED)
                    reason = "native_creation_failed"
                raise _RuntimeStop()
            if not invocation.adopt_created():
                raise _RuntimeStop()
            stage = "post_create_setup"
            for resource in scope.creation_only:
                if not self._release(invocation, resource):
                    raise TerminalLifecycleError("creation-only resource release failed")
            process = invocation.resources.resource("root-process")
            thread = invocation.resources.resource("primary-thread")
            if not self._call(invocation, self._backend.membership, process, job):
                raise TerminalLifecycleError("creation-time Job membership not confirmed")
            self._checkpoint(invocation)
            if any(s.operation is None or s.stopped for s in scope.streams):
                raise TerminalLifecycleError("capture is not ready")
            stage = "resume"
            if not invocation.commit_resume():
                raise _RuntimeStop()
            if self._call(invocation, self._backend.resume, thread) != 1:
                raise TerminalLifecycleError("unexpected prior suspend count")
            invocation.record_resumed()
            stage = "execution"
            while invocation.snapshot().phase is InvocationPhase.RUNNING:
                self._checkpoint(invocation)
                for stream in scope.streams:
                    if not stream.stopped:
                        if stream.operation.completed:
                            self._io(invocation, stream, submit=True)
                        else:
                            self._io(invocation, stream)
                if self._call(invocation, self._backend.root_exited, process):
                    invocation.confirm_process_exited()
                    exit_code = self._call(invocation, self._backend.exit_code, process)
                    break
                self._wait(invocation, scope, process, invocation.snapshot().execution_deadline)
        except _RuntimeStop:
            pass
        except BaseException:
            receipt = invocation.creation.snapshot()
            trigger = (CleanupReason.CREATION_UNCERTAIN if receipt.result_pending else
                       CleanupReason.POST_CREATE_FAILURE if receipt.process_created else
                       CleanupReason.CREATE_CALL_FAILED if receipt.call_count else
                       CleanupReason.PREFLIGHT_FAILURE)
            invocation.begin_cleanup(trigger)
            reason = "creation_uncertain" if receipt.result_pending else "runtime_failure"
        receipt = invocation.creation.snapshot()
        if invocation.snapshot().phase is not InvocationPhase.CLEANING:
            trigger = (CleanupReason.CREATION_UNCERTAIN if receipt.result_pending else
                       CleanupReason.POST_CREATE_FAILURE if receipt.process_created else
                       CleanupReason.CREATE_CALL_FAILED if receipt.call_count else
                       CleanupReason.PREFLIGHT_FAILURE)
            invocation.begin_cleanup(trigger)
        try:
            descendants, exit_code = self._cleanup(invocation, scope, job, exit_code)
        except BaseException:
            invocation.record_cleanup_issue(CleanupIssue.FAILURE)
            invocation.quarantine(QuarantineReason.INCOMPLETE_CLEANUP)
        captures = {stream.name: stream.capture.finalize(
            pending=stream.operation is not None and not stream.operation.completed)
                    for stream in scope.streams}
        missing = CaptureAccumulator().finalize()
        trigger = invocation.snapshot().cleanup_reason
        if reason is None and trigger is CleanupReason.ROOT_EXIT:
            if not invocation.cleanup_evidence().cleanup_completed:
                reason = "cleanup_incomplete"
            elif descendants:
                reason = "descendants_outlived_root"
            elif exit_code != 0:
                reason = "exit_nonzero"
            elif not all(c.complete for c in captures.values()) or len(captures) != 2:
                reason = "capture_incomplete"
            elif invocation.outcome().cleanup_issues:
                reason = "cleanup_failure"
        if reason is None and trigger is not CleanupReason.ROOT_EXIT:
            reason = trigger.value
        execution = TerminalExecutionEvidence(
            exit_code, captures.get("stdout", missing), captures.get("stderr", missing),
            verified, environment_identity, max(0.0, self._now() - started),
            ("cleanup" if reason in {"cleanup_incomplete", "cleanup_failure",
                                     "capture_incomplete", "descendants_outlived_root"}
             else stage if reason is not None else None), reason, descendants)
        outcome = invocation.outcome()
        return TerminalRuntimeOutcome(outcome.cleanup, outcome.process_resumed,
                                      outcome.terminal_trigger, outcome.cleanup_issues, execution)

    def _create(self, invocation, launch):
        with invocation.resources.borrow(launch) as lease:
            self._backend.create_process(invocation.creation, lease.payload)

    def _wait(self, invocation, scope, process, deadline):
        resources = ([] if process is None else [process]) + [
            s.memory for s in scope.streams if s.operation is not None and not s.operation.completed]
        leases = []
        try:
            for r in resources:
                leases.append(invocation.resources.borrow(r))
            root = None if process is None else leases[0].payload
            memories = tuple(l.payload for l in leases[(0 if process is None else 1):])
            self._backend.wait(root, memories, min(0.02, max(0.0, deadline - self._now())))
        finally:
            for lease in reversed(leases):
                lease.close()

    def _cleanup(self, invocation, scope, job, exit_code):
        snap = invocation.snapshot()
        receipt = snap.creation
        process = (invocation.resources.resource("root-process")
                   if receipt.process_created and receipt.process_attached else None)
        descendants = False
        containment = False
        termination_requested = False
        membership_failed = False
        if (process is not None and job is not None
                and snap.cleanup_reason is CleanupReason.ROOT_EXIT):
            try:
                membership_failed = not self._call(invocation, self._backend.membership, process, job)
            except BaseException:
                membership_failed = True
            if membership_failed:
                invocation.record_cleanup_issue(CleanupIssue.FAILURE)
        if job is not None and (receipt.process_created or receipt.result_pending):
            active = None
            try:
                active = self._call(invocation, self._backend.job_active, job)
                if active == 0:
                    invocation.confirm_job_empty()
            except BaseException:
                invocation.record_cleanup_issue(CleanupIssue.FAILURE)
            descendants = (snap.cleanup_reason is CleanupReason.ROOT_EXIT
                           and active is not None and active > 0)
            if descendants:
                invocation.record_cleanup_issue(CleanupIssue.FAILURE)
            if (receipt.result_pending or snap.cleanup_reason is not CleanupReason.ROOT_EXIT
                    or active != 0 or membership_failed):
                try:
                    self._call(invocation, self._backend.terminate_job, job)
                    termination_requested = True
                except BaseException:
                    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
        # Known parent writers must close to make EOF observable, including P0/C1.
        for resource in scope.creation_only:
            if receipt.result_pending and resource.snapshot().name in {"launch", "attributes"}:
                continue
            self._release(invocation, resource)
        deadline = snap.cleanup_deadline
        cancel_at = self._now() + max(0.0, (deadline - self._now()) / 2)
        while self._now() < deadline:
            if process is not None and not invocation.snapshot().process_exited:
                try:
                    if self._call(invocation, self._backend.root_exited, process):
                        invocation.confirm_process_exited()
                        exit_code = self._call(invocation, self._backend.exit_code, process)
                except BaseException:
                    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
            if job is not None and (receipt.process_created or receipt.result_pending):
                try:
                    if self._call(invocation, self._backend.job_active, job) == 0:
                        invocation.confirm_job_empty()
                        containment = True
                        if invocation.snapshot().process_exited:
                            invocation.confirm_containment_empty()
                except BaseException:
                    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
            for stream in scope.streams:
                if stream.operation is None or stream.stopped:
                    continue
                try:
                    if self._now() >= cancel_at:
                        if not stream.operation.completed and not stream.cancel_sent:
                            stream.operation.request_cancel()
                            stream.cancel_sent = True
                            self._call(invocation, self._backend.cancel_read, stream.memory)
                        if stream.operation.completed:
                            stream.stopped = True
                            continue
                    if stream.operation.completed:
                        self._io(invocation, stream, submit=True)
                    else:
                        self._io(invocation, stream)
                except BaseException:
                    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
                    if stream.operation.completed:
                        stream.stopped = True
            settled = all(s.operation is None or s.operation.completed and s.stopped
                          for s in scope.streams)
            if settled and (not receipt.process_created and not receipt.result_pending
                            or containment and (receipt.result_pending or invocation.snapshot().process_exited)):
                break
            try:
                self._wait(invocation, scope,
                           process if process is not None and not invocation.snapshot().process_exited else None,
                           deadline)
            except BaseException:
                invocation.record_cleanup_issue(CleanupIssue.FAILURE)
                break
        # Last opportunity to request cancellation, never evidence of completion.
        for stream in scope.streams:
            if stream.operation is not None and not stream.operation.completed and not stream.cancel_sent:
                stream.operation.request_cancel()
                stream.cancel_sent = True
                try:
                    self._call(invocation, self._backend.cancel_read, stream.memory)
                except BaseException:
                    invocation.record_cleanup_issue(CleanupIssue.FAILURE)
        # Job is LAST. Pending dependencies and uncertain creation storage stay owned.
        for resource in reversed(scope.ordered):
            if resource is job or resource.snapshot().name in {"executable", "cwd"}:
                continue
            if receipt.result_pending and resource.snapshot().name in {"launch", "attributes"}:
                continue
            if receipt.process_created and not receipt.adoption_complete and resource.snapshot().name == "launch":
                continue
            self._release(invocation, resource)
        if receipt.process_created and receipt.adoption_complete and invocation.snapshot().containment_empty:
            self._release(invocation, invocation.resources.resource("primary-thread"))
            self._release(invocation, invocation.resources.resource("root-process"))
        for resource in reversed(scope.ordered):
            if resource.snapshot().name in {"cwd", "executable"}:
                self._release(invocation, resource)
        if job is not None and not receipt.result_pending and (
                not receipt.process_created or invocation.snapshot().containment_empty):
            self._release(invocation, job)
        try:
            invocation.finish_cleanup()
        except TerminalLifecycleError:
            invocation.quarantine(QuarantineReason.CREATION_UNCERTAIN if receipt.result_pending
                                  else QuarantineReason.INCOMPLETE_CLEANUP)
        return (descendants and termination_requested and invocation.snapshot().containment_empty), exit_code
