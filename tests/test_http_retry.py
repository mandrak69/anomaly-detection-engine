import email.message
import email.utils
import io
import logging
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from anomaly_detection_engine.collectors.http_retry import _redact_url, http_get_with_retry


class DummyError(RuntimeError):
    pass


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://example.test", code, "error", headers, io.BytesIO(b"error body")
    )


def make_fake_urlopen(actions):
    """actions: a list where each item is either bytes (a successful
    response body) or an exception instance to raise -- one consumed
    per call, in order."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        action = actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return FakeResponse(action)

    fake_urlopen.calls = calls
    return fake_urlopen


def test_redact_url_strips_the_query_string():
    # The Odds API sends its API key as ?apiKey=... -- the query string
    # must never survive into a log line.
    redacted = _redact_url("https://api.the-odds-api.com/v4/sports/soccer/odds?apiKey=SECRET123&regions=eu")

    assert "SECRET123" not in redacted
    assert redacted == "https://api.the-odds-api.com/v4/sports/soccer/odds"


def test_redact_url_strips_userinfo_too():
    redacted = _redact_url("https://user:hunter2@example.test/path?x=1")

    assert "hunter2" not in redacted
    assert redacted == "https://example.test/path"


def test_retry_log_never_contains_the_api_key_in_the_url(monkeypatch, caplog):
    url = "https://api.the-odds-api.com/v4/sports/soccer/odds?apiKey=SUPER-SECRET-KEY&regions=eu"
    fake_urlopen = make_fake_urlopen([http_error(503), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )

    # The package logger (see observability.logging_config.configure_logging)
    # sets propagate=False once configured -- if an earlier test in this
    # same process already called it, records would never reach caplog's
    # own handler on the root logger otherwise. Attaching caplog's handler
    # directly works regardless of that, so this doesn't depend on test
    # execution order.
    package_logger = logging.getLogger("anomaly_detection_engine")
    package_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="anomaly_detection_engine"):
            http_get_with_retry(
                url,
                headers={},
                timeout=10,
                error_cls=DummyError,
                provider_label="Dummy",
                sleep=lambda seconds: None,
            )
    finally:
        package_logger.removeHandler(caplog.handler)

    assert len(caplog.records) >= 1  # the retry warning was actually captured
    assert "SUPER-SECRET-KEY" not in caplog.text
    for record in caplog.records:
        assert "SUPER-SECRET-KEY" not in str(record.__dict__.get("url", ""))


def test_succeeds_on_the_first_attempt_without_sleeping(monkeypatch):
    fake_urlopen = make_fake_urlopen([b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    result = http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        sleep=sleeps.append,
    )

    assert result == b"ok"
    assert sleeps == []
    assert len(fake_urlopen.calls) == 1


def test_retries_a_transient_503_and_then_succeeds(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(503), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    result = http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        sleep=sleeps.append,
    )

    assert result == b"ok"
    assert len(sleeps) == 1
    assert len(fake_urlopen.calls) == 2


def test_retries_a_429_and_then_succeeds(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(429), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )

    result = http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        sleep=lambda seconds: None,
    )

    assert result == b"ok"


def test_retries_a_network_error_and_then_succeeds(monkeypatch):
    fake_urlopen = make_fake_urlopen(
        [urllib.error.URLError("connection refused"), b"ok"]
    )
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )

    result = http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        sleep=lambda seconds: None,
    )

    assert result == b"ok"


def test_exhausting_all_attempts_on_a_transient_error_raises_the_wrapped_error(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(503), http_error(503), http_error(503)])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    with pytest.raises(DummyError, match="HTTP 503"):
        http_get_with_retry(
            "https://example.test",
            headers={},
            timeout=10,
            error_cls=DummyError,
            provider_label="Dummy",
            max_attempts=3,
            sleep=sleeps.append,
        )

    assert len(fake_urlopen.calls) == 3
    # Exactly max_attempts - 1 retries -- one sleep between each pair of
    # attempts, none after the final, non-retried failure.
    assert len(sleeps) == 2


def test_a_permanent_401_fails_immediately_without_retrying(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(401)])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    with pytest.raises(DummyError, match="HTTP 401"):
        http_get_with_retry(
            "https://example.test",
            headers={},
            timeout=10,
            error_cls=DummyError,
            provider_label="Dummy",
            sleep=sleeps.append,
        )

    assert len(fake_urlopen.calls) == 1
    assert sleeps == []


def test_a_permanent_404_fails_immediately_without_retrying(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(404)])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )

    with pytest.raises(DummyError, match="HTTP 404"):
        http_get_with_retry(
            "https://example.test",
            headers={},
            timeout=10,
            error_cls=DummyError,
            provider_label="Dummy",
            sleep=lambda seconds: None,
        )

    assert len(fake_urlopen.calls) == 1


def test_retry_after_header_in_seconds_is_honored_instead_of_backoff(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(429, retry_after="30"), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        backoff_base_seconds=1.0,
        sleep=sleeps.append,
    )

    assert sleeps == [30.0]


def test_retry_after_zero_is_honored_not_treated_as_absent(monkeypatch):
    # Regression test: `retry_after or backoff` would discard a real
    # "Retry-After: 0" (retry immediately) because 0.0 is falsy, and
    # fall through to a slower exponential-backoff wait the server never
    # asked for. Must be distinguished from "no header at all" via an
    # explicit `is not None` check.
    fake_urlopen = make_fake_urlopen([http_error(429, retry_after="0"), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        backoff_base_seconds=5.0,
        sleep=sleeps.append,
    )

    assert sleeps == [0.0]


def test_retry_after_larger_than_max_delay_is_clamped(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(429, retry_after="600"), b"ok"])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        max_delay_seconds=60.0,
        sleep=sleeps.append,
    )

    assert sleeps == [60.0]


def test_exponential_backoff_is_also_clamped_to_max_delay(monkeypatch):
    fake_urlopen = make_fake_urlopen(
        [http_error(503), http_error(503), http_error(503), b"ok"]
    )
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        max_attempts=4,
        backoff_base_seconds=10.0,
        max_delay_seconds=15.0,
        sleep=sleeps.append,
    )

    # Unclamped this would be 10, 20, 40 -- clamped to 15 wherever it
    # would otherwise exceed that.
    assert sleeps == [10.0, 15.0, 15.0]


def test_retry_after_header_as_an_http_date_is_honored(monkeypatch):
    future = datetime.now(UTC) + timedelta(seconds=30)
    fake_urlopen = make_fake_urlopen(
        [http_error(503, retry_after=email.utils.format_datetime(future)), b"ok"]
    )
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        sleep=sleeps.append,
    )

    assert len(sleeps) == 1
    # Allow a couple seconds of slack for real wall-clock time elapsed
    # while this test ran, not an exact 30.0.
    assert 27.0 <= sleeps[0] <= 30.0


def test_no_retry_after_header_falls_back_to_exponential_backoff(monkeypatch):
    fake_urlopen = make_fake_urlopen(
        [http_error(503), http_error(503), b"ok"]
    )
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )
    sleeps = []

    http_get_with_retry(
        "https://example.test",
        headers={},
        timeout=10,
        error_cls=DummyError,
        provider_label="Dummy",
        max_attempts=3,
        backoff_base_seconds=1.0,
        sleep=sleeps.append,
    )

    assert sleeps == [1.0, 2.0]


def test_error_message_includes_the_provider_label_and_response_body(monkeypatch):
    fake_urlopen = make_fake_urlopen([http_error(401)])
    monkeypatch.setattr(
        "anomaly_detection_engine.collectors.http_retry.urllib.request.urlopen", fake_urlopen
    )

    with pytest.raises(DummyError, match="Dummy request failed with HTTP 401: error body"):
        http_get_with_retry(
            "https://example.test",
            headers={},
            timeout=10,
            error_cls=DummyError,
            provider_label="Dummy",
            sleep=lambda seconds: None,
        )
