"""
Real browser capability for Jarvis.

This capability opens HTTP(S) URLs using the operating system's
default web browser.

Input:

    {
        "url": "https://example.com"
    }

Output:

    {
        "opened": True,
        "opened_url": "https://example.com"
    }

Safety properties:

- Only http:// and https:// URLs are allowed.
- file:// URLs are rejected.
- javascript: URLs are rejected.
- data: URLs are rejected.
- No arbitrary shell commands are executed.
"""

from collections.abc import Callable, Mapping
from typing import Any
from unicodedata import category
from urllib.parse import urlparse, urlsplit
from uuid import UUID

from core.approval import PendingApprovalTarget, ApprovalSubject
from core.contracts import PermissionMode, RiskLevel
import webbrowser


# ============================================================
# ERRORS
# ============================================================

class BrowserCapabilityError(Exception):
    """
    Raised when the browser capability cannot safely open a URL.
    """


# ============================================================
# URL VALIDATION
# ============================================================

def _normalize_url(
    value: object,
) -> str:
    """
    Validate and normalize one browser URL.
    """

    if not isinstance(
        value,
        str,
    ):

        raise BrowserCapabilityError(
            "browser url must be a string."
        )


    normalized = (
        value.strip()
    )


    if not normalized:

        raise BrowserCapabilityError(
            "browser url must not be empty."
        )


    try:

        parsed = urlparse(
            normalized
        )

    except Exception as error:

        raise BrowserCapabilityError(
            "browser url could not be parsed."
        ) from error


    if (
        parsed.scheme
        not in {
            "http",
            "https",
        }
    ):

        raise BrowserCapabilityError(
            "browser only allows http:// "
            "or https:// URLs."
        )


    if not parsed.netloc:

        raise BrowserCapabilityError(
            "browser url must contain a host."
        )


    return normalized


def _validate_approval_url(value: object) -> str:
    """Validate an exact display-safe URL without rewriting its text."""
    if type(value) is not str:
        raise BrowserCapabilityError("browser approval url must be a plain string.")
    if (
        not value
        or len(value) > 2048
        or "\\" in value
        or any(character.isspace() or category(character) in {"Cc", "Cf"}
               for character in value)
    ):
        raise BrowserCapabilityError("browser approval url contains unsafe text.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BrowserCapabilityError("browser approval requires an HTTP(S) host without credentials.")
        _ = parsed.port
    except ValueError as error:
        raise BrowserCapabilityError("browser approval url could not be parsed.") from error
    return value


def prepare_browser_approval(
    arguments: Mapping[str, Any],
    *,
    request_id: UUID,
    step_number: int,
    risk: RiskLevel,
    effective_permission: PermissionMode,
) -> PendingApprovalTarget:
    """Prepare one browser approval identity, without calling a runtime."""
    if effective_permission is not PermissionMode.CONFIRM_BEFORE_EXECUTION:
        raise BrowserCapabilityError("browser approval requires confirmation permission.")
    if set(arguments) != {"url"}:
        raise BrowserCapabilityError("browser approval requires exactly one url argument.")
    url = _validate_approval_url(arguments["url"])
    subject = ApprovalSubject(
        request_id=request_id,
        step_number=step_number,
        capability="browser",
        risk=risk,
        arguments={"url": url},
    )
    return PendingApprovalTarget(
        subject=subject,
        effective_permission=effective_permission,
        prepared_target=url,
    )


def validate_browser_approval_target(
    target: PendingApprovalTarget,
    *,
    request_id: UUID,
    step_number: int,
    risk: RiskLevel,
) -> None:
    """Check continuation correspondence; never create or select a target."""
    if not isinstance(target, PendingApprovalTarget):
        raise BrowserCapabilityError("browser continuation requires a pending approval target.")
    subject = target.subject
    if (
        subject.request_id != request_id
        or subject.step_number != step_number
        or subject.capability != "browser"
        or subject.risk != risk
        or target.effective_permission is not PermissionMode.CONFIRM_BEFORE_EXECUTION
    ):
        raise BrowserCapabilityError("browser approval metadata does not match its step.")
    _validate_approval_url(target.prepared_target)
    if subject.arguments != {"url": target.prepared_target}:
        raise BrowserCapabilityError("browser approval subject does not represent its prepared url.")


# ============================================================
# ARGUMENT VALIDATION
# ============================================================

def _read_url(
    arguments: Mapping[
        str,
        Any,
    ],
) -> str:
    """
    Read and validate the required url argument.
    """

    if "url" not in arguments:

        raise BrowserCapabilityError(
            "browser requires a 'url' argument."
        )


    return _normalize_url(
        arguments[
            "url"
        ]
    )


# ============================================================
# HANDLER FACTORY
# ============================================================

def create_browser_handler(
    *,
    opener: Callable[
        [str],
        bool,
    ] | None = None,
):
    """
    Create a runtime-compatible browser handler.

    opener is injectable for tests.

    The production default uses:

        webbrowser.open_new_tab
    """

    browser_opener = (
        opener
        if opener is not None
        else webbrowser.open_new_tab
    )


    def browser_handler(
        arguments: Mapping[
            str,
            Any,
        ],
    ) -> Mapping[
        str,
        Any,
    ]:

        url = _read_url(
            arguments
        )


        try:

            opened = browser_opener(
                url
            )

        except Exception as error:

            raise BrowserCapabilityError(
                f"Browser failed to open URL: {url}"
            ) from error


        if not opened:

            raise BrowserCapabilityError(
                f"Browser reported failure opening URL: {url}"
            )


        return {
            "opened": True,
            "opened_url": url,
        }


    return browser_handler
