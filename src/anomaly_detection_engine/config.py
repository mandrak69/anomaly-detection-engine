import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

# Default location for the persistent runtime database -- overridable via
# DB_PATH so tests/alternate deployments aren't forced to use this exact
# file. ":memory:" (pass DB_PATH=:memory:) is still supported for anyone
# who wants the old throwaway-per-run behavior back.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "anomaly_detection.db"

# How long a connection waits for a lock before raising "database is
# locked", in seconds. Matters now that manual-capture sources can be
# triggered by independent watch_capture.py processes: two of them
# spawning app.py at close to the same moment both open this same file,
# and SQLite allows only one writer at a time. A generous timeout makes
# the second one wait instead of failing outright.
DB_BUSY_TIMEOUT_SECONDS = 30

_VALID_ODDS_SOURCES = ("demo", "the-odds-api")


@dataclass(frozen=True)
class AppConfig:
    """Every environment-variable-driven decision the *core* pipeline
    needs, resolved exactly once at startup -- so nothing downstream
    (pipeline.run_ingestion, pipeline.run_detection,
    pipeline.persist_detected_signals) reads os.environ directly, and
    every choice fails loudly here rather than silently deep inside a
    poll cycle.

    Deliberately does not include MIN_SUREBET_PROFIT_PERCENT: that
    threshold only ever controls whether a row is worth a line in
    reporting.opportunity_report's output (SUREBET candidates are
    persisted unconditionally -- see persist_detected_signals), a
    presentation decision belonging to reporting.console, not the core
    config every pipeline stage shares. min_value_gap_percent stays here
    because it *is* a detection-level threshold (part of what counts as
    an outlier, not a display filter on top).

    signal_ttl is the lifecycle policy for SignalRepository.
    expire_active_signals -- deliberately just a plain timedelta here,
    not a sport/phase-specific table: the repository itself only ever
    takes an already-computed cutoff datetime (see run_detection), so a
    future per-sport or per-MarketPhase policy can replace how this one
    field is computed without changing the repository's API at all.
    """

    db_path: str
    odds_source: str
    sport_key: str
    odds_api_mode: str
    odds_api_capture_dir: str | None
    mozzart_capture_dir: str | None
    mozzart_mode: str
    min_value_gap_percent: Decimal
    signal_ttl: timedelta
    # None means "not configured" -- api-football.com is an opt-in
    # supplemental source (see pipeline._supplemental_collectors), the
    # same shape as mozzart_capture_dir above, not a required setting.
    api_football_key: str | None


def load_config() -> AppConfig:
    odds_source = os.environ.get("ODDS_SOURCE", "demo")
    if odds_source not in _VALID_ODDS_SOURCES:
        # A typo here (e.g. "the-odds-ap1") must not silently fall back to
        # demo data -- that is exactly the kind of thing that would go
        # unnoticed once this runs unattended.
        raise ValueError(
            f"ODDS_SOURCE={odds_source!r} must be one of {_VALID_ODDS_SOURCES}."
        )

    return AppConfig(
        db_path=os.environ.get("DB_PATH", str(DEFAULT_DB_PATH)),
        odds_source=odds_source,
        sport_key=os.environ.get("ODDS_SPORT_KEY", "soccer_epl"),
        odds_api_mode=os.environ.get("ODDS_API_MODE", "auto"),
        odds_api_capture_dir=os.environ.get("ODDS_API_CAPTURE_DIR"),
        mozzart_capture_dir=os.environ.get("MOZZART_CAPTURE_DIR"),
        mozzart_mode=os.environ.get("MOZZART_MODE", "manual"),
        min_value_gap_percent=Decimal(os.environ.get("MIN_VALUE_GAP_PERCENT", "15.0")),
        signal_ttl=timedelta(hours=float(os.environ.get("SIGNAL_TTL_HOURS", "3"))),
        api_football_key=os.environ.get("API_FOOTBALL_KEY"),
    )
