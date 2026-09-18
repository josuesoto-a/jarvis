from types import SimpleNamespace

from core.bootstrap import (
    JarvisApplication,
    build_default_jarvis,
)
from core.executor import (
    Executor,
)
from core.orchestrator import (
    Orchestrator,
)
from core.permissions import (
    PermissionEngine,
)
from core.plan_validator import (
    PlanValidator,
)
from core.planner import (
    Planner,
)
from core.registry import (
    CapabilityRegistry,
)
from core.resolver import (
    CapabilityResolver,
)
from core.runtime import (
    CapabilityRuntimeRegistry,
)


# ============================================================
# FAKE OPENAI CLIENT
# ============================================================

class FakeResponses:

    def parse(
        self,
        **kwargs,
    ):
        raise AssertionError(
            "OpenAI should not be called "
            "while merely building Jarvis."
        )


class FakeClient:

    def __init__(
        self,
    ):
        self.responses = (
            FakeResponses()
        )


# ============================================================
# TESTS
# ============================================================

def test_build_returns_jarvis_application():

    app = build_default_jarvis(
        client=FakeClient(),
        browser_opener=lambda url: True,
    )

    assert isinstance(
        app,
        JarvisApplication,
    )


def test_default_capabilities_are_registered():

    app = build_default_jarvis(
        client=FakeClient(),
        browser_opener=lambda url: True,
    )

    assert (
        app
        .capability_registry
        .names()
        == (
            "browser",
            "web_search",
        )
    )


def test_default_runtimes_are_registered():

    app = build_default_jarvis(
        client=FakeClient(),
        browser_opener=lambda url: True,
    )

    assert (
        app
        .runtime_registry
        .names()
        == (
            "browser",
            "web_search",
        )
    )


def test_core_components_are_built():

    app = build_default_jarvis(
        client=FakeClient(),
        browser_opener=lambda url: True,
    )

    assert isinstance(
        app.capability_registry,
        CapabilityRegistry,
    )

    assert isinstance(
        app.runtime_registry,
        CapabilityRuntimeRegistry,
    )

    assert isinstance(
        app.resolver,
        CapabilityResolver,
    )

    assert isinstance(
        app.validator,
        PlanValidator,
    )

    assert isinstance(
        app.permission_engine,
        PermissionEngine,
    )

    assert isinstance(
        app.planner,
        Planner,
    )

    assert isinstance(
        app.executor,
        Executor,
    )

    assert isinstance(
        app.orchestrator,
        Orchestrator,
    )


def test_browser_runtime_uses_injected_opener():

    calls = []

    def opener(
        url,
    ):

        calls.append(
            url
        )

        return True


    app = build_default_jarvis(
        client=FakeClient(),
        browser_opener=opener,
    )


    handler = (
        app
        .runtime_registry
        .get(
            "browser"
        )
    )


    result = handler(
        {
            "url":
                "https://example.com"
        }
    )


    assert calls == [
        "https://example.com"
    ]

    assert result == {
        "opened": True,
        "opened_url":
            "https://example.com",
    }


def test_building_application_does_not_call_openai():

    client = FakeClient()

    app = build_default_jarvis(
        client=client,
        browser_opener=lambda url: True,
    )

    assert app is not None
