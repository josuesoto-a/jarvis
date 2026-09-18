from types import SimpleNamespace

import pytest

from capabilities.web_search import (
    WebSearchCapabilityError,
    WebSearchDraft,
    create_web_search_handler,
)


# ============================================================
# FAKE OPENAI CLIENT
# ============================================================

class FakeResponse:

    def __init__(
        self,
        *,
        output_parsed,
        source_urls=(),
    ):

        self.output_parsed = (
            output_parsed
        )


        sources = [
            SimpleNamespace(
                url=url
            )
            for url in source_urls
        ]


        self.output = [
            SimpleNamespace(
                type="web_search_call",

                action=SimpleNamespace(
                    type="search",
                    sources=sources,
                ),
            )
        ]


class FakeResponses:

    def __init__(
        self,
        response,
    ):

        self.response = response

        self.last_kwargs = None


    def parse(
        self,
        **kwargs,
    ):

        self.last_kwargs = (
            kwargs
        )

        return self.response


class FakeClient:

    def __init__(
        self,
        response,
    ):

        self.responses = (
            FakeResponses(
                response
            )
        )


# ============================================================
# HELPERS
# ============================================================

def make_response(
    *,
    answer="A useful answer.",
    title="Example Result",
    best_result_url=(
        "https://example.com/result"
    ),
    sources=(
        "https://example.com/result",
    ),
):

    return FakeResponse(
        output_parsed=(
            WebSearchDraft(
                answer=answer,
                title=title,
                best_result_url=(
                    best_result_url
                ),
            )
        ),
        source_urls=sources,
    )


# ============================================================
# TESTS
# ============================================================

def test_real_source_url_is_preserved():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    result = handler(
        {
            "query":
                "Big Mac price"
        }
    )


    assert (
        result[
            "best_result_url"
        ]
        == "https://example.com/result"
    )

    assert result[
        "query"
    ] == "Big Mac price"


def test_query_is_stripped():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    result = handler(
        {
            "query":
                "   Big Mac price   "
        }
    )


    assert (
        result[
            "query"
        ]
        == "Big Mac price"
    )


def test_missing_query_is_rejected():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    with pytest.raises(
        WebSearchCapabilityError
    ):

        handler(
            {}
        )


def test_empty_query_is_rejected():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    with pytest.raises(
        WebSearchCapabilityError
    ):

        handler(
            {
                "query": "   "
            }
        )


def test_non_string_query_is_rejected():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    with pytest.raises(
        WebSearchCapabilityError
    ):

        handler(
            {
                "query": 123
            }
        )


def test_ungrounded_model_url_falls_back_to_real_source():

    client = FakeClient(
        make_response(
            best_result_url=(
                "https://invented.example/fake"
            ),
            sources=(
                "https://real.example/result",
            ),
        )
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    result = handler(
        {
            "query":
                "example"
        }
    )


    assert (
        result[
            "best_result_url"
        ]
        == "https://real.example/result"
    )


def test_valid_model_url_can_be_used_when_sources_are_absent():

    client = FakeClient(
        make_response(
            best_result_url=(
                "https://example.com/result"
            ),
            sources=(),
        )
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    result = handler(
        {
            "query":
                "example"
        }
    )


    assert (
        result[
            "best_result_url"
        ]
        == "https://example.com/result"
    )


def test_invalid_url_without_sources_is_rejected():

    client = FakeClient(
        make_response(
            best_result_url=(
                "not-a-url"
            ),
            sources=(),
        )
    )

    handler = (
        create_web_search_handler(
            client=client
        )
    )


    with pytest.raises(
        WebSearchCapabilityError
    ):

        handler(
            {
                "query":
                    "example"
            }
        )


def test_openai_request_uses_web_search_tool():

    client = FakeClient(
        make_response()
    )

    handler = (
        create_web_search_handler(
            client=client,
            model="test-model",
        )
    )


    handler(
        {
            "query":
                "Big Mac price"
        }
    )


    kwargs = (
        client
        .responses
        .last_kwargs
    )


    assert (
        kwargs[
            "model"
        ]
        == "test-model"
    )

    assert (
        kwargs[
            "tools"
        ]
        == [
            {
                "type":
                    "web_search_preview"
            }
        ]
    )

    assert (
        kwargs[
            "tool_choice"
        ]
        == "required"
    )

    assert (
        kwargs[
            "text_format"
        ]
        is WebSearchDraft
    )

    assert (
        (
            "web_search_call."
            "action.sources"
        )
        in kwargs[
            "include"
        ]
    )
