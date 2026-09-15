<#
.SYNOPSIS
    Launches poller.py as a detached background process, independent of
    this PowerShell session/terminal window.

.DESCRIPTION
    Uses pythonw.exe rather than python.exe purely to avoid an extra
    always-visible console window -- NOT a fix for anything. This
    project's soak testing has seen Windows' AppHangXProcB1 detector
    kill both python.exe AND pythonw.exe background instances alike
    after a long idle stretch between poll cycles, so switching
    executables does not by itself solve unattended reliability -- see
    -SkipIfRunning below and the project's own notes on Task Scheduler /
    Windows "Efficiency Mode" background-app settings as the real fixes,
    neither of which this script (a plain user-level process launch) can
    provide on its own. stdout/stderr redirection to files works
    identically regardless of which executable is used -- Python's
    sys.stdout/stderr are the same standard handles either way.

    Same cadence math as run_soak_test.ps1 (see that script's own
    .DESCRIPTION for the ~5-requests/cycle, 100/day budget reasoning) --
    kept in sync manually since this script's whole reason to exist
    (detached, no foreground console) is different from that one's
    (interactive, Ctrl+C-able, console-visible) despite launching the
    exact same poller.py.

    Does not survive a reboot on its own -- pair this with a Startup
    folder shortcut (see scripts/install_startup_shortcut.ps1) for
    every-login recovery, or Task Scheduler (Register-ScheduledTask
    under a normal user account, no elevated rights needed) for the
    more complete fix that also survives mid-session kills.

.PARAMETER SkipIfRunning
    Does nothing (exits 0) if a pythonw.exe or python.exe process is
    already running -- avoids stacking up a second poller (and doubling
    the day's API request budget) if this script runs again while an
    earlier instance is still alive. Always passed by the Startup folder
    shortcut; off by default for a manual run, where a deliberate
    restart is normally exactly the point.

.EXAMPLE
    # API_FOOTBALL_KEY via .env (see .env.example) or already set in
    # this session's environment:
    .\scripts\start_background_poller.ps1
#>

param(
    [switch]$SkipIfRunning
)

if ($SkipIfRunning) {
    $existing = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Poller already running (PID $($existing.Id -join ', ')) -- not starting another."
        exit 0
    }
}

$env:ODDS_SOURCE = "api-football"
$env:POLL_INTERVAL_SECONDS = "5400"

$repoRoot = Join-Path $PSScriptRoot ".."
$pythonwExe = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

$process = Start-Process -FilePath $pythonwExe `
    -ArgumentList "-u", "-m", "anomaly_detection_engine.poller" `
    -RedirectStandardOutput (Join-Path $repoRoot "poller.log") `
    -RedirectStandardError (Join-Path $repoRoot "poller.err.log") `
    -WorkingDirectory $repoRoot `
    -PassThru

Write-Host "Started poller (pythonw.exe, PID $($process.Id)) -- logging to poller.log/poller.err.log"
Write-Host "Stop it with: Stop-Process -Id $($process.Id)"
