"""Task 15.4D3: real OpenAI Live + Jarvis production bootstrap.

This smoke test uses:

REAL:
- OpenAI Live WebSocket
- Responses delegation
- perform_action function call
- ResponsesEventAdapter
- LiveActionCoordinator
- ActionWorker
- build_default_jarvis()
- production Planner
- production Executor
- production PlanValidator
- production PermissionEngine
- production CapabilityRegistry
- production CapabilityRuntimeRegistry
- production web_search capability
- real function_call_output back to OpenAI
- real delegated continuation

SAFETY GUARD:
- browser remains registered by the production bootstrap
- browser_opener is replaced with a guard that refuses to open a window

No microphone, audio playback, terminal, filesystem, Codex, or computer-use
capability is involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(ROOT),
    )


from dotenv import load_dotenv
from openai import OpenAI

from core.action_worker import ActionWorker
from core.bootstrap import build_default_jarvis

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

INITIAL_RESPONSE_EVENT_ID = "d3_initial_response"
POST_TOOL_UPDATE_EVENT_ID = "d3_disable_tools"
TOOL_RESULT_EVENT_ID = "d3_tool_result"
CONTINUE_EVENT_ID = "d3_continue"

MAX_EVENTS = 400
WORKER_TIMEOUT = 30.0


# ============================================================
# DEFERRED TERMINAL WRITER
# ============================================================

class DeferredTerminalWriter:
    """Hold Jarvis terminal output until the Live protocol gate opens."""

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
# SAFE BROWSER GUARD
# ============================================================

browser_guard_calls: list[str] = []


def guarded_browser_opener(
    url: str,
) -> bool:
    """Prevent D3 from creating a real browser-side effect."""

    browser_guard_calls.append(
        url
    )

    raise RuntimeError(
        "D3 smoke test forbids browser opening; "
        "the requested action must use web_search only."
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
    call_id: str,
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
                0.01
            )
            continue

        if isinstance(
            projection,
            PendingPermissionUpdate,
        ):
            raise RuntimeError(
                "D3 read-only web search unexpectedly "
                "requires human confirmation"
            )

        if isinstance(
            projection,
            TerminalFunctionOutput,
        ):
            return projection

        raise RuntimeError(
            "Unexpected Jarvis projection: "
            f"{type(projection)!r}"
        )

    raise TimeoutError(
        "Production Jarvis did not return a terminal result"
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
# START
# ============================================================

print(
    "============================================================"
)
print(
    "TASK 15.4D3 - REAL LIVE + PRODUCTION JARVIS BOOTSTRAP"
)
print(
    "============================================================"
)


# ============================================================
# CREDENTIALS
# ============================================================

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
# SHARED OPENAI CLIENT
# ============================================================

client = OpenAI(
    api_key=api_key
)


# ============================================================
# PRODUCTION JARVIS APPLICATION
# ============================================================

print()
print(
    "Building production Jarvis bootstrap..."
)

app = build_default_jarvis(
    client=client,
    planner_model="gpt-5.6-luna",
    web_search_model="gpt-5.6-luna",
    browser_opener=guarded_browser_opener,
)

print(
    "✓ production bootstrap built"
)

print(
    "Capabilities:",
    app.capability_registry.names(),
)

print(
    "Runtimes:",
    app.runtime_registry.names(),
)


# ============================================================
# ACTION WORKER + LIVE ADAPTER
# ============================================================

worker = ActionWorker(
    app.orchestrator.run,
    capacity=8,
)

worker.start()

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
# LIVE SESSION
# ============================================================

session = {
    "model": "gpt-live-1",

    "instructions": (
        "This is a Jarvis production integration test. "
        "Delegate the requested action to the configured "
        "Responses backend."
    ),

    "store": False,

    "input": [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Search the public web for the official OpenAI "
                        "documentation page for GPT-5.6 Luna. "
                        "Find its page title or model name and its URL. "
                        "This is a read-only information search. "
                        "Do not open a browser window."
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
                "Translate the user's request into exactly one "
                "perform_action call for the local Jarvis engine. "
                "Preserve the requirement that this is read-only "
                "and that no browser window should be opened. "
                "Use an empty context object."
            ),

            "tools": [
                {
                    "type": "function",
                    "name": "perform_action",
                    "description": (
                        "Submit one user-requested action "
                        "to the local Jarvis Action Engine."
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

            "max_output_tokens": 192,
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
jarvis_payload = None
step_capabilities = []

backend_update_requested = False
backend_update_acknowledged = False

function_output_sent = False

continuation_requested = False
continuation_started = False
continuation_response_id = None
continuation_completed = False

continuation_text_parts: list[str] = []


# ============================================================
# REAL OPENAI ↔ PRODUCTION JARVIS
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
            # DELEGATION
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
                        delegation_id = value

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
            # SESSION UPDATE ACK
            # =================================================

            if (
                event_type
                == "session.updated"
            ):
                if (
                    not backend_update_requested
                ):
                    continue

                backend_update_acknowledged = True

                print()
                print(
                    "✓ session.updated"
                )

                if jarvis_terminal is None:
                    raise RuntimeError(
                        "Jarvis terminal result is missing"
                    )

                print()
                print(
                    "Sending PRODUCTION Jarvis result to OpenAI..."
                )

                connection.response.item.create(
                    event_id=(
                        TOOL_RESULT_EVENT_ID
                    ),
                    item=(
                        jarvis_terminal.function_call_output_item()
                    ),
                )

                function_output_sent = True

                print(
                    "✓ production Jarvis output sent"
                )

                print(
                    "Requesting delegated continuation..."
                )

                connection.response.create(
                    event_id=(
                        CONTINUE_EVENT_ID
                    )
                )

                continuation_requested = True

                continue

            # =================================================
            # RESPONSES EVENT ONLY
            # =================================================

            if (
                event_type
                != "response.event"
            ):
                continue

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
                        "REAL ACTION ADMITTED TO PRODUCTION JARVIS"
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

                elif (
                    current_admission.call_id
                    != admission.call_id
                ):
                    raise RuntimeError(
                        "Unexpected second perform_action call"
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
                    continuation_started = True

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

                if not first_response_completed:
                    first_response_completed = True

                    print()
                    print(
                        "✓ first response.completed"
                    )

                    if admission is None:
                        raise RuntimeError(
                            "No Jarvis action was admitted"
                        )

                    if call_id is None:
                        raise RuntimeError(
                            "No call_id is available"
                        )

                    print()
                    print(
                        "Waiting for PRODUCTION Jarvis..."
                    )

                    jarvis_terminal = (
                        wait_for_jarvis_terminal(
                            coordinator,
                            call_id,
                        )
                    )

                    print(
                        "✓ production Jarvis terminal result ready"
                    )

                    print(
                        "Jarvis status:",
                        jarvis_terminal.status,
                    )

                    print(
                        "request_id:",
                        jarvis_terminal.request_id,
                    )

                    jarvis_payload = (
                        json.loads(
                            jarvis_terminal.output_json
                        )
                    )

                    print()
                    print(
                        "Jarvis output:"
                    )

                    print(
                        json.dumps(
                            jarvis_payload,
                            ensure_ascii=False,
                            indent=2,
                        )
                    )

                    step_results = (
                        jarvis_payload.get(
                            "step_results",
                            [],
                        )
                    )

                    if isinstance(
                        step_results,
                        list,
                    ):
                        for result in step_results:
                            if not isinstance(
                                result,
                                dict,
                            ):
                                continue

                            capability = (
                                result.get(
                                    "capability"
                                )
                            )

                            if isinstance(
                                capability,
                                str,
                            ):
                                step_capabilities.append(
                                    capability
                                )

                    print()
                    print(
                        "Executed capabilities:",
                        step_capabilities,
                    )

                    if (
                        jarvis_terminal.status
                        != "completed"
                    ):
                        raise RuntimeError(
                            "Production Jarvis did not complete"
                        )

                    if (
                        "web_search"
                        not in step_capabilities
                    ):
                        raise RuntimeError(
                            "Production Planner did not execute web_search"
                        )

                    if browser_guard_calls:
                        raise RuntimeError(
                            "Production Planner attempted a browser side effect"
                        )

                    if (
                        deferred_writer.output
                        is not jarvis_terminal
                    ):
                        raise RuntimeError(
                            "Deferred writer did not capture terminal output"
                        )

                    if function_output_sent:
                        raise RuntimeError(
                            "Output crossed Live protocol gate too early"
                        )

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
                                "type": "responses",
                                "responses": {
                                    "tool_choice": "none",
                                    "instructions": (
                                        "The production Jarvis engine "
                                        "completed the action. "
                                        "Do not call tools. "
                                        "Briefly state that the search "
                                        "completed and summarize the "
                                        "returned result."
                                    ),
                                },
                            },
                        },
                    )

                    backend_update_requested = True

                    continue

                # ---------------------------------------------
                # CONTINUATION
                # ---------------------------------------------

                if continuation_started:
                    continuation_completed = True

                    print()
                    print(
                        "✓ continuation response.completed"
                    )

                    break

finally:
    worker.shutdown(
        wait=True,
        timeout=5,
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
    "15.4D3 RESULT"
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
    "action_admitted:",
    admission is not None,
)

print(
    "first_response_completed:",
    first_response_completed,
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
    "capabilities:",
    app.capability_registry.names(),
)

print(
    "runtimes:",
    app.runtime_registry.names(),
)

print(
    "executed_capabilities:",
    step_capabilities,
)

print(
    "browser_guard_calls:",
    browser_guard_calls,
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

    "delegation":
        isinstance(
            delegation_id,
            str,
        )
        and bool(
            delegation_id
        ),

    "action_admitted":
        admission is not None,

    "initial_response_completed":
        first_response_completed,

    "terminal_output":
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

    "production_registry":
        (
            "web_search"
            in app.capability_registry.names()
            and "browser"
            in app.capability_registry.names()
        ),

    "production_runtimes":
        (
            "web_search"
            in app.runtime_registry.names()
            and "browser"
            in app.runtime_registry.names()
        ),

    "real_web_search":
        "web_search"
        in step_capabilities,

    "no_browser_side_effect":
        browser_guard_calls
        == [],

    "deferred_once":
        deferred_writer.deliver_calls
        == 1,

    "backend_update":
        backend_update_acknowledged,

    "output_sent":
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
        "D3 checks failed: "
        + ", ".join(
            failed
        )
    )


print()
print(
    "SUCCESS:"
)

print(
    "REAL OpenAI Live -> PRODUCTION Jarvis bootstrap -> "
    "REAL web_search -> REAL OpenAI Live completed end-to-end."
)
