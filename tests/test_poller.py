import logging
from datetime import datetime

import anomaly_detection_engine.poller as poller
from anomaly_detection_engine.observability.logging_config import configure_logging


def test_logger_name_is_a_fixed_dotted_path_not___name__():
    # poller.py is routinely executed as the entry point itself
    # (`python -m anomaly_detection_engine.poller`), under which Python
    # sets the module's own __name__ to "__main__" -- a
    # logging.getLogger(__name__) there would silently create a logger
    # with no relation to configure_logging()'s "anomaly_detection_
    # engine" hierarchy, dropping every poller.* log message (including
    # a crashed cycle's traceback) with no error of its own. Confirmed
    # live: zero occurrences of poller.started/poller.cycle_completed/
    # poller.cycle_failed/poller.burst_window_enabled/poller.stopped in
    # any retained log from an actual `-m`-launched run.
    assert poller.logger.name == "anomaly_detection_engine.poller"


def test_logger_propagates_to_the_configured_handler():
    # The real-world consequence of the above: a record logged here
    # must actually reach the handler configure_logging() attaches
    # ("anomaly_detection_engine", which this module's logger must be a
    # descendant of), not just have the right name in isolation. Not
    # pytest's caplog fixture: configure_logging() sets propagate=False
    # on "anomaly_detection_engine" (so this project's own JSON
    # formatting is the only output, not also duplicated by a root
    # handler), which is exactly what stops a record from ever reaching
    # caplog's own default root-logger capture too -- a plain temporary
    # handler on the real logger is what actually proves propagation,
    # the same thing production relies on.
    root = configure_logging()
    records: list[logging.LogRecord] = []
    capture_handler = logging.Handler()
    capture_handler.emit = records.append  # type: ignore[method-assign]
    root.addHandler(capture_handler)
    try:
        poller.logger.info("test.marker")
    finally:
        root.removeHandler(capture_handler)
    assert any(r.getMessage() == "test.marker" for r in records)


def test_run_cycle_ingests_then_detects_in_order(monkeypatch):
    calls = []

    def fake_run_ingestion(runtime, config):
        calls.append("ingestion")
        return ["event-1"]

    def fake_run_detection(runtime, events, config):
        calls.append("detection")
        assert events == ["event-1"]
        return {"active_surebets": 0}

    monkeypatch.setattr(poller, "run_ingestion", fake_run_ingestion)
    monkeypatch.setattr(poller, "run_detection", fake_run_detection)

    result = poller.run_cycle(runtime=object(), config=object())

    assert calls == ["ingestion", "detection"]
    assert result == {"active_surebets": 0}


def test_run_forever_stops_after_max_cycles(monkeypatch):
    call_count = 0

    def fake_run_cycle(runtime, config):
        nonlocal call_count
        call_count += 1
        return {"active_surebets": 0}

    sleep_calls = []
    monkeypatch.setattr(poller, "run_cycle", fake_run_cycle)

    completed = poller.run_forever(
        runtime=object(),
        config=object(),
        interval_seconds=5,
        sleep=sleep_calls.append,
        max_cycles=3,
    )

    assert call_count == 3
    assert completed == 3
    # No sleep after the final cycle -- nothing left to wait for.
    assert sleep_calls == [5, 5]


def test_run_forever_stops_when_should_stop_becomes_true_between_cycles(monkeypatch):
    call_count = 0

    def fake_run_cycle(runtime, config):
        nonlocal call_count
        call_count += 1
        return {}

    monkeypatch.setattr(poller, "run_cycle", fake_run_cycle)

    checks = {"n": 0}

    def should_stop() -> bool:
        checks["n"] += 1
        # should_stop() is called twice per completed cycle (the loop
        # guard before it starts, the break-check right after it
        # finishes) -- False for the first 4 calls (guard/post for
        # cycles 1 and 2), True from the 5th call on, so a third cycle
        # never starts. Simulates a stop signal arriving while cycle 2
        # is still running.
        return checks["n"] > 4

    completed = poller.run_forever(
        runtime=object(),
        config=object(),
        interval_seconds=1,
        should_stop=should_stop,
        sleep=lambda seconds: None,
    )

    assert call_count == 2
    assert completed == 2


def test_run_forever_continues_after_a_cycle_raises(monkeypatch):
    attempts = []

    def flaky_run_cycle(runtime, config):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("simulated transient failure")
        return {"active_surebets": 0}

    monkeypatch.setattr(poller, "run_cycle", flaky_run_cycle)

    completed = poller.run_forever(
        runtime=object(),
        config=object(),
        interval_seconds=0,
        sleep=lambda seconds: None,
        max_cycles=2,
    )

    # Must not raise, and must still complete both scheduled cycles even
    # though the first one failed.
    assert len(attempts) == 2
    assert completed == 2


def test_run_forever_with_max_cycles_zero_never_calls_run_cycle(monkeypatch):
    monkeypatch.setattr(
        poller,
        "run_cycle",
        lambda runtime, config: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    completed = poller.run_forever(
        runtime=object(), config=object(), interval_seconds=1, max_cycles=0
    )

    assert completed == 0


def test_run_forever_uses_interval_provider_instead_of_static_interval(monkeypatch):
    monkeypatch.setattr(poller, "run_cycle", lambda runtime, config: {})

    sleep_calls = []
    intervals = iter([111, 222, 333])

    poller.run_forever(
        runtime=object(),
        config=object(),
        interval_seconds=999,  # must be ignored once interval_provider is given
        sleep=sleep_calls.append,
        max_cycles=3,
        interval_provider=lambda: next(intervals),
    )

    assert sleep_calls == [111, 222]


def test_resolve_poll_interval_inside_burst_window_returns_burst_interval():
    result = poller.resolve_poll_interval(
        base_interval_seconds=5400,
        burst_start_hour=18,
        burst_end_hour=21,
        burst_interval_seconds=1800,
        now=datetime(2026, 9, 11, 19, 30),
    )

    assert result == 1800


def test_resolve_poll_interval_outside_burst_window_returns_base_interval():
    result = poller.resolve_poll_interval(
        base_interval_seconds=5400,
        burst_start_hour=18,
        burst_end_hour=21,
        burst_interval_seconds=1800,
        now=datetime(2026, 9, 11, 21, 0),
    )

    assert result == 5400


def test_resolve_poll_interval_start_hour_boundary_is_inclusive():
    result = poller.resolve_poll_interval(
        base_interval_seconds=5400,
        burst_start_hour=18,
        burst_end_hour=21,
        burst_interval_seconds=1800,
        now=datetime(2026, 9, 11, 18, 0),
    )

    assert result == 1800


def test_resolve_poll_interval_end_hour_boundary_is_exclusive():
    result = poller.resolve_poll_interval(
        base_interval_seconds=5400,
        burst_start_hour=18,
        burst_end_hour=21,
        burst_interval_seconds=1800,
        now=datetime(2026, 9, 11, 20, 59, 59),
    )

    assert result == 1800
