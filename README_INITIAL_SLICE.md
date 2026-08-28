# Initial Working Slice

This package is the first executable slice of the project.

## What it demonstrates

`raw odds -> normalization -> event matching -> best odds -> surebet analysis`

The demo defaults to a local sample dataset. A real collector (`TheOddsApiCollector`, see README.md's Data Collection section) is also available and wired into this same demo.

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

- `models`: canonical event, team, bookmaker and odds snapshot models
- `normalization`: exact, alias and fuzzy team normalization
- `matching`: event matching by sport, league, teams and start-time tolerance
- `analysis.best_odds`: best price per outcome
- `analysis.arbitrage`: 1X2 surebet calculation
- `data/samples`: controlled test/demo input

## Next slice

1. ~~Introduce raw collector DTO/interface.~~ done (`RawEventOdds`, `OddsCollector`).
2. ~~Add first real data source.~~ done (`TheOddsApiCollector`).
3. ~~Persist `OddsSnapshot` history.~~ done (`OddsRepository`).
4. ~~Implement outlier detection.~~ done (`analysis.outlier_detector`).
5. Add structured reporting.
