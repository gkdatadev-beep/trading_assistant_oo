# Startup script with self-healing for Trade Assistant OO
# Run this in PowerShell: .\start.ps1

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = "$projectDir\data"
$logFile = "$logDir\app.log"
$maxRestarts = 10
$restartWindowSeconds = 300  # 5 minutes
$restartCount = 0
$windowStart = Get-Date

# Ensure log directory exists
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Write-Log {
    param($Message)
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts | $Message" | Out-File -FilePath $logFile -Append
    Write-Host "$ts | $Message"
}

Write-Log "============================================"
Write-Log "Trade Assistant OO starting..."
Write-Log "Project: $projectDir"

# Kill any existing python processes on port 8000
$existing = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Log "Port 8000 in use — attempting to free"
    $proc = Get-Process -Id $existing.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

while ($true) {
    # Rate-limit restarts
    $elapsed = (Get-Date) - $windowStart
    if ($elapsed.TotalSeconds -gt $restartWindowSeconds) {
        $restartCount = 0
        $windowStart = Get-Date
    }
    if ($restartCount -ge $maxRestarts) {
        Write-Log "TOO MANY RESTARTS ($maxRestarts in $restartWindowSeconds sec) — aborting"
        break
    }

    Write-Log "Starting application (attempt $($restartCount + 1))"
    $restartCount++

    try {
        $process = Start-Process -FilePath "python" -ArgumentList "-m", "app.main" -PassThru -NoNewWindow -RedirectStandardOutput "$logDir\stdout.log" -RedirectStandardError "$logDir\stderr.log"
        
        Write-Log "Process started (PID: $($process.Id))"
        
        # Wait for process to exit or run for at least 10 seconds (quick crash detection)
        $process.WaitForExit(10000) | Out-Null
        
        if (-not $process.HasExited) {
            Write-Log "App stable — running (PID: $($process.Id))"
            # Wait indefinitely
            $process.WaitForExit()
        }
        
        $exitCode = $process.ExitCode
        Write-Log "App exited with code $exitCode"
        
    } catch {
        Write-Log "ERROR starting app: $_"
    }

    Write-Log "Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}

Write-Log "Watchdog exiting"
