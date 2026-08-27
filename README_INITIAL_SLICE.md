# Initial Working Slice

This package is the first executable slice of the project.

## What it demonstrates

`raw odds -> normalization -> event matching -> best odds -> surebet analysis`

The implementation intentionally uses a local sample dataset. Real API/scraping collectors are deferred until the core domain and analysis behavior are stable.

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

## Current modules

- `models`: canonical event, team, bookmaker and odds snapshot models
- `normalization`: exact, alias and fuzzy team normalization
- `matching`: event matching by sport, league, teams and start-time tolerance
- `analysis.best_odds`: best price per outcome
- `analysis.arbitrage`: 1X2 surebet calculation
- `data/samples`: controlled test/demo input

## Next slice

1. Introduce raw collector DTO/interface.
2. Add first real data source.
3. Persist `OddsSnapshot` history.
4. Implement outlier detection.
5. Add structured reporting.
