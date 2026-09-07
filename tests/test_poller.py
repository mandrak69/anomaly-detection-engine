import anomaly_detection_engine.poller as poller


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
