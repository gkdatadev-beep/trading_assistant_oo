# =============================================================================
# start.ps1 - Trade Assistant OO startup watchdog
#
# 1. Ensures MetaTrader 5 is running (Start Menu shortcut first, exe fallback)
# 2. Creates/updates the .venv and installs dependencies
# 3. Starts the Flask app (port 8000) with self-healing and HTTP health checks
# 4. Monitors MT5 and the app; relaunches MT5 if the terminal dies
#    (the app reconnects to MT5 on its own - see app/core/broker.py)
#
# Run: powershell -ExecutionPolicy Bypass -File start.ps1
# =============================================================================

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = "$projectDir\.venv"
$logDir = "$projectDir\data"
$logFile = "$logDir\app.log"
$maxRestarts = 10
$restartWindowSeconds = 300
$restartCount = 0
$windowStart = Get-Date

# Health check settings
$healthUrl = "http://localhost:8000/api/health"
$healthCheckIntervalSeconds = 15
$maxHealthFailures = 3

# MT5 settings
$mt5ProcessName = "terminal64"
$mt5StartupWaitSeconds = 30
$mt5LoginGraceSeconds = 60
$mt5MaxStartAttempts = 3

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Write-Log {
    param($Message)
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts | $Message" | Out-File -FilePath $logFile -Append
    Write-Host "$ts | $Message"
}

# ---- MT5 helpers -----------------------------------------------------------

function Test-Mt5Running {
    $proc = Get-Process -Name $mt5ProcessName -ErrorAction SilentlyContinue
    return $null -ne $proc
}

