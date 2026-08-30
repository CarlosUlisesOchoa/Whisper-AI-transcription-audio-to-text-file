#Requires -RunAsAdministrator
# Registers the whisper-watcher as a Windows Scheduled Task that starts at logon,
# running as the interactive user (Google Drive mounts G:\ per-user session, so
# this must NOT run as SYSTEM). Run once from an elevated PowerShell.

param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$VenvPythonw = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\pythonw.exe"),
    [string]$TaskName = "WhisperWatcher"
)

if (-not (Test-Path $VenvPythonw)) {
    Write-Error "pythonw.exe not found at '$VenvPythonw'. Pass -VenvPythonw if your venv lives elsewhere."
    exit 1
}

# pywin32 must be installed INTO THIS VENV, not user-site — the scheduled task launches
# pythonw.exe from here directly, so a user-site install (pip install --user) is invisible
# to it. Without it, watcher.py's Explorer/COM staleness refresh silently degrades to a
# plain os.walk on every run (see dev-docs/watcher-deadline-aware-rescan.md). Fail loud here
# instead of registering a half-broken task.
$VenvPython = Join-Path (Split-Path -Parent $VenvPythonw) "python.exe"
& $VenvPython -c "import win32com.client" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "pywin32 is not installed in the venv at '$VenvPython'. Install it with:`n    $VenvPython -m pip install pywin32"
    exit 1
}

$watcherScript = Join-Path $RepoRoot "watcher.py"
if (-not (Test-Path $watcherScript)) {
    Write-Error "watcher.py not found at '$watcherScript'."
    exit 1
}

# watcher.py already reconfigures stdout to UTF-8 defensively, but set it at the
# user level too, matching the same PYTHONUTF8=1 requirement as the CLI.
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")

$action = New-ScheduledTaskAction -Execute $VenvPythonw -Argument "`"$watcherScript`"" -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -RunOnlyIfNetworkAvailable:$false `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Watches configured Google Drive folders and submits new audio to whisper-api for transcription." `
    -Force

Write-Host "Registered scheduled task '$TaskName'. It will run at your next logon."
Write-Host "To start it immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Log file: `$env:LOCALAPPDATA\whisper-watcher\watcher.log"
