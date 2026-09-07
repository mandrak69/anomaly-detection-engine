import logging
import os
import signal
import threading
import time
from collections.abc import Callable

from anomaly_detection_engine.config import AppConfig, load_config, load_dotenv
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.pipeline import run_detection, run_ingestion
from anomaly_detection_engine.runtime import Runtime, build_runtime

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 300.0


def run_cycle(runtime: Runtime, config: AppConfig) -> dict:
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
) -> int:
    """Repeats run_cycle() every interval_seconds until should_stop()
    returns True (or max_cycles is reached) -- the actual continuous
    odds-monitoring process, as opposed to app.py's main(), which still
    does exactly one cycle and exits (kept as-is: still useful for a
    cron/systemd-timer-driven single poll, or a one-off manual run).

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
        sleep(interval_seconds)

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
        runtime, config, interval_seconds=interval_seconds, should_stop=stop_requested.is_set
    )


if __name__ == "__main__":
    main()
