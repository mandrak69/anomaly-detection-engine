<#
.SYNOPSIS
    Launches poller.py as a detached background process, independent of
    this PowerShell session/terminal window.

.DESCRIPTION
    Uses pythonw.exe, not python.exe: a plain python.exe process still
    gets a (normally hidden) console window that pumps Windows messages,
    and a long-running background process that mostly just sleeps
    between poll cycles has been observed -- twice, in this project's own
    soak testing -- getting killed by Windows' AppHangXProcB1 detector,
    which flags a console app as "not responding" purely because nothing
    is servicing that message queue for an extended stretch. pythonw.exe
    has no console at all, so there is no window for that watchdog to
    judge unresponsive. stdout/stderr redirection to files still works
    identically either way -- Python's sys.stdout/stderr are the same
    standard handles regardless of whether a console is attached.

    Same cadence math as run_soak_test.ps1 (see that script's own
    .DESCRIPTION for the ~5-requests/cycle, 100/day budget reasoning) --
    kept in sync manually since this script's whole reason to exist
    (detached, no foreground console) is different from that one's
    (interactive, Ctrl+C-able, console-visible) despite launching the
    exact same poller.py.

    Does not survive a reboot or a manual Task Scheduler-based restart --
    this is still a plain background process, just one no longer subject
    to the specific AppHang failure mode above. Re-run this script by
    hand after a reboot, or set up Task Scheduler yourself (Register-
    ScheduledTask under a normal user account, "At log on"/"At startup"
    triggers, no elevated rights needed) if unattended reboot-survival
    matters more than the manual-restart cost.

.EXAMPLE
    # API_FOOTBALL_KEY via .env (see .env.example) or already set in
    # this session's environment:
    .\scripts\start_background_poller.ps1
#>

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
