<#
.SYNOPSIS
    One-time setup: installs a shortcut in the current user's Startup
    folder so the poller relaunches automatically on every login.

.DESCRIPTION
    Partial mitigation, not a full fix, for this project's unattended-
    reliability problem (see start_background_poller.ps1's own
    .DESCRIPTION): Windows has been observed killing the poller process
    outright (AppHangXProcB1) after a long idle stretch between poll
    cycles, with no logout/reboot involved at all -- a Startup shortcut
    only recovers on the *next login*, so a kill that happens while the
    user stays logged in for days still means days of downtime until
    they next log out and back in (or reboot). It costs nothing to have
    alongside a real fix (Task Scheduler, or disabling Windows
    "Efficiency Mode"/background throttling for python.exe/pythonw.exe),
    and meaningfully shortens the worst case compared to no recovery
    mechanism at all, which is why it's worth installing regardless.

    Needs no elevated/admin rights -- the Startup folder is a plain
    per-user directory (shell:startup), unlike Task Scheduler
    registration, which this project's own tooling has hit Access
    Denied on in this environment.

    Idempotent: re-running this replaces the shortcut in place rather
    than accumulating duplicates.

.EXAMPLE
    .\scripts\install_startup_shortcut.ps1
#>

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$launcherScript = Join-Path $repoRoot "scripts\start_background_poller.ps1"
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "anomaly-detection-engine-poller.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherScript`" -SkipIfRunning"
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "anomaly-detection-engine: relaunch the odds poller if it isn't already running"
$shortcut.Save()

Write-Host "Installed: $shortcutPath"
Write-Host "Will run at every login: start_background_poller.ps1 -SkipIfRunning"
Write-Host "Remove it by deleting that .lnk file, or via: shell:startup"
