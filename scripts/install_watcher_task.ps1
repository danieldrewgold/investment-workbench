# Register the filing watcher with Windows Task Scheduler (free; no model calls).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1            # both tasks, nightly publishes the site
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -NoPublish # local only, never pushes
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Remove
#
# Two tasks, both run as you while you're logged in (desktop alerts and git credentials
# work) with pythonw so no console window flashes. Output: data\watcher\watcher.log.
#
#   Workbench Filing Watcher  weekdays, clustered on when press releases cross: pre-market
#                             (6:45-8:00) and after the close (4:00-5:00) plus a 12:30 sweep.
#                             ~12s a check; refreshes only names that filed; alerts. No publish.
#   Workbench Nightly         2:00am Tue-Sat (covers each weekday): light refresh of every
#                             name (~1 hour), then one publish. Wakes the PC if it's asleep.
# Times are local to this PC.

param(
    [string[]]$Times = @("6:45am", "7:00am", "7:15am", "7:30am", "7:45am", "8:00am",
                         "12:30pm",
                         "4:00pm", "4:15pm", "4:30pm", "4:45pm", "5:00pm"),
    [string]$NightlyAt = "2:00am",
    [switch]$NoPublish,
    [switch]$Remove
)

$DayTask = "Workbench Filing Watcher"
$NightTask = "Workbench Nightly"
$Repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    foreach ($t in $DayTask, $NightTask) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Output "Removed '$t'."
        }
    }
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

function New-WatcherSettings([switch]$Wake) {
    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    if ($Wake) { $s.WakeToRun = $true }
    return $s
}

# Daytime: check + per-name refresh + alert.
$weekdays = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$dayTriggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $_ }
$dayAction = New-ScheduledTaskAction -Execute $pythonw -Argument "scripts\watch_filings.py" -WorkingDirectory $Repo
Register-ScheduledTask -TaskName $DayTask -Action $dayAction -Trigger $dayTriggers -Settings (New-WatcherSettings) `
    -Description "Checks SEC EDGAR for new filings on covered names in the press-release windows; refreshes free data and alerts. No paid calls." `
    -Force | Out-Null

# Nightly: full light refresh, then publish (or not, with -NoPublish).
$nightArgs = if ($NoPublish) { "scripts\watch_filings.py --nightly --no-push" } else { "scripts\watch_filings.py --nightly" }
$nightTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday" -At $NightlyAt
$nightAction = New-ScheduledTaskAction -Execute $pythonw -Argument $nightArgs -WorkingDirectory $Repo
Register-ScheduledTask -TaskName $NightTask -Action $nightAction -Trigger $nightTrigger -Settings (New-WatcherSettings -Wake) `
    -Description "Nightly light refresh of every covered name, then one publish of the demo site. No paid calls." `
    -Force | Out-Null

Write-Output "Registered '$DayTask': weekdays at $($Times -join ', ')"
Write-Output "Registered '$NightTask': Tue-Sat at $NightlyAt ($nightArgs)"
Write-Output "Log: $Repo\data\watcher\watcher.log   Events: $Repo\data\watcher\events.jsonl"
