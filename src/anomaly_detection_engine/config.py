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

# Project root's .env -- see load_dotenv() below. Already covered by
# .gitignore's ".env" entry, so a real one sitting here never gets
# committed by accident.
DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_VALID_ODDS_SOURCES = ("demo", "the-odds-api", "api-football")


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

    max_quote_age/max_observation_spread are the production
    FreshnessPolicy thresholds (see pipeline.resolve_freshness_policy) --
    used for every non-demo odds_source. The demo JSON path keeps its
    own fixed, tight DEMO_FRESHNESS_POLICY (pipeline.py) regardless of
    these fields, since it replays static timestamps rather than real
    wall-clock polling. There is no one correct default for real
    sources: it depends on how far apart a deployment's actual
    POLL_INTERVAL_SECONDS is and how stale a given provider's own
    "update" timestamp tends to be by the time it's fetched -- these
    exist to be tuned per deployment, not treated as universal
    constants.
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
    max_quote_age: timedelta
    max_observation_spread: timedelta
    # None means "not configured" -- TheOddsApiCollector itself raises
    # if it ends up unset when actually needed. Kept here (not just read
    # by the collector directly) so the-odds-api's key comes through the
    # same config boundary api_football_key already does: .env/
    # environment -> load_config() -> AppConfig -> collectors, one place
    # to see every real credential this app can be given, not just some
    # of them.
    odds_api_key: str | None
    # None means "not configured". api-football.com can be either the
    # opt-in supplemental source it started as (see
    # pipeline._supplemental_collectors, same shape as
    # mozzart_capture_dir above) or the primary one (ODDS_SOURCE=
    # api-football, see pipeline.build_collectors) -- either way this
    # field is where the key comes from; ApiFootballCollector itself
    # raises if it ends up unset when actually needed.
    api_football_key: str | None


def load_dotenv(path: Path = DEFAULT_DOTENV_PATH) -> None:
    """Loads KEY=value pairs from a .env file into os.environ, filling in
    only variables not already set there -- a real environment variable
    (the shell, a process manager, CI) always wins over whatever the file
    says, the same precedence every dotenv-style tool uses. A missing
    file is not an error: .env is entirely optional, every existing
    *_KEY/*_CAPTURE_DIR field already works from real env vars alone.

    Exists for the case this project is now actually meant to scale to
    -- many real providers, each with its own API key, none of which fit
    comfortably as `$env:` exports retyped into every new terminal.  One
    gitignored file with as many `PROVIDER_KEY=...` lines as needed,
    loaded once at startup, both fixes that and keeps every existing
    env-var-only workflow (CI, a container's own env, `$env:` set by
    hand for a one-off run) working unchanged.

    Deliberately NOT called from load_config() itself: load_config()
    must stay a pure read of whatever os.environ already holds at the
    moment it runs, so the existing test suite (which explicitly
    clears/sets specific env vars via monkeypatch immediately before
    calling load_config()) keeps working regardless of whether the
    developer running the tests happens to have a real .env file sitting
    in the repo root -- load_config() itself never touches the
    filesystem. Every real entrypoint (app.py, poller.py) calls this
    once, explicitly, before load_config().

    Deliberately minimal -- no variable expansion, no multi-line values,
    just KEY=value lines (blank lines and #-comments skipped, one layer
    of matching quotes stripped from the value): this project's
    dependency-free philosophy (the only *runtime* dependency is
    rapidfuzz) doesn't justify a python-dotenv dependency for a handful
    of lines.
    """
    if not path.exists():
        return

    # utf-8-sig, not utf-8: several common Windows editors/tools
    # (PowerShell's own `Set-Content -Encoding utf8`, Notepad's "UTF-8")
    # write a leading BOM. Plain utf-8 leaves that BOM character
    # attached to the first line's key, silently producing a key like
    # "﻿API_FOOTBALL_KEY" that never matches what the rest of the
    # app reads via os.environ.get("API_FOOTBALL_KEY") -- verified
    # directly: this exact bug, on this exact file-writing path.
    # utf-8-sig strips the BOM if present and is otherwise identical to
    # utf-8 for a file that has none.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if not value:
            # A blank RHS ("DB_PATH=", left that way in .env.example for
            # every field with no natural non-secret default) must mean
            # "not set", the same as the line being absent entirely --
            # not "set to the empty string". Without this,
            # os.environ.get(key, default) in load_config() would return
            # "" (the var IS present) instead of falling through to
            # load_config()'s own default, silently discarding it. Hit
            # exactly this way for db_path: "" is a valid sqlite3.connect()
            # path (a private on-disk temp db, deleted on close), so a
            # blank DB_PATH= line silently produced a throwaway database
            # instead of the real persistent DEFAULT_DB_PATH -- caught
            # live, running a real soak-test setup.
            continue

        os.environ.setdefault(key, value)


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
        max_quote_age=timedelta(minutes=float(os.environ.get("MAX_QUOTE_AGE_MINUTES", "60"))),
        max_observation_spread=timedelta(
            minutes=float(os.environ.get("MAX_QUOTE_SPREAD_MINUTES", "30"))
        ),
        odds_api_key=os.environ.get("ODDS_API_KEY"),
        api_football_key=os.environ.get("API_FOOTBALL_KEY"),
    )
