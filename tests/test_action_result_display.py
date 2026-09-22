from __future__ import annotations

import json

from integrations.action_result_display import (
    extract_copyable_urls,
)


def test_extracts_best_result_url():
    output = json.dumps(
        {
            "status": "completed",
            "step_results": [
                {
                    "capability": "web_search",
                    "data": {
                        "best_result_url": (
                            "https://docs.python.org/3/"
                        ),
                        "title": (
                            "Python Documentation"
                        ),
                    },
                }
            ],
        }
    )

    assert extract_copyable_urls(
        output
    ) == (
        "https://docs.python.org/3/",
    )


def test_does_not_dump_sources_list():
    output = json.dumps(
        {
            "step_results": [
                {
                    "data": {
                        "best_result_url": (
                            "https://fastapi.tiangolo.com/"
                        ),
                        "sources": [
                            "https://example.com/1",
                            "https://example.com/2",
                        ],
                    }
                }
            ]
        }
    )

    assert extract_copyable_urls(
        output
    ) == (
        "https://fastapi.tiangolo.com/",
    )


def test_deduplicates_urls():
    output = json.dumps(
        {
            "url": "https://example.com/",
            "nested": {
                "best_result_url": (
                    "https://example.com/"
                )
            },
        }
    )

    assert extract_copyable_urls(
        output
    ) == (
        "https://example.com/",
    )


def test_invalid_json_returns_empty_tuple():
    assert extract_copyable_urls(
        "not-json"
    ) == ()
