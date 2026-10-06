"""D1G-D1b0: non-executing terminal lifecycle and ownership infrastructure.

This module records evidence supplied by a future backend; it does not acquire,
release, launch, resume, wait for, or terminate any OS resource. Admission is
not authorization. All owners that need process-wide admission MUST share one
LaunchAdmissionDomain. There is deliberately no default/global domain here.

Snapshots exclude opaque payloads. No destructor performs cleanup. A domain
retains its active invocation and any quarantine, even if callers discard them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from threading import Condition
from time import monotonic
from typing import Callable

from capabilities.terminal import TERMINAL_CLEANUP_SECONDS, TERMINAL_TIMEOUT_SECONDS


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
    call_count: int
    classification: CreationClassification | None
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
                    None if self.result_pending else
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

    def __post_init__(self) -> None:
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
    """Lifecycle-only result. COMPLETED does not assert a program exit code."""

    cleanup: CleanupEvidence
    process_resumed: bool
    terminal_trigger: CleanupReason
    cleanup_issues: tuple[CleanupIssue, ...]

    def __post_init__(self) -> None:
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
        return self.cleanup.creation.process_created

    @property
    def process_exited(self) -> bool:
        return self.cleanup.process_exited

    @property
    def cleanup_completed(self) -> bool:
        return self.cleanup.cleanup_completed

    @property
    def disposition(self) -> RuntimeDisposition:
        if (self.cleanup_completed and self.process_resumed
                and self.terminal_trigger is CleanupReason.ROOT_EXIT and not self.cleanup_issues):
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
    A call with no reported result is NOT C1. Late reports may supply facts to
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
                          None if pending else CreationClassification.PRE_CREATE)
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
        if not self._snapshot_unlocked().result_pending:
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
