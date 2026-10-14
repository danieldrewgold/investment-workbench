# Register the filing watcher with Windows Task Scheduler (free; no model calls).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1            # weekday release windows, publishes the site
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -NoPublish # local only, never pushes
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Remove
#
# Runs as you, only while you're logged in (so desktop alerts and git credentials work),
# with pythonw so no console window flashes. Output: data\watcher\watcher.log.

# Weekdays only, clustered on when press releases cross: pre-market (6:45-8:00) and
# after the close (4:00-5:00), plus a midday and an evening sweep (the evening run also
# does the daily light refresh). Times are local to this PC.
param(
    [string[]]$Times = @("6:45am", "7:00am", "7:15am", "7:30am", "7:45am", "8:00am",
                         "12:30pm",
                         "4:00pm", "4:15pm", "4:30pm", "4:45pm", "5:00pm",
                         "8:30pm"),
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
$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$trigger = $Times | ForEach-Object { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Polls SEC EDGAR for new filings on covered names, refreshes free data, republishes the demo site. No paid calls." `
    -Force | Out-Null

Write-Output "Registered '$TaskName': weekdays at $($Times -join ', '); flags: $flags"
Write-Output "Log: $Repo\data\watcher\watcher.log   Events: $Repo\data\watcher\events.jsonl"
