"""
Application composition root for Jarvis.

This module wires together the core engines and the currently enabled
runtime capabilities.

It contains no planning logic, no execution logic, and no permission
policy of its own.

Its job is dependency composition.
"""

from collections.abc import Callable
from dataclasses import dataclass

from openai import OpenAI

from capabilities.browser import (
    create_browser_handler,
)
from capabilities.web_search import (
    create_web_search_handler,
)
from core.contracts import (
    CapabilitySpec,
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
# BUILT APPLICATION
# ============================================================

@dataclass(frozen=True, slots=True)
class JarvisApplication:
    """
    Fully assembled Jarvis application components.

    Components remain exposed so they can be inspected,
    tested, or replaced independently.
    """

    capability_registry: CapabilityRegistry

    runtime_registry: CapabilityRuntimeRegistry

    resolver: CapabilityResolver

    validator: PlanValidator

    permission_engine: PermissionEngine

    planner: Planner

    executor: Executor

    orchestrator: Orchestrator


# ============================================================
# DEFAULT CAPABILITIES
# ============================================================

def _register_default_capabilities(
    registry: CapabilityRegistry,
) -> None:
    """
    Register the capabilities currently available in Jarvis.
    """

    registry.register(
        CapabilitySpec(
            name="web_search",
            description=(
                "Buscar información pública actual "
                "en Internet."
            ),
            tags=(
                "network",
                "read",
            ),
        )
    )

    registry.register(
        CapabilitySpec(
            name="browser",
            description=(
                "Abrir páginas web HTTP o HTTPS "
                "en el navegador local."
            ),
            tags=(
                "local",
                "browser",
            ),
        )
    )


# ============================================================
# DEFAULT RUNTIMES
# ============================================================

def _register_default_runtimes(
    *,
    runtime_registry: CapabilityRuntimeRegistry,
    client: OpenAI,
    web_search_model: str,
    browser_opener: Callable[[str], bool] | None,
) -> None:
    """
    Bind executable implementations for default capabilities.
    """

    runtime_registry.register(
        "web_search",
        create_web_search_handler(
            client=client,
            model=web_search_model,
        ),
    )

    runtime_registry.register(
        "browser",
        create_browser_handler(
            opener=browser_opener,
        ),
    )


# ============================================================
# BUILD APPLICATION
# ============================================================

def build_default_jarvis(
    *,
    client: OpenAI | None = None,
    planner_model: str = "gpt-5.6-luna",
    web_search_model: str = "gpt-5.6-luna",
    browser_opener: Callable[[str], bool] | None = None,
) -> JarvisApplication:
    """
    Build the default Jarvis application.

    A single OpenAI client is shared between planner and
    OpenAI-backed capabilities.
    """

    openai_client = (
        client
        if client is not None
        else OpenAI()
    )


    # ========================================================
    # CAPABILITIES
    # ========================================================

    capability_registry = (
        CapabilityRegistry()
    )

    _register_default_capabilities(
        capability_registry
    )


    # ========================================================
    # RUNTIMES
    # ========================================================

    runtime_registry = (
        CapabilityRuntimeRegistry()
    )

    _register_default_runtimes(
        runtime_registry=runtime_registry,
        client=openai_client,
        web_search_model=web_search_model,
        browser_opener=browser_opener,
    )


    # ========================================================
    # CORE ENGINES
    # ========================================================

    resolver = CapabilityResolver(
        capability_registry
    )

    validator = PlanValidator(
        resolver
    )

    permission_engine = (
        PermissionEngine()
    )

    planner = Planner(
        capability_registry,
        client=openai_client,
        model=planner_model,
    )

    executor = Executor(
        validator=validator,
        permission_engine=permission_engine,
        runtime_registry=runtime_registry,
    )

    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
    )


    # ========================================================
    # APPLICATION
    # ========================================================

    return JarvisApplication(
        capability_registry=(
            capability_registry
        ),
        runtime_registry=(
            runtime_registry
        ),
        resolver=resolver,
        validator=validator,
        permission_engine=(
            permission_engine
        ),
        planner=planner,
        executor=executor,
        orchestrator=orchestrator,
    )
