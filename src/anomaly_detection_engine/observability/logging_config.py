import json
import logging
from datetime import datetime, timezone

_RESERVED_LOG_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}

LOGGER_NAME = "anomaly_detection_engine"


class JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON object per line.

    Any field passed via `extra={...}` is merged into the object
    alongside timestamp/level/logger/message, so callers can attach
    structured context (source, run_id, records_accepted, ...) without
    stuffing it into the message string.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Attaches a JSON stream handler to the package logger, once.

    Safe to call multiple times (e.g. once per script entry point) --
    a second call just adjusts the level rather than adding a duplicate
    handler.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    if not any(isinstance(handler.formatter, JsonFormatter) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False

    return logger
