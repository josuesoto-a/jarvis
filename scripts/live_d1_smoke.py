"""Real OpenAI Live / Responses protocol smoke test.

Task 15.4D1.1

This test verifies the real protocol boundary:

Live WebSocket
    -> Responses delegation
    -> perform_action function call
    -> client function_call_output
    -> backend continuation
    -> completed handoff

It deliberately does NOT run the Jarvis ActionWorker yet.

Important lifecycle rule:

1. Receive the completed function_call item.
2. Wait for the first delegated Response itself to complete.
3. Change backend tool_choice so the continuation cannot call the tool again.
4. Send the function result.
5. Explicitly continue the delegated Response.
6. Wait for the continuation Response to complete.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from integrations.openai_live import (
    parse_perform_action_call,
    perform_action_argument_schema,
)


# ============================================================
# CONFIG
# ============================================================

ENV_PATH = Path.cwd() / ".env"

INITIAL_RESPONSE_EVENT_ID = "d1_initial_backend"
POST_TOOL_UPDATE_EVENT_ID = "d1_disable_tools"
TOOL_RESULT_EVENT_ID = "d1_tool_result"
CONTINUE_EVENT_ID = "d1_continue_backend"

MAX_EVENTS = 300


# ============================================================
# HELPERS
# ============================================================

def event_to_dict(event):
    if isinstance(event, dict):
        return event

    model_dump = getattr(
        event,
        "model_dump",
        None,
    )

    if callable(model_dump):
        return model_dump(
            mode="json"
        )

    to_dict = getattr(
        event,
        "to_dict",
        None,
    )

    if callable(to_dict):
        return to_dict()

    raise TypeError(
        f"Cannot convert event type: {type(event)!r}"
    )


def response_id_from_created(
    inner: dict,
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
    "TASK 15.4D1.1 - REAL LIVE HANDOFF LIFECYCLE"
)
print(
    "============================================================"
)

load_dotenv(
    dotenv_path=ENV_PATH
)

api_key = os.getenv(
    "OPENAI_API_KEY"
)

if not api_key:
    raise RuntimeError(
        f"OPENAI_API_KEY was not loaded from {ENV_PATH}"
    )

print()
print(
    "Credentials: loaded"
)
print(
    "API key: NOT DISPLAYED"
)


# ============================================================
# CLIENT
# ============================================================

client = OpenAI(
    api_key=api_key
)


# ============================================================
# SESSION
# ============================================================

session = {
    "model": "gpt-live-1",

    "instructions": (
        "This is a protocol smoke test. "
        "Backend delegated work should be used for the requested action."
    ),

    "store": False,

    "input": [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Run the Jarvis protocol smoke test action. "
                        "Verify that a custom function call can travel "
                        "through the Live Responses backend."
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
                "This is a Jarvis protocol smoke test. "
                "Call perform_action exactly once using the user's "
                "request as both goal and raw_input. "
                "Use an empty object for context. "
                "After the application returns the function result, "
                "return a very short factual completion result."
            ),

            "tools": [
                {
                    "type": "function",
                    "name": "perform_action",
                    "description": (
                        "Submit one user-requested action to the "
                        "local Jarvis Action Engine."
                    ),
                    "parameters": (
                        perform_action_argument_schema()
                    ),
                    "strict": False,
                }
            ],

            # Force the FIRST backend response to exercise our function.
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

call_id = None
function_call_seen = False
parsed_action_call = None

backend_update_requested = False
backend_update_acknowledged = False

function_output_sent = False

continuation_requested = False
continuation_response_id = None
continuation_started = False
continuation_completed = False

continuation_text_parts: list[str] = []


# ============================================================
# CONNECTION
# ============================================================

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
        event = connection.recv()

        payload = event_to_dict(
            event
        )

        event_type = payload.get(
            "type"
        )

        # ----------------------------------------------------
        # ERROR
        # ----------------------------------------------------

        if event_type == "error":
            print_server_error(
                payload
            )

            raise RuntimeError(
                "Live server returned an error"
            )

        # ----------------------------------------------------
        # SESSION START
        # ----------------------------------------------------

        if event_type == "session.started":
            session_started = True

            print()
            print(
                "✓ session.started"
            )

            print(
                "Requesting initial Responses execution..."
            )

            connection.response.create(
                event_id=(
                    INITIAL_RESPONSE_EVENT_ID
                )
            )

            continue

        # ----------------------------------------------------
        # DELEGATION
        # ----------------------------------------------------

        if (
            event_type
            == "session.delegation.created"
        ):
            delegation = payload.get(
                "delegation"
            )

            if isinstance(
                delegation,
                dict,
            ):
                value = delegation.get(
                    "id"
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

        # ----------------------------------------------------
        # SESSION UPDATE ACK
        # ----------------------------------------------------

        if event_type == "session.updated":
            if not backend_update_requested:
                continue

            backend_update_acknowledged = True

            print()
            print(
                "✓ session.updated"
            )

            print(
                "Backend tool_choice is now none."
            )

            if parsed_action_call is None:
                raise RuntimeError(
                    "No validated function call is available"
                )

            # -----------------------------------------------
            # NOW SEND THE TOOL RESULT
            # -----------------------------------------------

            smoke_result = {
                "status": "completed",
                "message": (
                    "Jarvis Live protocol smoke test succeeded."
                ),
                "mode": (
                    "15.4D1.1-protocol-only"
                ),
            }

            print()
            print(
                "Sending function_call_output..."
            )

            connection.response.item.create(
                event_id=(
                    TOOL_RESULT_EVENT_ID
                ),
                item={
                    "type": (
                        "function_call_output"
                    ),
                    "call_id": (
                        parsed_action_call.call_id
                    ),
                    "output": json.dumps(
                        smoke_result,
                        ensure_ascii=False,
                        separators=(
                            ",",
                            ":",
                        ),
                    ),
                },
            )

            function_output_sent = True

            print(
                "✓ function_call_output sent"
            )

            # -----------------------------------------------
            # EXPLICITLY CONTINUE THE BACKEND
            # -----------------------------------------------

            print(
                "Requesting backend continuation..."
            )

            connection.response.create(
                event_id=(
                    CONTINUE_EVENT_ID
                )
            )

            continuation_requested = True

            continue

        # ----------------------------------------------------
        # ONLY RESPONSES EVENTS BELOW
        # ----------------------------------------------------

        if event_type != "response.event":
            continue

        outer_delegation_id = payload.get(
            "delegation_id"
        )

        if isinstance(
            outer_delegation_id,
            str,
        ):
            if delegation_id is None:
                delegation_id = (
                    outer_delegation_id
                )

            elif (
                delegation_id
                != outer_delegation_id
            ):
                raise RuntimeError(
                    "delegation_id changed unexpectedly"
                )

        inner = payload.get(
            "event"
        )

        if not isinstance(
            inner,
            dict,
        ):
            continue

        inner_type = inner.get(
            "type"
        )

        # ----------------------------------------------------
        # RESPONSE CREATED
        # ----------------------------------------------------

        if inner_type == "response.created":
            current_response_id = (
                response_id_from_created(
                    inner
                )
            )

            if first_response_id is None:
                first_response_id = (
                    current_response_id
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
                    current_response_id
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

        # ----------------------------------------------------
        # FUNCTION CALL
        # ----------------------------------------------------

        if (
            inner_type
            == "response.output_item.done"
        ):
            item = inner.get(
                "item"
            )

            if not isinstance(
                item,
                dict,
            ):
                continue

            if (
                item.get(
                    "type"
                )
                != "function_call"
            ):
                continue

            if function_call_seen:
                raise RuntimeError(
                    "More than one function call was received"
                )

            current_call_id = item.get(
                "call_id"
            )

            name = item.get(
                "name"
            )

            arguments = item.get(
                "arguments"
            )

            if not isinstance(
                current_call_id,
                str,
            ):
                raise RuntimeError(
                    "function_call has no valid call_id"
                )

            if not isinstance(
                name,
                str,
            ):
                raise RuntimeError(
                    "function_call has no valid name"
                )

            if not isinstance(
                arguments,
                str,
            ):
                raise RuntimeError(
                    "function_call arguments are not JSON text"
                )

            if not isinstance(
                delegation_id,
                str,
            ):
                raise RuntimeError(
                    "No delegation_id is available"
                )

            # -----------------------------------------------
            # USE OUR REAL 15.4A VALIDATOR
            # -----------------------------------------------

            parsed_action_call = (
                parse_perform_action_call(
                    delegation_id=(
                        delegation_id
                    ),
                    call_id=(
                        current_call_id
                    ),
                    name=name,
                    arguments=arguments,
                )
            )

            call_id = (
                parsed_action_call.call_id
            )

            function_call_seen = True

            print()
            print(
                "============================================================"
            )
            print(
                "REAL perform_action RECEIVED + VALIDATED"
            )
            print(
                "============================================================"
            )

            print(
                "delegation_id:",
                parsed_action_call.delegation_id,
            )

            print(
                "call_id:",
                parsed_action_call.call_id,
            )

            print(
                "arguments:"
            )

            print(
                json.dumps(
                    parsed_action_call.arguments(),
                    ensure_ascii=False,
                    indent=2,
                )
            )

            print()
            print(
                "Waiting for FIRST response.completed "
                "before continuing..."
            )

            # Important:
            # DO NOT send the tool output yet.
            # DO NOT call response.create yet.

            continue

        # ----------------------------------------------------
        # CONTINUATION TEXT
        # ----------------------------------------------------

        if (
            continuation_started
            and inner_type
            == "response.output_text.delta"
        ):
            delta = inner.get(
                "delta"
            )

            if isinstance(
                delta,
                str,
            ):
                continuation_text_parts.append(
                    delta
                )

            continue

        # ----------------------------------------------------
        # RESPONSE COMPLETED
        # ----------------------------------------------------

        if inner_type == "response.completed":

            # -----------------------------------------------
            # FIRST RESPONSE COMPLETED
            # -----------------------------------------------

            if not first_response_completed:
                first_response_completed = True

                print()
                print(
                    "✓ first response.completed"
                )

                if not function_call_seen:
                    raise RuntimeError(
                        "Initial Response completed without perform_action"
                    )

                if parsed_action_call is None:
                    raise RuntimeError(
                        "Function call was not validated"
                    )

                # -------------------------------------------
                # IMPORTANT:
                # The named tool choice was useful to force
                # our FIRST call.
                #
                # It must not remain forced while asking the
                # backend to finish after the tool result.
                # -------------------------------------------

                print()
                print(
                    "Updating backend:"
                )
                print(
                    "tool_choice: perform_action -> none"
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
                                    "The requested Jarvis action "
                                    "has now returned its result. "
                                    "Do not call any tools. "
                                    "Return a short factual completion "
                                    "result so the Live conversation "
                                    "can continue."
                                ),
                            },
                        },
                    },
                )

                backend_update_requested = True

                # Wait for session.updated before adding
                # the output and continuing.
                continue

            # -----------------------------------------------
            # CONTINUATION RESPONSE COMPLETED
            # -----------------------------------------------

            if continuation_started:
                continuation_completed = True

                print()
                print(
                    "✓ continuation response.completed"
                )

                break


# ============================================================
# RESULTS
# ============================================================

continuation_text = "".join(
    continuation_text_parts
).strip()


print()
print(
    "============================================================"
)
print(
    "15.4D1.1 RESULT"
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
    "function_call_seen:",
    function_call_seen,
)

print(
    "call_id:",
    call_id,
)

print(
    "first_response_completed:",
    first_response_completed,
)

print(
    "backend_update_requested:",
    backend_update_requested,
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
    "continuation_response_id:",
    continuation_response_id,
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

    "function_call_seen":
        function_call_seen,

    "first_response_completed":
        first_response_completed,

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
        "D1.1 checks failed: "
        + ", ".join(
            failed
        )
    )


print()
print(
    "SUCCESS:"
)
print(
    "Real Live -> Responses -> perform_action -> "
    "function output -> continuation completed."
)
