import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime

from anomaly_detection_engine.config import AppConfig, load_config, load_dotenv
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.pipeline import run_detection, run_ingestion
from anomaly_detection_engine.runtime import Runtime, build_runtime

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 300.0
DEFAULT_BURST_INTERVAL_SECONDS = 1800.0


def resolve_poll_interval(
    base_interval_seconds: float,
    burst_start_hour: int,
    burst_end_hour: int,
    burst_interval_seconds: float,
    *,
    now: datetime | None = None,
) -> float:
    """Returns burst_interval_seconds while the current *local* wall-clock
    hour falls in [burst_start_hour, burst_end_hour), else
    base_interval_seconds -- lets a fixed daily API request budget be
    spent unevenly on purpose: a short daily window polled much faster
    (e.g. tripled) around real match-kickoff peaks, with every other
    hour left at the slower baseline that keeps the whole day under the
    provider's daily cap. See poller.main()'s BURST_START_HOUR/
    BURST_END_HOUR/BURST_INTERVAL_SECONDS.

    Local time, deliberately, unlike every other timestamp in this
    system (which is UTC because it's compared against or stored
    alongside real event/quote data): this window is an operator's own
    daily schedule ("6-9pm on this machine"), not a fact about the data.

    A simple non-wrapping range only (burst_start_hour < burst_end_hour)
    -- main() validates this at startup so a misconfigured window fails
    loud immediately rather than silently never triggering (or always
    triggering) hours into an unattended run.
    """
    current_hour = (now if now is not None else datetime.now()).hour
    if burst_start_hour <= current_hour < burst_end_hour:
        return burst_interval_seconds
    return base_interval_seconds


def run_cycle(runtime: Runtime, config: AppConfig) -> dict[str, int]:
    """One full ingest-then-detect cycle -- exactly the two calls
    app.py's main() makes once, extracted here so run_forever() can
    repeat them without duplicating that sequence.
    """
    events = run_ingestion(runtime, config)
    return run_detection(runtime, events, config)


