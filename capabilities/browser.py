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
from urllib.parse import urlparse
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
