<#
.SYNOPSIS
    Launches the continuous poller (poller.py) against api-football.com
    as the sole primary source, at a poll cadence sized for the free
    tier's 100 requests/day limit.

.DESCRIPTION
    Cadence math: observed production behavior shows each poll cycle
    costs ~5 requests (one /fixtures, plus /odds pagination -- the
    free plan caps pagination at page 3, so odds alone can cost up to
    3 requests -- see ApiFootballCollector.collect() and the
    api_football.pagination_capped_by_plan log event). 5400s (90 min)
    intervals give 86400 / 5400 = 16 cycles/day * 5 requests =
    ~80 requests/day, under the 100/day limit with margin. Tighten
    this (a lower POLL_INTERVAL_SECONDS) only if the plan actually
    allows more, or if pagination cost drops.

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
$env:POLL_INTERVAL_SECONDS = "5400"

Write-Host "Starting poller: ODDS_SOURCE=api-football, POLL_INTERVAL_SECONDS=5400 (16 cycles/day)"
Write-Host "DB: data\anomaly_detection.db (default DB_PATH) -- logging to poller.log and this console"
Write-Host "Stop with Ctrl+C (handled cleanly -- the current cycle finishes before exiting)."
Write-Host ""

# Uses this repo's own venv directly (not a bare `python`), so this
# works whether or not the venv is already activated in this session.
$pythonExe = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
& $pythonExe -m anomaly_detection_engine.poller | Tee-Object -FilePath poller.log
