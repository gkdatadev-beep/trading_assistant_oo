# =============================================================================
# install_startup.ps1 - Install Trade Assistant OO as a Windows startup task
#
# Creates a Task Scheduler task that runs start.ps1 (MT5 + Flask app watchdog)
# at user logon. Also removes the legacy "TradeAssistantWatchdog" task that
# pointed to the OLD project (trade_assistant\scripts\watchdog.ps1).
#
# Usage:
#   .\install_startup.ps1            # install
#   .\install_startup.ps1 -Remove    # remove this task
# =============================================================================
param(
    [switch]$Remove,
    [string]$TaskName = "TradeAssistantOOWatchdog",
    [string]$LegacyTaskName = "TradeAssistantWatchdog",
    [int]$StartupDelaySeconds = 30
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $ProjectDir "start.ps1"

function Remove-Task {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Task '$Name' removed."
    }
    else {
        Write-Host "Task '$Name' not found - nothing to remove."
    }
}

if ($Remove) {
    Remove-Task -Name $TaskName
    exit 0
}

if (-not (Test-Path $StartScript)) {
    Write-Error "Start script not found: $StartScript"
    exit 1
}

# Reinstall clean: remove previous versions of BOTH tasks
Remove-Task -Name $TaskName
Remove-Task -Name $LegacyTaskName

$ArgumentStr = "-ExecutionPolicy Bypass -File `"$StartScript`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ArgumentStr -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME" -RandomDelay (New-TimeSpan -Seconds $StartupDelaySeconds)

$principal = New-ScheduledTaskPrincipal -UserID "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries:$true `
    -DontStopIfGoingOnBatteries:$true `
    -StartWhenAvailable:$true `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew

$desc = "Trade Assistant OO - auto-start MT5 + Flask app on logon, crash recovery"

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $desc -Force

Write-Host "============================================"
Write-Host "Task '$TaskName' installed successfully!"
Write-Host "Project:   $ProjectDir"
Write-Host "Startup:   $StartScript"
Write-Host ""
Write-Host "Legacy task '$LegacyTaskName' removed (if present)."
Write-Host "The watchdog will start automatically at the next logon."
Write-Host "To test now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove:   .\install_startup.ps1 -Remove"
Write-Host "============================================"
