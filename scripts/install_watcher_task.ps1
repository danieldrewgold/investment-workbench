# Register the filing watcher with Windows Task Scheduler (free; no model calls).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1            # every 15 min, publishes the site
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -NoPublish # local only, never pushes
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Remove
#
# Runs as you, only while you're logged in (so desktop alerts and git credentials work),
# with pythonw so no console window flashes. Output: data\watcher\watcher.log.

param(
    [int]$EveryMinutes = 15,
    [switch]$NoPublish,
    [switch]$Remove
)

$TaskName = "Workbench Filing Watcher"
$Repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'."
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$flags = "--daily-refresh"
if (-not $NoPublish) { $flags = "--publish $flags" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "scripts\watch_filings.py $flags" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Polls SEC EDGAR for new filings on covered names, refreshes free data, republishes the demo site. No paid calls." `
    -Force | Out-Null

Write-Output "Registered '$TaskName': every $EveryMinutes min, flags: $flags"
Write-Output "Log: $Repo\data\watcher\watcher.log   Events: $Repo\data\watcher\events.jsonl"
