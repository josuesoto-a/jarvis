import pytest

from core.runtime import (
    CapabilityRuntimeRegistry,
    InvalidRuntimeHandlerError,
    RuntimeAlreadyRegisteredError,
    RuntimeNotFoundError,
)


def fake_weather(arguments):
    return {
        "temperature_c": 22,
        "location": arguments["location"],
    }


def fake_search(arguments):
    return {
        "best_result_url": (
            "https://example.com/result"
        ),
        "query": arguments["query"],
    }


def test_runtime_can_be_registered_and_retrieved():
    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "weather",
        fake_weather,
    )

    handler = runtime.get(
        "weather"
    )

    assert handler is fake_weather


def test_runtime_names_are_canonicalized():
    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "Weather",
        fake_weather,
    )

    assert runtime.has(
        "WEATHER"
    )

    assert runtime.get(
        " weather "
    ) is fake_weather


def test_duplicate_runtime_is_rejected():
    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "weather",
        fake_weather,
    )

    with pytest.raises(
        RuntimeAlreadyRegisteredError
    ):

        runtime.register(
            "WEATHER",
            fake_weather,
        )


def test_missing_runtime_is_rejected():
    runtime = CapabilityRuntimeRegistry()

    with pytest.raises(
        RuntimeNotFoundError
    ):

        runtime.get(
            "computer_use"
        )


def test_non_callable_handler_is_rejected():
    runtime = CapabilityRuntimeRegistry()

    with pytest.raises(
        InvalidRuntimeHandlerError
    ):

        runtime.register(
            "weather",
            "not-a-function",
        )


def test_runtime_names_are_deterministic():
    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "weather",
        fake_weather,
    )

    runtime.register(
        "web_search",
        fake_search,
    )

    assert runtime.names() == (
        "weather",
        "web_search",
    )


def test_runtime_length():
    runtime = CapabilityRuntimeRegistry()

    assert len(runtime) == 0

    runtime.register(
        "weather",
        fake_weather,
    )

    assert len(runtime) == 1


def test_handler_receives_arguments_and_returns_data():
    runtime = CapabilityRuntimeRegistry()

    runtime.register(
        "web_search",
        fake_search,
    )

    handler = runtime.get(
        "web_search"
    )

    result = handler(
        {
            "query": "Big Mac price",
        }
    )

    assert result == {
        "best_result_url": (
            "https://example.com/result"
        ),
        "query": "Big Mac price",
    }