function Find-Mt5Path {
    # Try registry first
    $regPaths = @(
        "HKLM:\SOFTWARE\MetaQuotes\MetaTrader 5",
        "HKLM:\SOFTWARE\WOW6432Node\MetaQuotes\MetaTrader 5"
    )
    foreach ($rp in $regPaths) {
        try {
            if (Test-Path $rp) {
                $installPath = (Get-ItemProperty -Path $rp -ErrorAction Stop).InstallPath
                if ($installPath) {
                    $candidate = Join-Path $installPath "terminal64.exe"
                    if (Test-Path $candidate) { return $candidate }
                }
            }
        }
        catch { }
    }

    # Fallback: common install paths
    $commonPaths = @(
        "${env:ProgramFiles(x86)}\MetaTrader 5\terminal64.exe",
        "${env:ProgramFiles}\MetaTrader 5\terminal64.exe",
        "$env:LOCALAPPDATA\Programs\MetaTrader 5\terminal64.exe"
    )
    foreach ($p in $commonPaths) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Start-Mt5 {
    if (Test-Mt5Running) {
        Write-Log "MT5 already running"
        return $true
    }

    # Preferred: launch via Start Menu shortcut (preserves full desktop env
    # and correct MT5 data folder resolution)
    $shortcutRoots = @(
        "$env:PROGRAMDATA\Microsoft\Windows\Start Menu\Programs",
        "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
    )
    $mt5Lnk = $null
    foreach ($sp in $shortcutRoots) {
        if (Test-Path $sp) {
            $mt5Lnk = Get-ChildItem -Path $sp -Filter "*MetaTrader 5*.lnk" -Recurse -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -notmatch 'Editor|Uninstall' } |
                      Select-Object -First 1
            if ($mt5Lnk) { break }
        }
    }

    if ($mt5Lnk) {
        Write-Log "Launching MT5 via shortcut: $($mt5Lnk.FullName)"
        try {
            $lnkPath = $mt5Lnk.FullName
            Start-Process -FilePath "cmd.exe" `
                          -ArgumentList "/c start `"`" `"$lnkPath`"" `
                          -WindowStyle Hidden
            Start-Sleep -Seconds $mt5StartupWaitSeconds
            if (Test-Mt5Running) {
                Write-Log "MT5 started successfully (shortcut)"
                return $true
            }
            Write-Log "MT5 not detected via shortcut - trying exe"
        }
        catch {
            Write-Log "Shortcut launch failed: $_"
        }
    }

    $foundPath = Find-Mt5Path
    if (-not $foundPath) {
        Write-Log "MT5 terminal64.exe NOT FOUND"
        return $false
    }

    Write-Log "Starting MT5 exe: $foundPath"
    try {
        $mt5Dir = Split-Path $foundPath -Parent
        Start-Process -FilePath $foundPath -WorkingDirectory $mt5Dir -WindowStyle Normal
        Start-Sleep -Seconds $mt5StartupWaitSeconds
    }
    catch {
        Write-Log "Failed to start MT5 exe: $_"
        return $false
    }

    if (Test-Mt5Running) {
        Write-Log "MT5 started successfully (exe)"
        return $true
    }
    Write-Log "WARNING: MT5 process not detected yet - may still be loading"
    return $false
}

function Ensure-Mt5Running {
    # MT5 already up - no login wait needed
    if (Test-Mt5Running) {
        Write-Log "MT5 already running - no login wait needed"
        return $true
    }

    $justStarted = $false
    for ($attempt = 1; $attempt -le $mt5MaxStartAttempts; $attempt++) {
        Write-Log "MT5 not running - start attempt $attempt/$mt5MaxStartAttempts"
        if (Start-Mt5) {
            $justStarted = $true
            break
        }
        Start-Sleep -Seconds 10
    }

    if (Test-Mt5Running) {
        if ($justStarted) {
            Write-Log "MT5 running - waiting $mt5LoginGraceSeconds s for auto-login"
            Start-Sleep -Seconds $mt5LoginGraceSeconds
        }
        else {
            Write-Log "MT5 running (started externally)"
        }
        return $true
    }

    Write-Log "WARNING: MT5 not running after $mt5MaxStartAttempts attempts - app will reconnect on its own"
    return $false
}

Write-Log "============================================"
Write-Log "Trade Assistant OO starting..."
Write-Log "Project: $projectDir"
try { $host.UI.RawUI.WindowTitle = "Trade Assistant OO Watchdog" } catch { }

# ---- Activate or create virtual environment ----
if (-not (Test-Path "$venvDir\Scripts\python.exe")) {
    Write-Log "Creating virtual environment in .venv ..."
    python -m venv "$venvDir"
    if (-not (Test-Path "$venvDir\Scripts\python.exe")) {
        Write-Log "FATAL: Failed to create venv"
        exit 1
    }
    Write-Log "Virtual environment created"
}

$pythonExe = "$venvDir\Scripts\python.exe"
Write-Log "Using Python: $pythonExe"

# ---- Install / update dependencies ----
$reqFile = "$projectDir\requirements.txt"
if (Test-Path $reqFile) {
    Write-Log "Installing dependencies from requirements.txt ..."
    & $pythonExe -m pip install -r "$reqFile" --quiet 2>&1 | Out-File "$logDir\pip.log"
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARNING: pip install had errors - check data/pip.log"
    }
    else {
        Write-Log "Dependencies OK"
    }
}
else {
    Write-Log "WARNING: requirements.txt not found - skipping pip install"
}

# ---- Kill any existing processes on port 8000 ----
$existing = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Log "Port 8000 in use - attempting to free"
    $proc = Get-Process -Id $existing.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

# ---- Ensure MT5 is running BEFORE the app starts ----
$null = Ensure-Mt5Running

# ---- Main loop with self-healing and HTTP health checks ----
try {
while ($true) {
    $elapsed = (Get-Date) - $windowStart
    if ($elapsed.TotalSeconds -gt $restartWindowSeconds) {
        $restartCount = 0
        $windowStart = Get-Date
    }
    if ($restartCount -ge $maxRestarts) {
        Write-Log "TOO MANY RESTARTS ($maxRestarts in $restartWindowSeconds sec) - aborting"
        break
    }

    Write-Log "Starting application (attempt $($restartCount + 1))"
    $restartCount++

    try {
        $processArgs = @("-m", "app.main")
        $process = Start-Process -FilePath $pythonExe -ArgumentList $processArgs -PassThru -NoNewWindow -WorkingDirectory $projectDir -RedirectStandardOutput "$logDir\stdout.log" -RedirectStandardError "$logDir\stderr.log"

        Write-Log "Process started (PID: $($process.Id))"

        # Wait for app to start accepting connections (up to 90s - the app can
        # spend ~60s inside mt5.initialize() IPC timeout at boot)
        $startupTimeout = 90
        $appReady = $false
        for ($i = 0; $i -lt $startupTimeout; $i++) {
            Start-Sleep -Seconds 1
            if ($process.HasExited) {
                Write-Log "App exited during startup (code: $($process.ExitCode))"
                break
            }
            try {
                $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -ErrorAction SilentlyContinue
                if ($response.StatusCode -eq 200) {
                    $appReady = $true
                    Write-Log "App healthy and accepting connections after $($i+1)s"
                    break
                }
            }
            catch {
                # Not ready yet
            }
        }

        if ((-not $appReady) -and (-not $process.HasExited)) {
            Write-Log "WARNING: App started but health check not responding - continuing anyway"
        }

        # Health check monitoring loop
        $healthFailures = 0
        $mt5LoginWait = 0
        while (-not $process.HasExited) {
            Start-Sleep -Seconds $healthCheckIntervalSeconds

            try {
                $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -ErrorAction Stop
                $healthData = $response.Content | ConvertFrom-Json

                if (($response.StatusCode -eq 200) -and ($healthData.status -eq "healthy")) {
                    $healthFailures = 0
                    Write-Log "Health OK: uptime=$($healthData.uptime_seconds)s heartbeat=$($healthData.heartbeat_age_seconds)s ago"
                }
                elseif ($response.StatusCode -eq 503) {
                    $issuesList = @($healthData.issues)
                    # MT5 logged out while the terminal is alive: restarting the
                    # app will NOT help - the app reconnects on its own (it can
                    # now auto-login with the credentials from /parm).
                    if (($issuesList -contains "mt5_disconnected") -and (Test-Mt5Running)) {
                        $healthFailures = 0
                        $mt5LoginWait++
                        if (($mt5LoginWait % 4) -eq 1) {
                            Write-Log "MT5 running but not logged in - waiting for app auto-login/reconnect"
                        }
                    }
                    else {
                        $healthFailures++
                        $issues = $issuesList -join ", "
                        Write-Log "Health DEGRADED ($healthFailures/$maxHealthFailures): $issues"
                    }
                }
                else {
                    $healthFailures++
                    Write-Log "Health UNEXPECTED ($healthFailures/$maxHealthFailures): status=$($response.StatusCode)"
                }
            }
            catch {
                $healthFailures++
                Write-Log "Health CHECK FAILED ($healthFailures/$maxHealthFailures): $_"
            }

            if ($healthFailures -ge $maxHealthFailures) {
                Write-Log "CRITICAL: $maxHealthFailures consecutive health failures - restarting app"
                if (-not $process.HasExited) {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 2
                }
                break
            }

            # MT5 liveness check - relaunch terminal if it died
            # (the app reconnects to MT5 on its own)
            if (-not (Test-Mt5Running)) {
                Write-Log "MT5 terminal not running - relaunching"
                $null = Start-Mt5
            }
        }

        if ($process.HasExited) {
            $exitCode = $process.ExitCode
            Write-Log "App exited with code $exitCode"
        }
        else {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            Write-Log "App killed due to health check failures"
        }
    }
    catch {
        Write-Log "ERROR starting app: $_"
    }

    Write-Log "Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
}
catch {
    Write-Log "FATAL watchdog error: $_"
    exit 1
}

Write-Log "Watchdog exiting"
