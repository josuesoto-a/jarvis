"""Offline coordination between Responses events and the Jarvis ActionWorker.

This module contains no OpenAI SDK types and performs no network I/O.

It connects:

ResponsesEventAdapter
    -> TransportRequest
    -> ActionWorker

and:

ActionWorker
    -> TransportResponse
    -> action projection
    -> ResponsesOutputWriter

WAITING_FOR_PERMISSION is deliberately non-terminal and is never delivered
as a final function_call_output.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.action_worker import ActionWorker
from integrations.openai_live import (
    ActionProjection,
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    project_transport_response,
)
from integrations.openai_live_events import (
    ActionAdmission,
    ResponsesEventAdapter,
    ResponsesOutputWriter,
)


class CorrelationError(RuntimeError):
    """The Live/Responses identity disagrees with the ActionWorker identity."""


class LiveActionCoordinator:
    """Coordinate fake/real Responses events with one ActionWorker.

    This class does not own the worker lifecycle. The caller must start and
    shut down the worker.

    No planning or execution happens directly in this class.
    """

    def __init__(
        self,
        *,
        worker: ActionWorker,
        event_adapter: ResponsesEventAdapter,
        output_writer: ResponsesOutputWriter,
    ) -> None:
        self._worker = worker
        self._event_adapter = event_adapter
        self._output_writer = output_writer

    def ingest(
        self,
        event: Mapping[str, object],
    ) -> ActionAdmission | None:
        """Admit a new function call without waiting for its execution."""

        admission = self._event_adapter.ingest(
            event
        )

        if admission is None:
            return None

        if not admission.is_new:
            return admission

        request = admission.transport_request

        if request is None:
            raise CorrelationError(
                "new action admission has no TransportRequest"
            )

        worker_request_id = self._worker.submit(
            request
        )

        if worker_request_id != admission.request_id:
            raise CorrelationError(
                "ActionWorker request_id differs from Responses binding"
            )

        return admission

    def poll(
        self,
        call_id: str,
    ) -> ActionProjection | None:
        """Poll one call without blocking.

        Returns:
            None if the ActionWorker has no new snapshot yet.
            PendingPermissionUpdate while human approval is required.
            TerminalFunctionOutput after final completion/failure/blocking.

        Terminal outputs are delivered through ResponsesOutputWriter before
        being returned.
        """

        binding = self._event_adapter.binding_for_call(
            call_id
        )

        try:
            response = self._worker.result(
                binding.request_id,
                timeout=0,
            )

        except TimeoutError:
            return None

        projection = project_transport_response(
            binding,
            response,
        )

        if isinstance(
            projection,
            TerminalFunctionOutput,
        ):
            self._output_writer.deliver(
                projection
            )

        return projection

    def confirm(
        self,
        call_id: str,
        *,
        confirmed_steps: frozenset[int],
    ) -> str:
        """Queue a trusted local confirmation for the bound Jarvis request."""

        binding = self._event_adapter.binding_for_call(
            call_id
        )

        request_id = self._worker.confirm(
            binding.request_id,
            confirmed_steps=confirmed_steps,
        )

        if request_id != binding.request_id:
            raise CorrelationError(
                "ActionWorker confirmation returned a different request_id"
            )

        return request_id

    def binding_request_id(
        self,
        call_id: str,
    ) -> str:
        """Return the local Jarvis request ID bound to one Responses call."""

        return self._event_adapter.binding_for_call(
            call_id
        ).request_id
