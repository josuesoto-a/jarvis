import pytest

from capabilities.browser import (
    BrowserCapabilityError,
    create_browser_handler,
)


# ============================================================
# TEST HELPERS
# ============================================================

class FakeOpener:

    def __init__(
        self,
        *,
        result=True,
        error=None,
    ):

        self.result = result
        self.error = error

        self.calls = []


    def __call__(
        self,
        url,
    ):

        self.calls.append(
            url
        )

        if self.error is not None:

            raise self.error

        return self.result


# ============================================================
# TESTS
# ============================================================

def test_https_url_is_opened():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    result = handler(
        {
            "url":
                "https://example.com"
        }
    )


    assert opener.calls == [
        "https://example.com"
    ]

    assert result == {
        "opened": True,
        "opened_url":
            "https://example.com",
    }


def test_http_url_is_allowed():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    result = handler(
        {
            "url":
                "http://example.com"
        }
    )


    assert (
        result[
            "opened_url"
        ]
        == "http://example.com"
    )


def test_url_is_stripped():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    result = handler(
        {
            "url":
                "   https://example.com/path   "
        }
    )


    assert opener.calls == [
        "https://example.com/path"
    ]

    assert (
        result[
            "opened_url"
        ]
        == "https://example.com/path"
    )


def test_missing_url_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {}
        )


    assert opener.calls == []


def test_non_string_url_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url": 123
            }
        )


    assert opener.calls == []


def test_file_url_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "file:///C:/Windows/System32"
            }
        )


    assert opener.calls == []


def test_javascript_url_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "javascript:alert('hello')"
            }
        )


    assert opener.calls == []


def test_data_url_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "data:text/html,hello"
            }
        )


    assert opener.calls == []


def test_url_without_host_is_rejected():

    opener = FakeOpener()

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "https:///missing-host"
            }
        )


    assert opener.calls == []


def test_opener_failure_is_reported():

    opener = FakeOpener(
        result=False
    )

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "https://example.com"
            }
        )


def test_opener_exception_is_wrapped():

    opener = FakeOpener(
        error=RuntimeError(
            "browser unavailable"
        )
    )

    handler = (
        create_browser_handler(
            opener=opener
        )
    )


    with pytest.raises(
        BrowserCapabilityError
    ):

        handler(
            {
                "url":
                    "https://example.com"
            }
        )
