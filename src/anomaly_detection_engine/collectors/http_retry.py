import email.utils
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# HTTP statuses this project treats as transient -- worth retrying,
# since the same request can plausibly succeed moments later: 429 (rate
# limited), and the three 5xx statuses that mean "the server itself had
# a problem", not "your request was wrong". Every other status
# (401/403 auth, 404, 400/422 malformed request, ...) is a permanent
# failure retrying cannot fix and must fail immediately, the same as
# before this module existed.
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(headers: object) -> float | None:
    """Parses a Retry-After response header, if the server sent one --
    either the delta-seconds form ("Retry-After: 120") or an HTTP-date
    form ("Retry-After: Wed, 21 Oct 2026 07:28:00 GMT"), both valid per
    RFC 7231 -- returns None (fall back to this module's own backoff)
    when absent or unparseable, never a guessed value.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return None
    value = get("Retry-After")
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _redact_url(url: str) -> str:
    """scheme://host/path only -- no query string, no userinfo. The Odds
    API sends its API key as a query parameter (`?apiKey=...`), so
    logging the complete url would put a real credential in the
    application log on every retry (429/5xx/network failure) -- exactly
    the kind of log line that ends up in a log aggregator, a support
    ticket, or a screen share. The query string carries no information
    this log line actually needs (which host/path is being retried is
    the point, not what parameters it was called with), so it's dropped
    entirely rather than trying to enumerate every provider's own
    credential parameter name, which would only catch known ones and
    silently miss the next provider added later.
    """
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def http_get_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    error_cls: type[Exception],
    provider_label: str,
    max_attempts: int = 3,
    backoff_base_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetches `url`, retrying up to `max_attempts` total attempts (so
    at most max_attempts - 1 retries) for transient failures only:
    a network-level error (urllib.error.URLError -- covers DNS failure,
    connection refused, timeout, ...) or an HTTP response whose status
    is in _RETRYABLE_HTTP_STATUSES. Every other HTTPError (auth,
    malformed request, not found, ...) is raised immediately on the
    first attempt -- retrying a permanent failure only delays reporting
    it, and this project's own "no silent default" discipline (see
    e.g. api_football_collector._map_fixture_status) applies here the
    same way: a request that will never succeed should fail loudly and
    promptly, not be quietly retried into a slower failure.

    error_cls is the caller's own error type (ApiFootballError,
    TheOddsApiError, ...) and provider_label its human-readable name
    ("API-Football", "The Odds API") for the wrapped message -- this
    module has no other provider-specific knowledge; it wraps whatever
    collector-specific exception the caller already raised on a
    permanent failure, just also on the final retryable failure once
    attempts are exhausted, with the exact same message wording either
    way.

    Backoff is exponential (backoff_base_seconds * 2**attempt) unless
    the server sent a Retry-After header (common on 429, sometimes on
    503) -- when present, that value is honored instead of guessing,
    since the server knows its own recovery time better than a fixed
    backoff schedule could. sleep is injectable so tests exercise the
    retry path without actually waiting.
    """
    last_exception: Exception | None = None

    for attempt in range(max_attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data: bytes = response.read()
                return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            wrapped = error_cls(
                f"{provider_label} request failed with HTTP {exc.code}: {body}"
            )
            if exc.code not in _RETRYABLE_HTTP_STATUSES or attempt == max_attempts - 1:
                raise wrapped from exc
            last_exception = wrapped
            delay = _retry_after_seconds(exc.headers) or backoff_base_seconds * (2**attempt)
        except urllib.error.URLError as exc:
            wrapped = error_cls(f"{provider_label} request failed: {exc.reason}")
            if attempt == max_attempts - 1:
                raise wrapped from exc
            last_exception = wrapped
            delay = backoff_base_seconds * (2**attempt)

        logger.warning(
            "http_retry.retrying",
            extra={
                "url": _redact_url(url),
                "attempt": attempt + 1,
                "max_attempts": max_attempts,
                "delay_seconds": delay,
                "error": str(last_exception),
            },
        )
        sleep(delay)

    # Unreachable: the loop above always either returns or raises on its
    # final attempt -- this satisfies the type checker, which cannot
    # otherwise see that last_exception is always set and always raised
    # before the loop would exit normally.
    assert last_exception is not None
    raise last_exception
