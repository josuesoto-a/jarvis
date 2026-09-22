"""Helpers for displaying structured Jarvis action results."""

from __future__ import annotations

import json


_COPYABLE_URL_KEYS = {
    "url",
    "best_result_url",
}


def extract_copyable_urls(
    output_json: str,
) -> tuple[str, ...]:
    """Extract useful HTTP(S) URLs from one terminal action result.

    Only explicitly URL-shaped dictionary fields are collected.
    Arbitrary strings inside lists such as search sources are not dumped
    automatically, keeping the console result concise.
    """

    try:
        payload = json.loads(
            output_json
        )

    except (
        TypeError,
        json.JSONDecodeError,
    ):
        return ()

    found: list[str] = []

    def visit(
        value,
    ) -> None:
        if isinstance(
            value,
            dict,
        ):
            for key, child in value.items():

                if (
                    key in _COPYABLE_URL_KEYS
                    and isinstance(
                        child,
                        str,
                    )
                ):
                    candidate = (
                        child.strip()
                    )

                    if (
                        candidate.startswith(
                            (
                                "https://",
                                "http://",
                            )
                        )
                        and candidate
                        not in found
                    ):
                        found.append(
                            candidate
                        )

                visit(
                    child
                )

        elif isinstance(
            value,
            list,
        ):
            for child in value:
                visit(
                    child
                )

    visit(
        payload
    )

    return tuple(
        found
    )
