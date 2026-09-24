from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from core.contracts import (
    ActionRequest,
    ActionStatus,
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
from integrations.local_browser_approval import (
    approval_is_current,
    preview_browser_approval_v2,
)
from integrations.openai_live import PendingPermissionUpdate


def build_system(
    *,
    browser_handler=None,
):
    search_calls = []
    browser_calls = []

    def search(arguments):
        search_calls.append(
            dict(arguments)
        )

        return {
            "best_result_url":
                "https://example.com/original"
        }

    if browser_handler is None:
        def browser_handler(arguments):
            browser_calls.append(
                dict(arguments)
            )

            return {
                "opened_url":
                    arguments["url"]
            }

    registry = CapabilityRegistry()

    registry.register(
        CapabilitySpec(
            "web_search",
            "Search",
        )
    )

    registry.register(
        CapabilitySpec(
            "browser",
            "Browser",
        )
    )

    runtimes = CapabilityRuntimeRegistry()

    runtimes.register(
        "web_search",
        search,
    )

    runtimes.register(
        "browser",
        browser_handler,
    )

    permissions = PermissionEngine()

    executor = Executor(
        validator=PlanValidator(
            CapabilityResolver(
                registry
            )
        ),
        permission_engine=permissions,
        runtime_registry=runtimes,
    )

    request = ActionRequest(
        "Search and open",
        "Search and open",
    )

    plan = ExecutionPlan(
        request.request_id,
        (
            ExecutionStep(
                1,
                "Search",
                "web_search",
                arguments={
                    "query":
                        ExecutionArgument.literal(
                            "example"
                        )
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
            ExecutionStep(
                2,
                "Open result",
                "browser",
                arguments={
                    "url":
                        ExecutionArgument.step_output(
                            1,
                            "best_result_url",
                        )
                },
                risk=RiskLevel.LOW,
                permission=(
                    PermissionMode.AUTOMATIC
                ),
            ),
        ),
        RiskLevel.LOW,
    )

    class Planner:
        calls = 0

        def plan(
            self,
            incoming,
        ):
            self.calls += 1
            return plan

    planner = Planner()

    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )

    return SimpleNamespace(
        request=request,
        plan=plan,
        planner=planner,
        permissions=permissions,
        executor=executor,
        orchestrator=orchestrator,
        search_calls=search_calls,
        browser_calls=browser_calls,
    )


def projection(
    request_id,
    *,
    call_id="call-1",
):
    return PendingPermissionUpdate(
        delegation_id="delegation-1",
        call_id=call_id,
        request_id=str(request_id),
        confirmation_steps=(2,),
        pending_confirmation_steps=(2,),
        message="Permission required",
    )


def session_for(
    request_id,
    pending,
):
    return SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            action=SimpleNamespace(
                request_id=str(request_id),
                call_id=pending.call_id,
                pending_permission=pending,
            )
        )
    )


def test_executor_rejects_checkpoint_resolved_target_mismatch():
    setup = build_system()

    waiting = setup.executor.execute(
        setup.plan
    )

    assert waiting.checkpoint is not None

    forged = replace(
        waiting.checkpoint,
        resolved_arguments={
            "url":
                "https://example.com/evil"
        },
    )

    result = setup.executor.execute(
        setup.plan,
        confirmed_steps=frozenset({2}),
        checkpoint=forged,
    )

    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert setup.browser_calls == []


def test_executor_rejects_checkpoint_output_result_mismatch():
    setup = build_system()

    waiting = setup.executor.execute(
        setup.plan
    )

    assert waiting.checkpoint is not None

    forged = replace(
        waiting.checkpoint,
        outputs={
            1: {
                "best_result_url":
                    "https://example.com/evil"
            }
        },
        resolved_arguments={
            "url":
                "https://example.com/evil"
        },
    )

    result = setup.executor.execute(
        setup.plan,
        confirmed_steps=frozenset({2}),
        checkpoint=forged,
    )

    assert (
        result.status
        == ActionStatus.FAILED
    )

    assert setup.browser_calls == []


