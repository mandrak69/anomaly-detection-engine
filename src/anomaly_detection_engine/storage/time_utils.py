from datetime import datetime, timezone


def to_utc_iso(value: datetime) -> str:
    """Normalizes a timezone-aware datetime to UTC before storing it as
    text. Without this, two equally-valid but differently-offset
    timestamps for the same instant (+00:00 vs +02:00) would sort
    incorrectly against each other under plain lexicographic ORDER BY --
    every timestamp this project persists is normalized at the point it
    is written, regardless of what offset the original source reported.
    """
    return value.astimezone(timezone.utc).isoformat()
