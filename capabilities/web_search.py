"""
Real read-only web search capability for Jarvis.

This capability uses the OpenAI Responses API web search tool.

Input:

    {
        "query": "..."
    }

Output:

    {
        "query": "...",
        "answer": "...",
        "title": "...",
        "best_result_url": "...",
        "sources": (...)
    }

Safety properties:

- Read-only.
- Does not open a local browser.
- Does not modify files.
- Does not execute terminal commands.
- Does not perform purchases or external writes.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


# ============================================================
# ERRORS
# ============================================================

class WebSearchCapabilityError(Exception):
    """
    Raised when a web search cannot produce a safe,
    usable capability result.
    """


# ============================================================
# STRUCTURED SEARCH RESULT
# ============================================================

class WebSearchDraft(BaseModel):
    """
    Structured final answer produced after web search.
    """

    model_config = ConfigDict(
        extra="forbid"
    )

    answer: str = Field(
        min_length=1,
        max_length=4000,
    )

    title: str = Field(
        min_length=1,
        max_length=500,
    )

    best_result_url: str = Field(
        min_length=1,
        max_length=4000,
    )


# ============================================================
# URL VALIDATION
# ============================================================

def _is_http_url(
    value: str,
) -> bool:
    """
    Return True only for usable HTTP(S) URLs.
    """

    try:

        parsed = urlparse(
            value.strip()
        )

    except Exception:

        return False


    return (
        parsed.scheme
        in {
            "http",
            "https",
        }
        and bool(
            parsed.netloc
        )
    )


# ============================================================
# SOURCE EXTRACTION
# ============================================================

def _extract_source_urls(
    response: object,
) -> tuple[str, ...]:
    """
    Extract unique source URLs from web_search_call items.

    The OpenAI SDK exposes search sources on:

        item.action.sources
    """

    urls: list[str] = []


    output = getattr(
        response,
        "output",
        None,
    )


    if not output:
        return ()


    for item in output:

        if (
            getattr(
                item,
                "type",
                None,
            )
            != "web_search_call"
        ):
            continue


        action = getattr(
            item,
            "action",
            None,
        )


        if action is None:
            continue


        sources = getattr(
            action,
            "sources",
            None,
        )


        if not sources:
            continue


        for source in sources:

            url = getattr(
                source,
                "url",
                None,
            )


            if (
                not isinstance(
                    url,
                    str,
                )
                or not _is_http_url(
                    url
                )
            ):
                continue


            normalized = (
                url.strip()
            )


            if normalized not in urls:

                urls.append(
                    normalized
                )


    return tuple(
        urls
    )


# ============================================================
# QUERY VALIDATION
# ============================================================

def _read_query(
    arguments: Mapping[
        str,
        Any,
    ],
) -> str:
    """
    Read and validate the required query argument.
    """

    if "query" not in arguments:

        raise WebSearchCapabilityError(
            "web_search requires a 'query' argument."
        )


    query = arguments[
        "query"
    ]


    if not isinstance(
        query,
        str,
    ):

        raise WebSearchCapabilityError(
            "web_search query must be a string."
        )


    normalized = (
        query.strip()
    )


    if not normalized:

        raise WebSearchCapabilityError(
            "web_search query must not be empty."
        )


    return normalized


# ============================================================
# HANDLER FACTORY
# ============================================================

def create_web_search_handler(
    *,
    client: OpenAI | None = None,
    model: str = "gpt-5.6-luna",
):
    """
    Create a runtime-compatible web_search handler.

    The returned function matches Jarvis's runtime contract:

        Mapping[str, Any]
            ->
        Mapping[str, Any]
    """

    openai_client = (
        client
        if client is not None
        else OpenAI()
    )


    def web_search_handler(
        arguments: Mapping[
            str,
            Any,
        ],
    ) -> Mapping[
        str,
        Any,
    ]:

        query = _read_query(
            arguments
        )


        # ====================================================
        # REAL WEB SEARCH
        # ====================================================

        response = (
            openai_client
            .responses
            .parse(
                model=model,

                tools=[
                    {
                        "type":
                            "web_search_preview",
                    }
                ],

                tool_choice="required",

                include=[
                    (
                        "web_search_call."
                        "action.sources"
                    )
                ],

                instructions=(
                    "You are the web-search runtime "
                    "for a personal software agent. "
                    "Use web search to answer the query. "
                    "Choose one source that best supports "
                    "the answer. "
                    "best_result_url must be an actual URL "
                    "from the web search results, never a "
                    "made-up URL. "
                    "Keep the answer concise."
                ),

                input=(
                    "Search the web for this query:\n\n"
                    f"{query}"
                ),

                text_format=(
                    WebSearchDraft
                ),
            )
        )


        # ====================================================
        # STRUCTURED OUTPUT
        # ====================================================

        draft = (
            response.output_parsed
        )


        if draft is None:

            raise WebSearchCapabilityError(
                "Web search returned no structured result."
            )


        answer = (
            draft.answer.strip()
        )

        title = (
            draft.title.strip()
        )

        proposed_url = (
            draft
            .best_result_url
            .strip()
        )


        if not answer:

            raise WebSearchCapabilityError(
                "Web search returned an empty answer."
            )


        if not title:

            raise WebSearchCapabilityError(
                "Web search returned an empty title."
            )


        # ====================================================
        # REAL SOURCES USED BY WEB SEARCH
        # ====================================================

        sources = (
            _extract_source_urls(
                response
            )
        )


        # ====================================================
        # GROUNDED URL SELECTION
        # ====================================================

        if (
            proposed_url in sources
            and _is_http_url(
                proposed_url
            )
        ):

            best_result_url = (
                proposed_url
            )


        elif sources:

            # The model may canonicalize or slightly rewrite a URL.
            # We refuse to trust an ungrounded URL and instead use
            # a URL that definitely came from the web-search tool.

            best_result_url = (
                sources[
                    0
                ]
            )


        elif _is_http_url(
            proposed_url
        ):

            # Fallback for API responses where source expansion
            # is unavailable but the structured result contains
            # a syntactically valid URL.

            best_result_url = (
                proposed_url
            )


        else:

            raise WebSearchCapabilityError(
                "Web search produced no usable source URL."
            )


        # ====================================================
        # RUNTIME RESULT
        # ====================================================

        return {
            "query":
                query,

            "answer":
                answer,

            "title":
                title,

            "best_result_url":
                best_result_url,

            "sources":
                sources,
        }


    return web_search_handler