def run_forever(
    runtime: Runtime,
    config: AppConfig,
    *,
    interval_seconds: float,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    interval_provider: Callable[[], float] | None = None,
) -> int:
    """Repeats run_cycle() every interval_seconds until should_stop()
    returns True (or max_cycles is reached) -- the actual continuous
    odds-monitoring process, as opposed to app.py's main(), which still
    does exactly one cycle and exits (kept as-is: still useful for a
    cron/systemd-timer-driven single poll, or a one-off manual run).

    interval_provider, when given, is called fresh before every sleep
    instead of reusing the static interval_seconds -- this is how a
    burst window (see resolve_poll_interval) takes effect mid-run
    without needing the loop restarted: interval_seconds itself still
    has to be a plain float (it's also what gets logged below as the
    run's nominal interval), so the *varying* schedule is layered on
    top via this optional callable rather than changing what
    interval_seconds means for every existing caller/test.

    A failure in one cycle (an unexpected DB error, a bug in detection,
    anything that isn't a single collector's own fetch failure --
    OddsIngestionService.run() already isolates *that* into a FAILED
    CollectorRun without raising) is logged and the loop continues
    rather than crashing the whole process. One bad cycle should not end
    a run meant to keep going for days; this is the outer safety net for
    whatever escapes OddsIngestionService's own per-collector handling.

    should_stop/sleep/max_cycles are all injectable so this loop is
    unit-testable without real signals or real waiting: should_stop
    lets a test (or main()'s signal handling) request a clean exit
    between cycles rather than mid-cycle, sleep lets a test observe
    every requested delay without actually waiting it out, and
    max_cycles bounds an otherwise-infinite loop for a test (or a
    deliberately finite run) -- production use leaves it None and relies
    on should_stop/an actual interrupt instead.

    Returns the number of cycles actually completed.
    """
    logger.info(
        "poller.started", extra={"interval_seconds": interval_seconds, "max_cycles": max_cycles}
    )
    cycle = 0

    while not should_stop() and (max_cycles is None or cycle < max_cycles):
        cycle += 1
        try:
            summary = run_cycle(runtime, config)
            logger.info("poller.cycle_completed", extra={"cycle": cycle, **summary})
        except Exception as exc:
            logger.error(
                "poller.cycle_failed",
                extra={
                    "cycle": cycle,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
                exc_info=True,
            )

        if should_stop() or (max_cycles is not None and cycle >= max_cycles):
            break
        sleep(interval_provider() if interval_provider is not None else interval_seconds)

    logger.info("poller.stopped", extra={"cycles_completed": cycle})
    return cycle


def main() -> None:
    configure_logging()

    load_dotenv()
    config = load_config()
    runtime = build_runtime(config)
    interval_seconds = float(
        os.environ.get("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))
    )

    # Burst window is entirely opt-in: BURST_START_HOUR/BURST_END_HOUR
    # unset (the default) means interval_provider stays None below and
    # run_forever() behaves exactly as it did before this feature
    # existed. Both must be set together -- one without the other is
    # almost certainly a typo'd .env, not a deliberate half-config, so
    # it fails loud here rather than silently ignoring the one that was
    # set.
    burst_start_raw = os.environ.get("BURST_START_HOUR")
    burst_end_raw = os.environ.get("BURST_END_HOUR")
    interval_provider: Callable[[], float] | None = None
    if burst_start_raw is not None or burst_end_raw is not None:
        if burst_start_raw is None or burst_end_raw is None:
            raise ValueError(
                "BURST_START_HOUR and BURST_END_HOUR must both be set together "
                f"(got BURST_START_HOUR={burst_start_raw!r}, BURST_END_HOUR={burst_end_raw!r})"
            )
        burst_start_hour = int(burst_start_raw)
        burst_end_hour = int(burst_end_raw)
        if not (0 <= burst_start_hour < burst_end_hour <= 24):
            raise ValueError(
                "BURST_START_HOUR/BURST_END_HOUR must satisfy "
                f"0 <= start < end <= 24, got {burst_start_hour}-{burst_end_hour}"
            )
        burst_interval_seconds = float(
            os.environ.get("BURST_INTERVAL_SECONDS", str(DEFAULT_BURST_INTERVAL_SECONDS))
        )
        logger.info(
            "poller.burst_window_enabled",
            extra={
                "burst_start_hour": burst_start_hour,
                "burst_end_hour": burst_end_hour,
                "burst_interval_seconds": burst_interval_seconds,
                "base_interval_seconds": interval_seconds,
            },
        )

        def interval_provider() -> float:
            return resolve_poll_interval(
                interval_seconds, burst_start_hour, burst_end_hour, burst_interval_seconds
            )

    # A threading.Event, not a bare module-level flag, so should_stop is
    # a plain zero-arg callable run_forever can call without knowing
    # anything about signals -- signal handling stays entirely main()'s
    # concern. Checked between cycles, not inside one: letting an
    # in-progress cycle finish (every write it makes is already its own
    # transaction) is simpler and safer than trying to interrupt one
    # partway through.
    stop_requested = threading.Event()

    def _handle_stop_signal(signum: int, frame: object) -> None:
        logger.info("poller.signal_received", extra={"signal": signum})
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_stop_signal)
    # SIGTERM is what a process manager (systemd, docker stop, ...) sends
    # for a graceful shutdown request; registered defensively since not
    # every platform's process-kill path actually delivers it to a
    # Python handler (notably plain TerminateProcess on Windows), but
    # costs nothing where it does work.
    signal.signal(signal.SIGTERM, _handle_stop_signal)

    run_forever(
        runtime,
        config,
        interval_seconds=interval_seconds,
        should_stop=stop_requested.is_set,
        interval_provider=interval_provider,
    )


if __name__ == "__main__":
    main()
