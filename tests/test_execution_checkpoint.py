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


def build_executor(search, browser):
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("web_search", "Search"))
    registry.register(CapabilitySpec("browser", "Browser"))

    runtimes = CapabilityRuntimeRegistry()
    runtimes.register("web_search", search)
    runtimes.register("browser", browser)

    return Executor(
        validator=PlanValidator(
            CapabilityResolver(registry)
        ),
        permission_engine=PermissionEngine(),
        runtime_registry=runtimes,
    )


def make_plan(request_id):
    return ExecutionPlan(
        request_id,
        (
            ExecutionStep(
                1,
                "Search",
                "web_search",
                arguments={
                    "query":
                        ExecutionArgument.literal("Python docs")
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.AUTOMATIC,
            ),
            ExecutionStep(
                2,
                "Open",
                "browser",
                arguments={
                    "url":
                        ExecutionArgument.step_output(
                            1,
                            "best_result_url",
                        )
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.AUTOMATIC,
            ),
        ),
        RiskLevel.LOW,
    )


def test_dependency_browser_creates_checkpoint():
    search_calls = []
    browser_calls = []

    def search(arguments):
        search_calls.append(dict(arguments))
        return {
            "best_result_url":
                "https://example.com/original"
        }

    def browser(arguments):
        browser_calls.append(dict(arguments))
        return {"opened_url": arguments["url"]}

    report = build_executor(
        search,
        browser,
    ).execute(
        make_plan(uuid4())
    )

    assert report.status == ActionStatus.WAITING_FOR_PERMISSION
    assert search_calls == [{"query": "Python docs"}]
    assert browser_calls == []
    assert len(report.step_results) == 1
    assert report.checkpoint is not None
    assert report.checkpoint.next_step_number == 2
    assert report.checkpoint.resolved_arguments["url"] == (
        "https://example.com/original"
    )
    assert report.pending_confirmation_steps == (2,)


def test_resume_uses_frozen_url_without_replaying_search():
    search_calls = []
    browser_calls = []

    target = {
        "url": "https://example.com/original"
    }

    def search(arguments):
        search_calls.append(dict(arguments))
        return {
            "best_result_url": target["url"]
        }

    def browser(arguments):
        browser_calls.append(dict(arguments))
        return {"opened_url": arguments["url"]}

    request = ActionRequest(
        "Search and open",
        "Search and open",
    )

    plan = make_plan(request.request_id)

    class Planner:
        calls = 0

        def plan(self, incoming):
            self.calls += 1
            return plan

    planner = Planner()

    engine = Orchestrator(
        planner=planner,
        executor=build_executor(
            search,
            browser,
        ),
    )

    waiting = engine.run(request)

    assert waiting.waiting_for_permission

    presented = (
        waiting.execution_report
        .checkpoint
        .resolved_arguments["url"]
    )

    target["url"] = "https://example.com/changed"

    result = engine.resume(
        request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert result.completed
    assert planner.calls == 1
    assert search_calls == [{"query": "Python docs"}]
    assert browser_calls == [{"url": presented}]
    assert presented == "https://example.com/original"


def test_external_checkpoint_mutation_does_not_change_private_target():
    browser_calls = []

    def search(arguments):
        return {
            "best_result_url":
                "https://example.com/original"
        }

    def browser(arguments):
        browser_calls.append(dict(arguments))
        return {"opened_url": arguments["url"]}

    request = ActionRequest(
        "Search and open",
        "Search and open",
    )

    plan = make_plan(request.request_id)

    class Planner:
        def plan(self, incoming):
            return plan

    engine = Orchestrator(
        planner=Planner(),
        executor=build_executor(search, browser),
    )

    waiting = engine.run(request)

    exposed = waiting.execution_report.checkpoint
    assert exposed is not None

    exposed.resolved_arguments["url"] = (
        "https://example.com/evil"
    )

    result = engine.resume(
        request.request_id,
        confirmed_steps=frozenset({2}),
    )

    assert result.completed
    assert browser_calls == [
        {
            "url":
                "https://example.com/original"
        }
    ]


def test_literal_browser_keeps_existing_preflight():
    browser_calls = []

    def search(arguments):
        raise AssertionError("search must not execute")

    def browser(arguments):
        browser_calls.append(dict(arguments))
        return {"opened_url": arguments["url"]}

    plan = ExecutionPlan(
        uuid4(),
        (
            ExecutionStep(
                1,
                "Open",
                "browser",
                arguments={
                    "url":
                        ExecutionArgument.literal(
                            "https://example.com"
                        )
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.AUTOMATIC,
            ),
        ),
        RiskLevel.LOW,
    )

    report = build_executor(
        search,
        browser,
    ).execute(plan)

    assert report.status == ActionStatus.WAITING_FOR_PERMISSION
    assert report.step_results == ()
    assert report.checkpoint is None
    assert browser_calls == []


def test_forbidden_browser_still_blocks_before_search():
    search_calls = []

    def search(arguments):
        search_calls.append(dict(arguments))
        return {
            "best_result_url":
                "https://example.com"
        }

    def browser(arguments):
        raise AssertionError("browser must not execute")

    executor = build_executor(search, browser)

    plan = ExecutionPlan(
        uuid4(),
        (
            ExecutionStep(
                1,
                "Search",
                "web_search",
                arguments={
                    "query":
                        ExecutionArgument.literal("example")
                },
                risk=RiskLevel.LOW,
                permission=PermissionMode.AUTOMATIC,
            ),
            ExecutionStep(
                2,
                "Forbidden browser",
                "browser",
                arguments={
                    "url":
                        ExecutionArgument.step_output(
                            1,
                            "best_result_url",
                        )
                },
                risk=RiskLevel.CRITICAL,
                permission=PermissionMode.AUTOMATIC,
            ),
        ),
        RiskLevel.CRITICAL,
    )

    report = executor.execute(plan)

    assert report.status == ActionStatus.BLOCKED
    assert search_calls == []
