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
    -SkipIfRunning below, scripts/poller_watchdog.ps1 (periodic liveness
    check and auto-restart), and the project's own notes on Task
    Scheduler / Windows "Efficiency Mode" background-app settings as the
    other layers of the real fix, none of which this script (a plain
    user-level process launch) provides on its own. stdout/stderr
    redirection to files works identically regardless of which
    executable is used -- Python's sys.stdout/stderr are the same
    standard handles either way.

    Same cadence math as run_soak_test.ps1 (see that script's own
    .DESCRIPTION for the ~5-requests/cycle, 100/day budget reasoning) --
    kept in sync manually since this script's whole reason to exist
    (detached, no foreground console) is different from that one's
    (interactive, Ctrl+C-able, console-visible) despite launching the
    exact same poller.py.

    Does not survive a reboot on its own -- pair this with a Startup
    folder shortcut (see scripts/install_startup_shortcut.ps1) for
    every-login recovery, and scripts/poller_watchdog.ps1 run
    periodically (Task Scheduler, or a login-time loop) for the fix that
    also survives a mid-session hang/kill without needing a fresh login.

.PARAMETER SkipIfRunning
    Does nothing (exits 0) if poller.pid names a process that is both
    still alive AND actually running anomaly_detection_engine.poller
    (checked via its command line, not just its name) -- avoids stacking
    up a second poller (and doubling the day's API request budget) if
    this script runs again while an earlier instance is still alive.
    Deliberately not a plain "any python.exe/pythonw.exe process"
    check -- this machine may run unrelated Python processes (an IDE,
    another project, a one-off script), and matching on name alone
    already produced a false "already running" once. A stale/missing
    poller.pid, or one pointing at a PID that no longer exists or is no
    longer this poller, is treated as "not running" and a fresh instance
    is started. Always passed by the Startup folder shortcut and by
    poller_watchdog.ps1; off by default for a manual run, where a
    deliberate restart is normally exactly the point.

.EXAMPLE
    # API_FOOTBALL_KEY via .env (see .env.example) or already set in
    # this session's environment:
    .\scripts\start_background_poller.ps1
#>

param(
    [switch]$SkipIfRunning
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $repoRoot "poller.pid"

function Get-RunningPollerProcessId {
    # Returns the PID from poller.pid if -- and only if -- that PID still
    # exists AND its own command line still shows it running this
    # project's poller module, not just any process that happens to have
    # reused the PID since. $null otherwise (covers: no pid file, a PID
    # that's gone, or a PID reused by something unrelated).
    if (-not (Test-Path $pidFile)) {
        return $null
    }
    $recordedId = (Get-Content $pidFile -Raw).Trim()
    if (-not $recordedId) {
        return $null
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$recordedId" -ErrorAction SilentlyContinue
    if (-not $proc) {
        return $null
    }
    if ($proc.CommandLine -notmatch "anomaly_detection_engine\.poller") {
        return $null
    }
    return [int]$recordedId
}

if ($SkipIfRunning) {
    $runningId = Get-RunningPollerProcessId
    if ($runningId) {
        Write-Host "Poller already running (PID $runningId) -- not starting another."
        exit 0
    }
}

$env:ODDS_SOURCE = "api-football"
$env:POLL_INTERVAL_SECONDS = "5400"

$pythonwExe = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

$process = Start-Process -FilePath $pythonwExe `
    -ArgumentList "-u", "-m", "anomaly_detection_engine.poller" `
    -RedirectStandardOutput (Join-Path $repoRoot "poller.log") `
    -RedirectStandardError (Join-Path $repoRoot "poller.err.log") `
    -WorkingDirectory $repoRoot `
    -PassThru

Set-Content -Path $pidFile -Value $process.Id -NoNewline

Write-Host "Started poller (pythonw.exe, PID $($process.Id)) -- logging to poller.log/poller.err.log"
Write-Host "Stop it with: Stop-Process -Id $($process.Id)"
