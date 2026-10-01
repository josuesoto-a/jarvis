"""Production local-approval adapter composition.

The executable depends on this composition root rather than on
capability-specific approval functions.
"""

from __future__ import annotations

from integrations.local_approval_registry import (
    LocalApprovalAdapter,
    LocalApprovalAdapterRegistry,
)
from integrations.local_browser_approval import (
    approval_is_current,
    format_browser_approval,
    preview_browser_target,
)
from integrations.local_terminal_approval import (
    format_terminal_approval,
    preview_terminal_target,
    terminal_approval_is_current,
)


def build_default_local_approval_registry(
) -> LocalApprovalAdapterRegistry:
    """Build a fresh production local approval registry."""

    registry = LocalApprovalAdapterRegistry()

    registry.register(
        LocalApprovalAdapter(
            capability="browser",
            preview=preview_browser_target,
            render=format_browser_approval,
            is_current=approval_is_current,
        )
    )

    # Presentation availability does not register an execution runtime.
    registry.register(
        LocalApprovalAdapter(
            capability="terminal",
            preview=preview_terminal_target,
            render=format_terminal_approval,
            is_current=terminal_approval_is_current,
        )
    )

    return registry
