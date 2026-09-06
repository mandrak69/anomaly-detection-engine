from watch_capture import check_and_run_once


def test_no_run_when_no_capture_waiting(tmp_path):
    calls = []
    triggered = check_and_run_once(
        tmp_path / "live.json",
        env={},
        poll_seconds=0.01,
        runner=lambda *a, **k: calls.append((a, k)),
    )

    assert triggered is False
    assert calls == []


def test_runs_the_app_when_a_capture_is_waiting(tmp_path):
    watch_path = tmp_path / "live.json"
    watch_path.write_text("{}", encoding="utf-8")

    calls = []
    triggered = check_and_run_once(
        watch_path,
        env={"MOZZART_CAPTURE_DIR": str(tmp_path)},
        poll_seconds=0.01,
        runner=lambda *a, **k: calls.append((a, k)),
    )

    assert triggered is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["env"] == {"MOZZART_CAPTURE_DIR": str(tmp_path)}
    assert "-m" in args[0] and "anomaly_detection_engine.app" in args[0]
