#Requires -RunAsAdministrator
# Removes the whisper-watcher Scheduled Task registered by register-watcher-task.ps1.

param(
    [string]$TaskName = "WhisperWatcher"
)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'."
} else {
    Write-Host "No scheduled task named '$TaskName' found."
}
