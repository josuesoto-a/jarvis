"""One in-memory action worker, with bounded admission and transport results.

Construct with ActionWorker(app.orchestrator.run, capacity=8), then start().
submit() validates/copies input synchronously, but never executes the action or
waits for queue space. Capacity counts queued jobs, excluding the running job.
The caller must not mutate input DURING submit; subsequent mutations are safe.

IDs are canonical UUID strings and confer no permission. Accepted IDs remain
reserved for this worker's lifetime, even after terminal result consumption.
A fresh action requires a fresh ID, so delayed confirmations cannot target a
replacement plan.

confirm(id, confirmed_steps=frozenset({...})) requires a bound Orchestrator.run.
It validates against that same owner, then enqueues resume without executing it.
Only the Orchestrator owns pending plans and cumulative authorization. Run-only
callables remain supported for submit(), but cannot support confirm().

result() consumes snapshots in publication order, including an unread WAITING
snapshot preceding a resume result. Consuming WAITING preserves confirmability.
Unknown/consumed terminal IDs raise KeyError; pending results raise TimeoutError
unless the caller explicitly waits. Results remain readable after shutdown.
Once a closed waiting request has no unread results, result raises RuntimeError
instead of waiting for a confirmation that can no longer be admitted.
Only the operation queue is bounded; pending records and results use memory.

shutdown() closes admission and drains all accepted jobs without cancellation.
It is idempotent, nonblocking by default, and cannot restart the worker. Optional
waiting joins the thread; timeout leaves draining in progress. An action that
never returns can prevent shutdown completion (the thread is non-daemon).
Waiting plans are never auto-confirmed. After shutdown they cannot be resumed
through this worker; the Orchestrator retains its in-memory state.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from queue import Queue
from threading import Condition, Thread, current_thread
from typing import TYPE_CHECKING

from core.contracts import ActionRequest
from core.transport import TransportRequest, TransportResponse
from core.transport import to_action_request, to_transport_response

if TYPE_CHECKING:
    from core.orchestrator import OrchestrationResult


@dataclass
class _Work:
    request: ActionRequest
    state: str = "queued"
    responses: deque[TransportResponse] = field(default_factory=deque)

    @property
    def response(self) -> TransportResponse | None:
        return self.responses[0] if self.responses else None


@dataclass(frozen=True)
class _Operation:
    work: _Work
    # None is initial run; a nonempty set is a queued resume command, not grants.
    confirmed_steps: frozenset[int] | None = None


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (not isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be finite and non-negative, or None")


class ActionWorker:
    """Run an injected ActionRequest -> OrchestrationResult callable serially."""

    def __init__(
        self,
        run: Callable[[ActionRequest], "OrchestrationResult"],
        *,
        capacity: int = 8,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not callable(run):
            raise TypeError("run must be callable")
        self._run = run
        owner = getattr(run, "__self__", None)
        self._resume = getattr(owner, "resume", None)
        self._validate_confirmation = getattr(owner, "validate_confirmation", None)
        self._queue: Queue[_Operation] = Queue(maxsize=capacity)
        self._used_request_ids: set[str] = set()
        self._work: dict[str, _Work] = {}
        # All lifecycle, queue admission/removal and result state use this lock.
        # Queue operations under it are always nonblocking. Condition waits
        # release it; neither domain execution nor thread joins hold it.
        self._condition = Condition()
        self._thread: Thread | None = None
        self._closed = False

    def start(self) -> None:
        """Start exactly one thread; repeated starts are harmless until closed."""
        with self._condition:
            if self._closed:
                raise RuntimeError("worker is shut down")
            if self._thread is None:
                thread = Thread(target=self._serve, name="action-worker", daemon=False)
                thread.start()
                self._thread = thread

    def _check_accepting(self) -> None:
        # Called only with the condition lock held.
        if self._closed:
            raise RuntimeError("worker is shut down")
        if self._thread is None:
            raise RuntimeError("worker has not been started")

    def submit(self, payload: TransportRequest) -> str:
        """Return canonical request_id; raise queue.Full immediately if full.

        Transport validation errors propagate without admission or execution.
        Copy/normalization cost scales with payload size. No action runs here.
        A concurrent shutdown during normalization rejects this submission.
        """
        with self._condition:
            self._check_accepting()
        request = to_action_request(payload)
        request_id = str(request.request_id)
        work = _Work(request)
        with self._condition:
            self._check_accepting()
            if request_id in self._used_request_ids:
                raise ValueError("request_id already used; use a fresh ID for a new action")
            self._queue.put_nowait(_Operation(work))
            self._used_request_ids.add(request_id)
            self._work[request_id] = work
            self._condition.notify_all()
        return request_id

    def confirm(self, request_id: str, *, confirmed_steps: frozenset[int]) -> str:
        """Admit resume immediately or raise; never execute on the caller thread.

        Use the canonical ID returned by submit and an explicit nonempty
        frozenset of outstanding step numbers. Unknown IDs raise KeyError;
        terminal/non-waiting/in-flight requests and invalid steps are rejected.
        queue.Full leaves scheduling and Orchestrator authorization untouched.
        A concurrent shutdown either rejects this operation or drains it once.
        """
        with self._condition:
            self._check_accepting()
            work = self._work.get(request_id)
            if work is None:
                if request_id in self._used_request_ids:
                    raise ValueError("request is terminal")
                raise KeyError(request_id)
            if work.state != "waiting_for_permission":
                raise ValueError("request is not waiting for confirmation (or resume is queued/running)")
            if not callable(self._resume) or not callable(self._validate_confirmation):
                raise RuntimeError("confirm requires a bound Orchestrator.run")
            # Read-only domain validation: no plans or grant history are copied.
            self._validate_confirmation(
                work.request.request_id, confirmed_steps=confirmed_steps,
            )
            self._queue.put_nowait(_Operation(work, confirmed_steps))
            work.state = "queued_resume"
            self._condition.notify_all()
        return request_id

    def result(self, request_id: str, *, timeout: float | None = 0) -> TransportResponse:
        """Consume by the ID returned from submit; None waits without a deadline.

        Concurrent readers race to consume each snapshot exactly once. WAITING
        does not end a request; results from accepted resumes remain retrievable.
        """
        _validate_timeout(timeout)
        with self._condition:
            work = self._work[request_id]
            if work.response is None and timeout != 0 and current_thread() is self._thread:
                raise RuntimeError("worker cannot wait for its own results")
            ready = self._condition.wait_for(
                lambda: (
                    work.response is not None or self._work.get(request_id) is not work
                    or (self._closed and work.state == "waiting_for_permission")
                ),
                timeout=timeout,
            )
            if self._work.get(request_id) is not work:
                raise KeyError(request_id)
            if work.response is None and self._closed and work.state == "waiting_for_permission":
                raise RuntimeError("worker is shut down; waiting request cannot resume")
            if not ready:
                raise TimeoutError(f"Result not ready: {request_id}")
            assert work.response is not None
            response = work.responses.popleft()
            if work.state == "terminal" and not work.responses:
                del self._work[request_id]
            self._condition.notify_all()
            return response

    def shutdown(self, *, wait: bool = False, timeout: float | None = None) -> None:
        """Drain accepted jobs; optionally join, raising TimeoutError on timeout."""
        _validate_timeout(timeout)
        if not wait and timeout is not None:
            raise ValueError("timeout requires wait=True")
        with self._condition:
            thread = self._thread
            if wait and thread is current_thread():
                raise RuntimeError("worker cannot join itself")
            self._closed = True
            self._condition.notify_all()
        if wait and thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Worker is still draining accepted jobs")

    def _serve(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or not self._queue.empty())
                if self._queue.empty():
                    return
                operation = self._queue.get_nowait()
                work = operation.work
                resuming = operation.confirmed_steps is not None
                work.state = "running_resume" if resuming else "running"
            request_id = str(work.request.request_id)
            phase = "resume" if resuming else "execution"
            try:
                if resuming:
                    outcome = self._resume(
                        work.request.request_id, confirmed_steps=operation.confirmed_steps,
                    )
                else:
                    outcome = self._run(work.request)
                phase = "response_conversion"
                response = to_transport_response(outcome)
                if response["request_id"] != request_id:
                    raise ValueError("result request_id differs from submitted request")
            except Exception as error:
                # Do not stringify arbitrary exceptions: __str__ can itself fail.
                # Keep failures transportable, without leaking traceback/locals.
                response = TransportResponse(
                    request_id=request_id, status="failed", message=None,
                    error=f"Worker {phase} failed ({type(error).__name__}).",
                    step_results=[], confirmation_steps=[], pending_confirmation_steps=[],
                    metadata={"worker_error": {
                        "phase": phase, "exception_type": type(error).__name__,
                    }},
                )
            with self._condition:
                work.responses.append(response)
                work.state = (
                    "waiting_for_permission" if response["status"] == "waiting_for_permission"
                    else "terminal"
                )
                self._queue.task_done()
                self._condition.notify_all()
