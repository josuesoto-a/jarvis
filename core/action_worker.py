"""One in-memory action worker, with bounded admission and transport results.

Construct with ActionWorker(app.orchestrator.run, capacity=8), then start().
submit() validates/copies input synchronously, but never executes the action or
waits for queue space. Capacity counts queued jobs, excluding the running job.
The caller must not mutate input DURING submit; subsequent mutations are safe.

IDs are canonical UUID strings, as in core.transport, and confer no permission.
A duplicate ID is rejected while queued, running, or awaiting result retrieval.
After consumption it may be submitted again and WILL execute again: this is an
in-memory correlation guard, not deduplication or confirmed-plan resumption.

result() consumes a result once. Unknown/consumed IDs raise KeyError; pending
ones raise TimeoutError unless the caller explicitly waits. Unconsumed results
remain available after shutdown and occupy memory until consumed. Only the
input queue is bounded; this component is not a durable result store.

shutdown() closes admission and drains all accepted jobs without cancellation.
It is idempotent, nonblocking by default, and cannot restart the worker. Optional
waiting joins the thread; timeout leaves draining in progress. An action that
never returns can prevent shutdown completion (the thread is non-daemon).
"""

from collections.abc import Callable
from dataclasses import dataclass
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
    response: TransportResponse | None = None


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
        self._queue: Queue[_Work] = Queue(maxsize=capacity)
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
            if request_id in self._work:
                raise ValueError("request_id already has an unconsumed job")
            self._queue.put_nowait(work)
            self._work[request_id] = work
            self._condition.notify_all()
        return request_id

    def result(self, request_id: str, *, timeout: float | None = 0) -> TransportResponse:
        """Consume by the ID returned from submit; None waits without a deadline.

        Concurrent readers race to consume: exactly one succeeds. Readers of an
        old job cannot consume a later job reusing the ID (including after a wait).
        """
        _validate_timeout(timeout)
        with self._condition:
            work = self._work[request_id]
            if work.response is None and timeout != 0 and current_thread() is self._thread:
                raise RuntimeError("worker cannot wait for its own results")
            ready = self._condition.wait_for(
                lambda: work.response is not None or self._work.get(request_id) is not work,
                timeout=timeout,
            )
            if self._work.get(request_id) is not work:
                raise KeyError(request_id)
            if not ready:
                raise TimeoutError(f"Result not ready: {request_id}")
            assert work.response is not None
            del self._work[request_id]
            self._condition.notify_all()
            return work.response

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
                work = self._queue.get_nowait()
            request_id = str(work.request.request_id)
            phase = "execution"
            try:
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
                    step_results=[], confirmation_steps=[],
                    metadata={"worker_error": {
                        "phase": phase, "exception_type": type(error).__name__,
                    }},
                )
            with self._condition:
                work.response = response
                self._queue.task_done()
                self._condition.notify_all()