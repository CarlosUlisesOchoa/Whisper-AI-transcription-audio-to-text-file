# Stops watcher.py, however it happens to be running: via the WhisperWatcher Scheduled Task,
# or started manually in a terminal (`py watcher.py`). Does NOT unregister the Scheduled Task -
# it will start again at your next logon. Use unregister-watcher-task.ps1 for that.

param(
    [string]$TaskName = "WhisperWatcher"
)

$stoppedTask = $false
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($task) {
    if ($task.State -eq 'Running') {
        Write-Host "Stopping scheduled task '$TaskName'..."
        Stop-ScheduledTask -TaskName $TaskName
        $stoppedTask = $true
        Start-Sleep -Seconds 1
    } else {
        Write-Host "Scheduled task '$TaskName' exists but is not currently running (state: $($task.State))."
    }
} else {
    Write-Host "No scheduled task named '$TaskName' found - checking for a manually started process instead."
}

# Catch-all: a leftover process the task stop didn't clean up, or one started manually outside
# the task entirely. pythonw.exe/python.exe alone doesn't say which script, so match on command line.
$processes = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*watcher.py*' }

if (-not $processes) {
    if ($stoppedTask) {
        Write-Host "Watcher stopped."
    } else {
        Write-Host "No running watcher.py process found."
    }
    exit 0
}

foreach ($proc in $processes) {
    Write-Host "Killing PID $($proc.ProcessId): $($proc.CommandLine)"
    Stop-Process -Id $proc.ProcessId -Force
}

Write-Host "Watcher stopped."
