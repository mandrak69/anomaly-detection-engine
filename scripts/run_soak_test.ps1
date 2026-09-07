<#
.SYNOPSIS
    Launches the continuous poller (poller.py) against api-football.com
    as the sole primary source, at a poll cadence sized for the free
    tier's 100 requests/day limit.

.DESCRIPTION
    Cadence math: each poll cycle costs 2 requests (one /fixtures, one
    /odds -- see ApiFootballCollector.collect()), assuming neither
    response actually paginates on a given day (true for every response
    seen so far in this project). 1800s (30 min) intervals give
    86400 / 1800 = 48 cycles/day * 2 requests = 96 requests/day, under
    the 100/day limit with a small margin for the odd extra request.
    Tighten this (a lower POLL_INTERVAL_SECONDS) only if the plan
    actually allows more.

    API_FOOTBALL_KEY is never set or hardcoded here -- provide it either
    as a real environment variable in this session, or in a .env file at
    the repo root (config.load_dotenv(), loaded automatically by
    poller.py itself; see .env.example). This script does not check
    which one you used -- if neither is set, ApiFootballCollector raises
    its own clear error on the very first cycle. Never paste a real key
    into a chat session or a git-tracked file.

.EXAMPLE
    $env:API_FOOTBALL_KEY = "<your key>"
    .\scripts\run_soak_test.ps1

.EXAMPLE
    # Or put API_FOOTBALL_KEY=<your key> in a .env file at the repo root
    # (copy .env.example) and just run:
    .\scripts\run_soak_test.ps1
#>

$env:ODDS_SOURCE = "api-football"
$env:POLL_INTERVAL_SECONDS = "1800"

Write-Host "Starting poller: ODDS_SOURCE=api-football, POLL_INTERVAL_SECONDS=1800 (48 cycles/day)"
Write-Host "DB: data\anomaly_detection.db (default DB_PATH) -- logging to poller.log and this console"
Write-Host "Stop with Ctrl+C (handled cleanly -- the current cycle finishes before exiting)."
Write-Host ""

# Uses this repo's own venv directly (not a bare `python`), so this
# works whether or not the venv is already activated in this session.
$pythonExe = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
& $pythonExe -m anomaly_detection_engine.poller | Tee-Object -FilePath poller.log
