<#
.SYNOPSIS
    Liveness check for the background poller: restarts it if it has
    crashed, or if it is still running but has gone silent for too long
    (a hung/frozen process, not a crashed one).

.DESCRIPTION
    Exists because a *running* poller process is not the same thing as a
    *working* one. This project has observed Windows silently freezing a
    background pythonw.exe poller (AppHangXProcB1, or an idle-throttling
    "Efficiency Mode" stall) for 24+ hours: Get-Process still reports it
    alive the whole time, but poller.err.log never gains a new line,
    because the process is stuck, not merely slow. start_background_
    poller.ps1's own -SkipIfRunning only answers "is a process alive",
    which is exactly the check that misses this failure mode -- this
    script adds the second, actually-diagnostic question: "has it logged
    a completed poll cycle recently."

    Staleness is judged from whichever is more recent: poller.err.log's
    last-write time, or the process's own start time (so a poller that
    was only just launched -- and hasn't had time to log its first cycle
    yet -- is never mistaken for a hang; it gets a full staleness window
    of grace from its own start, not from a previous run's stale log).

    A crashed poller (PID gone, or reused by an unrelated process -- see
    start_background_poller.ps1's own PID-validation logic, duplicated
    here) is restarted immediately regardless of log age; a hung poller
    (PID alive, log stale) is force-killed first, then restarted the
    same way.

    Intended to run periodically and unattended -- Task Scheduler under
    the current user account (no elevated rights needed for a "when
    logged on" trigger) if registration succeeds in this environment, a
    login-time loop otherwise. Safe to run at any time, including while
    the poller is healthy: it then does nothing but log one quiet "ok"
    line to poller_watchdog.log.

.PARAMETER StalenessSeconds
    How long poller.err.log may go without a new line before this script
    treats an alive process as hung rather than merely between cycles.
    Default (14400s = 4h) is roughly 2.5x this project's own configured
    POLL_INTERVAL_SECONDS (5400s) -- generous enough that one slow real
    cycle, or the daily burst-window transition, never triggers a false
    restart, while still catching a genuine hang well inside a single
    working day rather than after the 24h+ this project has actually
    seen one go unnoticed.

.EXAMPLE
    .\scripts\poller_watchdog.ps1
    # Custom staleness window:
    .\scripts\poller_watchdog.ps1 -StalenessSeconds 7200
#>

param(
    [int]$StalenessSeconds = 14400
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $repoRoot "poller.pid"
$errLog = Join-Path $repoRoot "poller.err.log"
$watchdogLog = Join-Path $repoRoot "poller_watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')  $Message"
    Add-Content -Path $watchdogLog -Value $line
}

function Get-RunningPollerProcess {
    # Same PID-plus-command-line validation as start_background_
    # poller.ps1's Get-RunningPollerProcessId, but returns the full
    # process object (this script also needs CreationDate, not just the
    # id) -- small deliberate duplication rather than cross-script
    # dot-sourcing, so either script stays independently runnable.
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
    return $proc
}

$launcher = Join-Path $PSScriptRoot "start_background_poller.ps1"
$proc = Get-RunningPollerProcess

if (-not $proc) {
    Write-WatchdogLog "no live poller found (crashed, never started, or PID reused) -- starting one"
    & $launcher
    exit 0
}

$logLastWrite = if (Test-Path $errLog) { (Get-Item $errLog).LastWriteTime } else { $proc.CreationDate }
$referenceTime = if ($logLastWrite -gt $proc.CreationDate) { $logLastWrite } else { $proc.CreationDate }
$ageSeconds = (Get-Date) - $referenceTime | ForEach-Object TotalSeconds

if ($ageSeconds -gt $StalenessSeconds) {
    Write-WatchdogLog (
        "poller PID $($proc.ProcessId) alive but silent for " +
        "$([math]::Round($ageSeconds / 60, 1)) min (threshold " +
        "$([math]::Round($StalenessSeconds / 60, 1)) min) -- killing and restarting"
    )
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    & $launcher
    exit 0
}

Write-WatchdogLog "ok: PID $($proc.ProcessId), last activity $([math]::Round($ageSeconds / 60, 1)) min ago"
