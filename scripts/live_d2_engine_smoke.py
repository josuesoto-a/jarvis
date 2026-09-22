"""Task 15.4D2: real OpenAI Live + real Jarvis Action Engine.

Real components:
- OpenAI Live WebSocket
- Responses delegation
- perform_action call
- ResponsesEventAdapter
- LiveActionCoordinator
- ActionWorker
- Orchestrator
- Executor
- PlanValidator
- CapabilityResolver
- PermissionEngine
- CapabilityRuntimeRegistry
- function_call_output back to OpenAI
- delegated continuation

Controlled smoke-test components:
- deterministic Planner
- offline capability handler

No microphone, audio playback, browser, shell, or external capability is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

# Allow direct execution from scripts/ without PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(ROOT),
    )

from dotenv import load_dotenv
from openai import OpenAI

from core.action_worker import ActionWorker
from core.contracts import (
    CapabilitySpec,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    PermissionMode,
    RiskLevel,
)
from core.executor import Executor
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry

from integrations.openai_live import (
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    perform_action_argument_schema,
)
from integrations.openai_live_coordinator import (
    LiveActionCoordinator,
)
from integrations.openai_live_events import (
    ResponsesEventAdapter,
)


# ============================================================
# CONSTANTS
# ============================================================

INITIAL_RESPONSE_EVENT_ID = "d2_initial_response"
POST_TOOL_UPDATE_EVENT_ID = "d2_disable_tools"
TOOL_RESULT_EVENT_ID = "d2_tool_result"
CONTINUE_EVENT_ID = "d2_continue"

MAX_EVENTS = 300
WORKER_TIMEOUT = 3.0


# ============================================================
# DETERMINISTIC PLANNER
# ============================================================

class SmokePlanner:
    """Produce exactly one safe offline automatic plan."""

    def __init__(self) -> None:
        self.calls = []
        self.plans = []

    def plan(
        self,
        request,
    ) -> ExecutionPlan:
        self.calls.append(
            request
        )

        plan = ExecutionPlan(
            request_id=request.request_id,
            steps=(
                ExecutionStep(
                    step_number=1,
                    description=(
                        "Execute the D2 offline smoke capability"
                    ),
                    capability="web_search",
                    arguments={
                        "query": (
                            ExecutionArgument.literal(
                                request.goal
                            )
                        ),
                    },
                    risk=RiskLevel.LOW,
                    permission=(
                        PermissionMode.AUTOMATIC
                    ),
                ),
            ),
            overall_risk=RiskLevel.LOW,
        )

        self.plans.append(
            plan
        )

        return plan


# ============================================================
# DEFERRED WRITER
# ============================================================

class DeferredTerminalWriter:
    """Capture terminal Jarvis output without touching Live yet.

    LiveActionCoordinator normally asks its writer to deliver terminal output.
    For real Live, D1 proved delivery must wait until the delegated Response
    itself is complete and session.update has been acknowledged.

    This writer therefore stores exactly one terminal output for later delivery
    by the Live protocol loop.
    """

    def __init__(self) -> None:
        self.output = None
        self.deliver_calls = 0

    def deliver(
        self,
        output,
    ) -> bool:
        if not isinstance(
            output,
            TerminalFunctionOutput,
        ):
            raise TypeError(
                "DeferredTerminalWriter requires TerminalFunctionOutput"
            )

        self.deliver_calls += 1

        if self.output is None:
            self.output = output
            return True

        if self.output == output:
            return False

        raise RuntimeError(
            "Conflicting terminal output was produced"
        )


# ============================================================
# BUILD REAL JARVIS ENGINE
# ============================================================

def build_engine():
    planner = SmokePlanner()

    registry = (
        CapabilityRegistry()
    )

    registry.register(
        CapabilitySpec(
            name="web_search",
            description=(
                "Offline D2 smoke-test capability."
            ),
        )
    )

    runtime_calls = []

    runtimes = (
        CapabilityRuntimeRegistry()
    )

    def offline_handler(
        arguments,
    ):
        snapshot = dict(
            arguments
        )

        runtime_calls.append(
            snapshot
        )

        return {
            "mode": (
                "15.4D2-real-action-engine"
            ),
            "executed": True,
            "received_query": (
                snapshot["query"]
            ),
        }

    runtimes.register(
        "web_search",
        offline_handler,
    )

    executor = Executor(
        validator=(
            PlanValidator(
                CapabilityResolver(
                    registry
                )
            )
        ),
        permission_engine=(
            PermissionEngine()
        ),
        runtime_registry=runtimes,
    )

    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )

    worker = ActionWorker(
        orchestrator.run,
        capacity=8,
    )

    worker.start()

    return (
        planner,
        runtime_calls,
        worker,
    )


# ============================================================
# HELPERS
# ============================================================

def event_to_dict(
    event,
):
    if isinstance(
        event,
        dict,
    ):
        return event

    model_dump = getattr(
        event,
        "model_dump",
        None,
    )

    if callable(
        model_dump
    ):
        return model_dump(
            mode="json"
        )

    to_dict = getattr(
        event,
        "to_dict",
        None,
    )

    if callable(
        to_dict
    ):
        return to_dict()

    raise TypeError(
        f"Cannot convert event type: {type(event)!r}"
    )


def response_id_from_created(
    inner,
):
    response = inner.get(
        "response"
    )

    if not isinstance(
        response,
        dict,
    ):
        return None

    response_id = response.get(
        "id"
    )

    if isinstance(
        response_id,
        str,
    ):
        return response_id

    return None


def wait_for_jarvis_terminal(
    coordinator,
    call_id,
):
    deadline = (
        monotonic()
        + WORKER_TIMEOUT
    )

    while (
        monotonic()
        < deadline
    ):
        projection = (
            coordinator.poll(
                call_id
            )
        )

        if projection is None:
            sleep(
                0.005
            )
            continue

        if isinstance(
            projection,
            PendingPermissionUpdate,
        ):
            raise RuntimeError(
                "D2 automatic smoke action unexpectedly requires permission"
            )

        if isinstance(
            projection,
            TerminalFunctionOutput,
        ):
            return projection

        raise RuntimeError(
            f"Unexpected Jarvis projection: {type(projection)!r}"
        )

    raise TimeoutError(
        "Jarvis Action Engine did not produce a terminal result"
    )


def print_server_error(
    payload,
):
    print()
    print(
        "============================================================"
    )
    print(
        "SERVER ERROR"
    )
    print(
        "============================================================"
    )

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
    )


# ============================================================
# CREDENTIALS
# ============================================================

print(
    "============================================================"
)
print(
    "TASK 15.4D2 - REAL LIVE + REAL JARVIS ACTION ENGINE"
)
print(
    "============================================================"
)

env_path = (
    ROOT
    / ".env"
)

load_dotenv(
    dotenv_path=env_path
)

api_key = os.getenv(
    "OPENAI_API_KEY"
)

if not api_key:
    raise RuntimeError(
        f"OPENAI_API_KEY was not loaded from {env_path}"
    )

print()
print(
    "Credentials: loaded"
)
print(
    "API key: NOT DISPLAYED"
)


# ============================================================
# REAL JARVIS STACK
# ============================================================

(
    planner,
    runtime_calls,
    worker,
) = build_engine()

adapter = (
    ResponsesEventAdapter()
)

deferred_writer = (
    DeferredTerminalWriter()
)

coordinator = (
    LiveActionCoordinator(
        worker=worker,
        event_adapter=adapter,
        output_writer=deferred_writer,
    )
)


# ============================================================
# LIVE SESSION CONFIG
# ============================================================

client = OpenAI(
    api_key=api_key
)

session = {
    "model": "gpt-live-1",

    "instructions": (
        "This is a Jarvis integration test. "
        "Use the configured Responses backend for the requested action."
    ),

    "store": False,

    "input": [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Run the Jarvis D2 integration smoke test. "
                        "Verify that this request crosses the real Live "
                        "Responses protocol and the real local Jarvis "
                        "Action Engine."
                    ),
                }
            ],
        }
    ],

    "delegation": {
        "type": "responses",

        "responses": {
            "model": "gpt-5.6-luna",

            "instructions": (
                "Translate this request into exactly one perform_action "
                "call. Use the full user request as goal and raw_input. "
                "Use an empty context object. "
                "Do not call any other tool."
            ),

            "tools": [
                {
                    "type": "function",
                    "name": "perform_action",
                    "description": (
                        "Submit a user-requested action to the local "
                        "Jarvis Action Engine."
                    ),
                    "parameters": (
                        perform_action_argument_schema()
                    ),
                    "strict": False,
                }
            ],

            "tool_choice": {
                "type": "function",
                "name": "perform_action",
            },

            "parallel_tool_calls": False,

            "max_output_tokens": 128,
        },
    },
}


# ============================================================
# STATE
# ============================================================

session_started = False

delegation_id = None

first_response_id = None
first_response_completed = False

admission = None
call_id = None

jarvis_terminal = None

backend_update_requested = False
backend_update_acknowledged = False

function_output_sent = False

continuation_requested = False
continuation_response_id = None
continuation_started = False
continuation_completed = False

continuation_text_parts = []


# ============================================================
# REAL OPENAI ↔ REAL JARVIS
# ============================================================

try:
    print()
    print(
        "Opening REAL Live WebSocket..."
    )

    with client.live.connect() as connection:

        print(
            "WebSocket: connected"
        )

        connection.session.start(
            session=session
        )

        print(
            "session.start sent"
        )

        for event_number in range(
            1,
            MAX_EVENTS + 1,
        ):
            event = (
                connection.recv()
            )

            payload = (
                event_to_dict(
                    event
                )
            )

            event_type = (
                payload.get(
                    "type"
                )
            )

            # =================================================
            # SERVER ERROR
            # =================================================

            if event_type == "error":
                print_server_error(
                    payload
                )

                raise RuntimeError(
                    "Live server returned an error"
                )

            # =================================================
            # SESSION START
            # =================================================

            if (
                event_type
                == "session.started"
            ):
                session_started = True

                print()
                print(
                    "✓ session.started"
                )

                print(
                    "Requesting delegated Responses execution..."
                )

                connection.response.create(
                    event_id=(
                        INITIAL_RESPONSE_EVENT_ID
                    )
                )

                continue

            # =================================================
            # DELEGATION CREATED
            # =================================================

            if (
                event_type
                == "session.delegation.created"
            ):
                delegation = (
                    payload.get(
                        "delegation"
                    )
                )

                if isinstance(
                    delegation,
                    dict,
                ):
                    value = (
                        delegation.get(
                            "id"
                        )
                    )

                    if isinstance(
                        value,
                        str,
                    ):
                        delegation_id = (
                            value
                        )

                print()
                print(
                    "✓ session.delegation.created"
                )
                print(
                    "delegation_id:",
                    delegation_id,
                )

                continue

            # =================================================
            # SESSION UPDATED
            # =================================================

            if (
                event_type
                == "session.updated"
            ):
                if (
                    not backend_update_requested
                ):
                    continue

                backend_update_acknowledged = (
                    True
                )

                print()
                print(
                    "✓ session.updated"
                )

                if jarvis_terminal is None:
                    raise RuntimeError(
                        "Jarvis terminal output is missing"
                    )

                item = (
                    jarvis_terminal.function_call_output_item()
                )

                print()
                print(
                    "Sending REAL Jarvis function_call_output..."
                )

                connection.response.item.create(
                    event_id=(
                        TOOL_RESULT_EVENT_ID
                    ),
                    item=item,
                )

                function_output_sent = (
                    True
                )

                print(
                    "✓ real Jarvis output sent"
                )

                print(
                    "Requesting delegated continuation..."
                )

                connection.response.create(
                    event_id=(
                        CONTINUE_EVENT_ID
                    )
                )

                continuation_requested = (
                    True
                )

                continue

            # =================================================
            # RESPONSES EVENTS
            # =================================================

            if (
                event_type
                != "response.event"
            ):
                continue

            # Feed EVERY response.event through our production
            # adapter/coordinator. Irrelevant event kinds return None.
            current_admission = (
                coordinator.ingest(
                    payload
                )
            )

            if (
                current_admission
                is not None
            ):
                if admission is None:
                    admission = (
                        current_admission
                    )

                    call_id = (
                        admission.call_id
                    )

                    print()
                    print(
                        "============================================================"
                    )
                    print(
                        "REAL LIVE CALL ADMITTED TO REAL JARVIS"
                    )
                    print(
                        "============================================================"
                    )

                    print(
                        "delegation_id:",
                        admission.delegation_id,
                    )

                    print(
                        "call_id:",
                        admission.call_id,
                    )

                    print(
                        "request_id:",
                        admission.request_id,
                    )

                    print(
                        "is_new:",
                        admission.is_new,
                    )

                elif (
                    current_admission.call_id
                    != admission.call_id
                ):
                    raise RuntimeError(
                        "Unexpected second action call"
                    )

            inner = (
                payload.get(
                    "event"
                )
            )

            if not isinstance(
                inner,
                dict,
            ):
                continue

            inner_type = (
                inner.get(
                    "type"
                )
            )

            # =================================================
            # RESPONSE CREATED
            # =================================================

            if (
                inner_type
                == "response.created"
            ):
                response_id = (
                    response_id_from_created(
                        inner
                    )
                )

                if first_response_id is None:
                    first_response_id = (
                        response_id
                    )

                    print()
                    print(
                        "✓ first response.created"
                    )
                    print(
                        "response_id:",
                        first_response_id,
                    )

                elif continuation_requested:
                    continuation_started = (
                        True
                    )

                    continuation_response_id = (
                        response_id
                    )

                    print()
                    print(
                        "✓ continuation response.created"
                    )
                    print(
                        "response_id:",
                        continuation_response_id,
                    )

                continue

            # =================================================
            # CONTINUATION TEXT
            # =================================================

            if (
                continuation_started
                and inner_type
                == "response.output_text.delta"
            ):
                delta = (
                    inner.get(
                        "delta"
                    )
                )

                if isinstance(
                    delta,
                    str,
                ):
                    continuation_text_parts.append(
                        delta
                    )

                continue

            # =================================================
            # RESPONSE COMPLETED
            # =================================================

            if (
                inner_type
                == "response.completed"
            ):

                # ---------------------------------------------
                # FIRST DELEGATED RESPONSE
                # ---------------------------------------------

                if (
                    not first_response_completed
                ):
                    first_response_completed = (
                        True
                    )

                    print()
                    print(
                        "✓ first response.completed"
                    )

                    if admission is None:
                        raise RuntimeError(
                            "Responses completed without an admitted Jarvis action"
                        )

                    if call_id is None:
                        raise RuntimeError(
                            "No call_id was bound"
                        )

                    # =========================================
                    # REAL ACTION ENGINE RESULT
                    # =========================================

                    print()
                    print(
                        "Waiting for REAL Jarvis Action Engine..."
                    )

                    jarvis_terminal = (
                        wait_for_jarvis_terminal(
                            coordinator,
                            call_id,
                        )
                    )

                    print(
                        "✓ Jarvis terminal result ready"
                    )

                    print(
                        "Jarvis status:",
                        jarvis_terminal.status,
                    )

                    print(
                        "Jarvis request_id:",
                        jarvis_terminal.request_id,
                    )

                    print(
                        "Runtime calls:",
                        len(
                            runtime_calls
                        ),
                    )

                    if (
                        jarvis_terminal.status
                        != "completed"
                    ):
                        raise RuntimeError(
                            "D2 Jarvis action did not complete"
                        )

                    # The coordinator called DeferredTerminalWriter,
                    # not the network.
                    if (
                        deferred_writer.output
                        is not jarvis_terminal
                    ):
                        raise RuntimeError(
                            "Deferred writer did not capture Jarvis output"
                        )

                    if (
                        function_output_sent
                    ):
                        raise RuntimeError(
                            "Jarvis output was sent before protocol gate"
                        )

                    # =========================================
                    # OPEN LIVE PROTOCOL GATE
                    # =========================================

                    print()
                    print(
                        "Updating backend tool_choice -> none..."
                    )

                    connection.session.update(
                        event_id=(
                            POST_TOOL_UPDATE_EVENT_ID
                        ),
                        session={
                            "delegation": {
                                "type": (
                                    "responses"
                                ),
                                "responses": {
                                    "tool_choice": (
                                        "none"
                                    ),
                                    "instructions": (
                                        "Jarvis has completed the requested "
                                        "action. Do not call tools. "
                                        "Summarize the result in one short "
                                        "factual sentence."
                                    ),
                                },
                            },
                        },
                    )

                    backend_update_requested = (
                        True
                    )

                    continue

                # ---------------------------------------------
                # CONTINUATION RESPONSE
                # ---------------------------------------------

                if continuation_started:
                    continuation_completed = (
                        True
                    )

                    print()
                    print(
                        "✓ continuation response.completed"
                    )

                    break

finally:
    worker.shutdown(
        wait=True,
        timeout=3,
    )


# ============================================================
# RESULT
# ============================================================

continuation_text = "".join(
    continuation_text_parts
).strip()


print()
print(
    "============================================================"
)
print(
    "15.4D2 RESULT"
)
print(
    "============================================================"
)

print(
    "session_started:",
    session_started,
)

print(
    "delegation_id:",
    delegation_id,
)

print(
    "first_response_id:",
    first_response_id,
)

print(
    "first_response_completed:",
    first_response_completed,
)

print(
    "action_admitted:",
    admission is not None,
)

print(
    "call_id:",
    call_id,
)

print(
    "jarvis_status:",
    (
        jarvis_terminal.status
        if jarvis_terminal is not None
        else None
    ),
)

print(
    "planner_calls:",
    len(
        planner.calls
    ),
)

print(
    "runtime_calls:",
    len(
        runtime_calls
    ),
)

print(
    "deferred_writer_calls:",
    deferred_writer.deliver_calls,
)

print(
    "backend_update_acknowledged:",
    backend_update_acknowledged,
)

print(
    "function_output_sent:",
    function_output_sent,
)

print(
    "continuation_started:",
    continuation_started,
)

print(
    "continuation_completed:",
    continuation_completed,
)

print(
    "continuation_text:",
    repr(
        continuation_text
    ),
)


# ============================================================
# ASSERTIONS
# ============================================================

checks = {
    "session_started":
        session_started,

    "delegation_id":
        isinstance(
            delegation_id,
            str,
        )
        and bool(
            delegation_id
        ),

    "action_admitted":
        admission is not None,

    "first_response_completed":
        first_response_completed,

    "jarvis_terminal":
        isinstance(
            jarvis_terminal,
            TerminalFunctionOutput,
        ),

    "jarvis_completed":
        (
            jarvis_terminal is not None
            and jarvis_terminal.status
            == "completed"
        ),

    "planner_once":
        len(
            planner.calls
        )
        == 1,

    "runtime_once":
        len(
            runtime_calls
        )
        == 1,

    "deferred_once":
        deferred_writer.deliver_calls
        == 1,

    "backend_update_acknowledged":
        backend_update_acknowledged,

    "function_output_sent":
        function_output_sent,

    "continuation_started":
        continuation_started,

    "continuation_completed":
        continuation_completed,
}


failed = [
    name
    for name, passed
    in checks.items()
    if not passed
]


if failed:
    raise RuntimeError(
        "D2 checks failed: "
        + ", ".join(
            failed
        )
    )


print()
print(
    "SUCCESS:"
)

print(
    "REAL OpenAI Live -> REAL Jarvis Action Engine -> "
    "REAL OpenAI Live completed end-to-end."
)
