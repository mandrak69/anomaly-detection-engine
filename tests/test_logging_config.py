import json
import logging

from anomaly_detection_engine.observability.logging_config import (
    JsonFormatter,
    configure_logging,
)


def _make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="anomaly_detection_engine.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_record_as_valid_json_with_core_fields():
    formatter = JsonFormatter()
    record = _make_record()

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "anomaly_detection_engine.test"
    assert "timestamp" in payload


def test_merges_extra_fields_into_the_payload():
    formatter = JsonFormatter()
    record = _make_record(run_id="run-001", records_accepted=5)

    payload = json.loads(formatter.format(record))

    assert payload["run_id"] == "run-001"
    assert payload["records_accepted"] == 5


def test_serializes_non_json_native_extra_values_via_str():
    from decimal import Decimal

    formatter = JsonFormatter()
    record = _make_record(odds=Decimal("2.15"))

    payload = json.loads(formatter.format(record))

    assert payload["odds"] == "2.15"


def test_configure_logging_does_not_add_duplicate_handlers():
    logger = configure_logging()
    handler_count_after_first = len(logger.handlers)

    configure_logging()

    assert len(logger.handlers) == handler_count_after_first