def test_private_checkpoint_corruption_cannot_be_previewed():
    setup = build_system()

    waiting = setup.orchestrator.run(
        setup.request
    )

    assert waiting.waiting_for_permission

    private_checkpoint = (
        setup.orchestrator
        ._pending[
            setup.request.request_id
        ]
        .checkpoint
    )

    assert private_checkpoint is not None

    private_checkpoint.resolved_arguments[
        "url"
    ] = "https://example.com/evil"

    preview = (
        setup.orchestrator
        .preview_checkpoint_browser(
            setup.request.request_id,
            step_number=2,
        )
    )

    assert preview is None
    assert setup.browser_calls == []


def test_checkpoint_resume_rechecks_policy_before_browser(
    monkeypatch,
):
    setup = build_system()

    waiting = setup.orchestrator.run(
        setup.request
    )

    assert waiting.waiting_for_permission
    assert len(setup.search_calls) == 1

    original_evaluate = (
        setup.permissions.evaluate
    )

    def forbid(plan):
        report = original_evaluate(
            plan
        )

        return replace(
            report,
            decisions=tuple(
                replace(
                    decision,
                    effective_permission=(
                        PermissionMode.FORBIDDEN
                    ),
                )
                if decision.step_number == 2
                else decision
                for decision
                in report.decisions
            ),
        )

    monkeypatch.setattr(
        setup.permissions,
        "evaluate",
        forbid,
    )

    result = setup.orchestrator.resume(
        setup.request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert (
        result.status
        == ActionStatus.BLOCKED
    )

    assert len(setup.search_calls) == 1
    assert setup.browser_calls == []


def test_stale_checkpoint_approval_fails_after_request_consumed():
    setup = build_system()

    waiting = setup.orchestrator.run(
        setup.request
    )

    assert waiting.waiting_for_permission

    pending = projection(
        setup.request.request_id
    )

    approval = (
        preview_browser_approval_v2(
            setup.orchestrator,
            pending,
        )
    )

    assert approval is not None

    session = session_for(
        setup.request.request_id,
        pending,
    )

    assert approval_is_current(
        setup.orchestrator,
        session,
        approval,
    )

    completed = setup.orchestrator.resume(
        setup.request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert completed.completed

    assert not approval_is_current(
        setup.orchestrator,
        session,
        approval,
    )


def test_wrong_call_id_cannot_authorize_checkpoint():
    setup = build_system()

    setup.orchestrator.run(
        setup.request
    )

    pending = projection(
        setup.request.request_id
    )

    approval = (
        preview_browser_approval_v2(
            setup.orchestrator,
            pending,
        )
    )

    assert approval is not None

    wrong = projection(
        setup.request.request_id,
        call_id="wrong-call",
    )

    session = session_for(
        setup.request.request_id,
        wrong,
    )

    assert not approval_is_current(
        setup.orchestrator,
        session,
        approval,
    )

    assert setup.browser_calls == []


def test_concurrent_checkpoint_resumes_execute_browser_once():
    entered = Event()
    release = Event()
    browser_calls = []

    def browser(arguments):
        browser_calls.append(
            dict(arguments)
        )

        entered.set()

        assert release.wait(5)

        return {
            "opened_url":
                arguments["url"]
        }

    setup = build_system(
        browser_handler=browser,
    )

    waiting = setup.orchestrator.run(
        setup.request
    )

    assert waiting.waiting_for_permission
    assert len(setup.search_calls) == 1

    def resume():
        return setup.orchestrator.resume(
            setup.request.request_id,
            confirmed_steps=frozenset({2}),
        )

    with ThreadPoolExecutor(
        max_workers=2
    ) as pool:

        first = pool.submit(
            resume
        )

        try:
            assert entered.wait(5)

            second = pool.submit(
                resume
            )

            second_result = (
                second.result(
                    timeout=5
                )
            )

            assert (
                second_result.status
                == ActionStatus.FAILED
            )

        finally:
            release.set()

        first_result = first.result(
            timeout=5
        )

    assert first_result.completed

    assert len(setup.search_calls) == 1

    assert browser_calls == [
        {
            "url":
                "https://example.com/original"
        }
    ]


def test_second_resume_after_completion_is_rejected():
    setup = build_system()

    setup.orchestrator.run(
        setup.request
    )

    first = setup.orchestrator.resume(
        setup.request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert first.completed

    second = setup.orchestrator.resume(
        setup.request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert (
        second.status
        == ActionStatus.FAILED
    )

    assert len(setup.search_calls) == 1

    assert setup.browser_calls == [
        {
            "url":
                "https://example.com/original"
        }
    ]
