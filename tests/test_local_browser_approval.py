from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.contracts import (
    ActionRequest,
    ActionStatus,
    CapabilitySpec,
    ExecutionArgument,
    ExecutionPlan,
    ExecutionStep,
    RiskLevel,
)
from core.executor import Executor
from core.orchestrator import Orchestrator
from core.permissions import PermissionEngine
from core.plan_validator import PlanValidator
from core.registry import CapabilityRegistry
from core.resolver import CapabilityResolver
from core.runtime import CapabilityRuntimeRegistry
from integrations.local_browser_approval import (
    approval_is_current,
    format_browser_approval,
    preview_browser_approval,
)
from integrations.openai_live import PendingPermissionUpdate


def build_waiting_browser(url="https://docs.python.org/3/"):
    request = ActionRequest(
        goal="Abrir la URL",
        raw_input="Abre esta página",
    )

    calls = []
    planner_calls = []

    class StaticPlanner:
        def plan(self, received):
            planner_calls.append(received.request_id)

            return ExecutionPlan(
                request_id=received.request_id,
                steps=(
                    ExecutionStep(
                        step_number=1,
                        description="Abrir página",
                        capability="browser",
                        arguments={
                            "url": ExecutionArgument.literal(url),
                        },
                        risk=RiskLevel.LOW,
                    ),
                ),
                overall_risk=RiskLevel.LOW,
            )

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            name="browser",
            description="Abrir páginas",
        )
    )

    runtimes = CapabilityRuntimeRegistry()

    def browser_handler(arguments):
        calls.append(dict(arguments))
        return {
            "opened": True,
            "opened_url": arguments["url"],
        }

    runtimes.register("browser", browser_handler)

    executor = Executor(
        validator=PlanValidator(
            CapabilityResolver(registry)
        ),
        permission_engine=PermissionEngine(),
        runtime_registry=runtimes,
    )

    orchestrator = Orchestrator(
        planner=StaticPlanner(),
        executor=executor,
    )

    waiting = orchestrator.run(request)

    assert waiting.status == ActionStatus.WAITING_FOR_PERMISSION
    assert calls == []

    return orchestrator, request, calls, planner_calls


def make_projection(request_id, call_id="call-1"):
    return PendingPermissionUpdate(
        delegation_id="delegation-1",
        call_id=call_id,
        request_id=str(request_id),
        confirmation_steps=(1,),
        pending_confirmation_steps=(1,),
        message="Permiso requerido",
    )


def test_preview_shows_exact_url_without_opening():
    engine, request, calls, planner_calls = build_waiting_browser()

    preview = preview_browser_approval(
        engine,
        make_projection(request.request_id),
    )

    assert preview is not None
    assert preview.step_number == 1
    assert preview.url == "https://docs.python.org/3/"
    assert calls == []
    assert len(planner_calls) == 1

    display = format_browser_approval(preview)
    assert "docs.python.org" in display
    assert "https://docs.python.org/3/" in display
    assert "AUTORIZAR" in display


def test_confirmation_resumes_same_plan_without_replanning():
    engine, request, calls, planner_calls = build_waiting_browser()

    preview = preview_browser_approval(
        engine,
        make_projection(request.request_id),
    )

    pending = make_projection(request.request_id)

    session = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            action=SimpleNamespace(
                request_id=str(request.request_id),
                call_id=pending.call_id,
                pending_permission=pending,
            )
        )
    )

    assert approval_is_current(engine, session, preview)

    result = engine.resume(
        request.request_id,
        confirmed_steps=frozenset({1}),
    )

    assert result.status == ActionStatus.COMPLETED
    assert len(planner_calls) == 1
    assert calls == [
        {"url": "https://docs.python.org/3/"}
    ]

    assert engine.preview_single_browser(request.request_id) is None


def test_stale_call_cannot_be_approved():
    engine, request, calls, _ = build_waiting_browser()

    preview = preview_browser_approval(
        engine,
        make_projection(request.request_id),
    )

    wrong = make_projection(
        request.request_id,
        call_id="different-call",
    )

    session = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            action=SimpleNamespace(
                request_id=str(request.request_id),
                call_id=wrong.call_id,
                pending_permission=wrong,
            )
        )
    )

    assert not approval_is_current(engine, session, preview)
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret.txt",
        "javascript:alert(1)",
        "https://user:password@example.com/",
        "https://example.com/\\x1b[31m",
    ],
)
def test_preview_rejects_unsafe_or_ambiguous_url(url):
    engine, request, calls, _ = build_waiting_browser(url)

    assert preview_browser_approval(
        engine,
        make_projection(request.request_id),
    ) is None

    assert calls == []


def test_unknown_request_cannot_be_previewed():
    engine, _, calls, _ = build_waiting_browser()

    assert engine.preview_single_browser(uuid4()) is None
    assert calls == []


def test_preview_rejects_actual_ansi_escape():
    # chr(27) produces a real ESC control character.
    # This is different from the literal characters \\x1b.
    url = (
        "https://example.com/"
        + chr(27)
        + "[31m"
    )

    engine, request, calls, _ = (
        build_waiting_browser(url)
    )

    assert preview_browser_approval(
        engine,
        make_projection(request.request_id),
    ) is None

    assert calls == []
