# Initial Working Slice

This package is the first executable slice of the project.

## What it demonstrates

`collect -> validate -> normalize -> match -> persist -> freshness check -> analyze -> report`

The demo defaults to a local sample dataset, run twice (a second poll
with moved odds so the movement report has something to show). A real
collector (`TheOddsApiCollector`, see README.md's Data Collection
section) is also available and wired into this same demo via
`ODDS_SOURCE`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run tests

```bash
pytest
```

## Run demo

```bash
python -m anomaly_detection_engine.app
```

To run it against the real odds source instead of the sample dataset,
set `ODDS_SOURCE=the-odds-api` and `ODDS_API_KEY=<your key>` (optionally
`ODDS_SPORT_KEY`, defaults to `soccer_epl`):

```bash
ODDS_SOURCE=the-odds-api ODDS_API_KEY=<your key> python -m anomaly_detection_engine.app
```

## Current modules

- `models`: canonical event, team, bookmaker, market, odds snapshot, collector run and raw payload models
- `normalization`: exact, alias and fuzzy team normalization
- `matching`: event matching by sport, league, teams and start-time tolerance
- `validation`: structural/semantic validation of raw odds before they reach matching
- `collectors`: `JsonOddsCollector` (local file), `TheOddsApiCollector` (public API), `MozzartFileCollector` (manual capture for a bot-protected source)
- `ingestion.service`: `OddsIngestionService` -- collect -> validate -> match -> persist, producing a `CollectorRun`
- `storage`: SQLite-backed `OddsRepository`, `CollectorRunRepository`, `RawPayloadRepository`
- `analysis`: best odds, arbitrage (surebet), freshness, rapid movement, outlier detection, bookmaker lag
- `reporting`: `opportunity_report` (SUREBET/VALUE_GAP), `movement_report`
- `observability`: structured JSON logging, in-process `IngestionMetrics`
- `data/samples`: controlled test/demo input (two files, simulating two poll cycles)

## Next slice

1. ~~Introduce raw collector DTO/interface.~~ done (`RawEventOdds`, `OddsCollector`).
2. ~~Add first real data source.~~ done (`TheOddsApiCollector`; also `MozzartFileCollector` for a source that can't be fetched automatically).
3. ~~Persist `OddsSnapshot` history.~~ done (`OddsRepository`).
4. ~~Implement outlier detection.~~ done (`analysis.outlier_detector`).
5. ~~Add structured reporting.~~ done (`reporting.opportunity_report`, `reporting.movement_report`).

Open: a web dashboard (the reports above are still text-only), and a persistent event/fixtures catalog (see architecture.md's Next Architectural Step).
