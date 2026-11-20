# Pandora's Box Mk1 — Search Hub watchdog (round 9).
#
# The autostart task restarts the hub when it *exits* with an error,
# but a hub can also die silently — a hung process, a killed python,
# a port conflict after a bad shutdown — without the task noticing.
# The watchdog is the second layer: it polls /api/health and restarts
# the hub if it stops answering.
#
#   .\watchdog.ps1                  loop forever (check every 30 s)
#   .\watchdog.ps1 -Once            one check, then exit (for testing)
#   .\watchdog.ps1 -Install         register as a logon scheduled task
#                                   (run as Administrator), then exit
#   .\watchdog.ps1 -IntervalSeconds 60 -FailThreshold 2
#
# How it decides the hub is down: 3 consecutive failed status checks
# (each with a 5 s timeout). One failed check is noise — a reboot, a
# slow Recoll query — so it never restarts on a single miss.
#
# How it restarts: it verifies the recorded PID, launch time, and
# script path before stopping that instance. The launcher keeps the
# recorded hotspot mode and checks for Kiwix during startup.
#
# Only one watchdog runs at a time (a named mutex): if a second copy
# starts, it prints a message and exits instead of double-restarting
# the hub.
#
# Windows-only. It writes to watchdog.log in the scripts folder.
# Run -Install from an elevated PowerShell; the task runs at logon
# with the hub's own autostart task.

param(
    [string]$HubFolder = (Split-Path -Parent $PSScriptRoot),
    [int]$IntervalSeconds = 30,
    [int]$FailThreshold = 3,
    [int]$Port = 0,
    [switch]$Once,
    [switch]$Install
)

$ErrorActionPreference = "Continue"
$logPath = Join-Path $PSScriptRoot "watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    try {
        Add-Content -Path $logPath -Value $line -ErrorAction Stop
    } catch {
        # Logging must never kill the watchdog.
    }
}

function Get-HubPort {
    # The hub writes its actual port to hub.port at startup (round 9).
    $portFile = Join-Path $HubFolder "hub.port"
    if (Test-Path $portFile) {
        $port = (Get-Content $portFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($port -match '^\d+$' -and [int]$port -gt 0) { return [int]$port }
    }
    $configFile = Join-Path $HubFolder "config.json"
    if (Test-Path $configFile) {
        try {
            $cfg = Get-Content $configFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($cfg.port -and [int]$cfg.port -gt 0) { return [int]$cfg.port }
        } catch { }
    }
    return 8899
}

function Get-HubInstance {
    try {
        $path = Join-Path $HubFolder "hub.port.json"
        $state = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($state.instance -match '^[a-f0-9]{32}$' -and
            $state.pid -gt 0 -and $state.port -ge 1 -and $state.port -le 65535 -and
            $state.host -is [string] -and $state.host -and
            $state.requested_port -ge 0 -and $state.requested_port -le 65535 -and
            $state.recorded_at -gt 0) {
            return $state
        }
    } catch { }
    return $null
}

function Test-HubAlive {
    param([int]$CheckPort, $Instance)
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$CheckPort/api/health" -MaximumRedirection 0 -TimeoutSec 5 -ErrorAction Stop
        return ($health.ok -eq $true -and $health.service -eq "pandora-search-hub" -and
                $health.port -eq $CheckPort -and
                ($null -eq $Instance -or $health.instance -eq $Instance.instance))
    } catch {
        return $false
    }
}

function Restart-Hub {
    param($Instance, [int]$CheckPort)
    $latest = Get-HubInstance
    if ($latest.instance -ne $Instance.instance -or
        (Test-HubAlive -CheckPort $CheckPort -Instance $Instance)) { return }

    $launchArgs = @("--no-pause")
    if ($null -ne $Instance) {
        if ($Instance.host -notin @("localhost", "127.0.0.1", "0.0.0.0")) {
            return
        }
        $launchArgs += @("--host", $Instance.host, "--port", "$([int]$Instance.requested_port)")
    } elseif ($Port -gt 0) {
        $launchArgs += @("--port", "$Port")
    }

    if ($null -ne $Instance) {
        try {
            $processId = [int]$Instance.pid
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
            if ($null -ne $process) {
                $scriptPath = [IO.Path]::GetFullPath((Join-Path $HubFolder "hub.py"))
                $scriptArg = '(?:^|\s)"?' + [regex]::Escape($scriptPath) + '"?(?:\s|$)'
                $started = ([DateTimeOffset]$process.CreationDate).ToUnixTimeMilliseconds() / 1000.0
                if ($process.Name -notmatch '^pythonw?\.exe$' -or
                    $process.CommandLine -notmatch $scriptArg -or
                    $started -gt [double]$Instance.recorded_at) {
                    Write-WatchdogLog "[watchdog] Recorded PID belongs to another launch. Leaving it alone."
                } else {
                    Write-WatchdogLog "[watchdog] Stopping unresponsive hub process $processId."
                    Stop-Process -Id $processId -Force -ErrorAction Stop
                }
            }
        } catch {
            Write-WatchdogLog "[watchdog] Could not verify or stop the recorded hub. Restart deferred."
            return
        }
    }
    $launcher = Join-Path $HubFolder "run_hub.bat"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        Write-WatchdogLog "[watchdog] Launcher missing. Restart deferred."
        return
    }
    try {
        Start-Process -FilePath $launcher -ArgumentList $launchArgs -WorkingDirectory $HubFolder -WindowStyle Minimized -ErrorAction Stop
        Write-WatchdogLog "[watchdog] Hub relaunched ($($launchArgs -join ' '))."
    } catch {
        Write-WatchdogLog "[watchdog] ERROR: could not relaunch the hub: $($_.Exception.Message)"
    }
}

if ($env:OS -ne "Windows_NT") {
    Write-Host "[watchdog] Windows-only — this machine is not Windows. Exiting."
    exit 1
}

if ($Install) {
    $taskName = "Pandora's Box Mk1 Search Hub Watchdog"
    $scriptPath = Join-Path $PSScriptRoot "watchdog.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`"" -WorkingDirectory $HubFolder
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "[watchdog] Task '$taskName' installed. It starts at logon and watches the hub."
    Write-Host "  To remove it: Unregister-ScheduledTask -TaskName `"$taskName`""
    exit 0
}

# One watchdog at a time: a second copy exits instead of racing the first.
$mutex = New-Object System.Threading.Mutex($false, "PandoraBoxMk1HubWatchdog")
if (-not $mutex.WaitOne(0)) {
    Write-Host "[watchdog] Another watchdog is already running — exiting."
    exit 0
}

Write-WatchdogLog "[watchdog] Checking hub liveness every ${IntervalSeconds}s."
$failures = 0
$lastTarget = ""
try {
    while ($true) {
        $instance = Get-HubInstance
        if ($null -ne $instance -and $instance.host -notin @("localhost", "127.0.0.1", "0.0.0.0")) {
            Write-WatchdogLog "[watchdog] This bind address does not support the loopback liveness check."
            exit 1
        }
        $checkPort = if ($null -ne $instance) { $instance.port } elseif ($Port -gt 0) { $Port } else { Get-HubPort }
        $target = "$checkPort/$($instance.instance)"
        if ($target -ne $lastTarget) {
            $lastTarget = $target
            $failures = 0
            Write-WatchdogLog "[watchdog] Watching http://127.0.0.1:$checkPort/."
        }
        $alive = Test-HubAlive -CheckPort $checkPort -Instance $instance
        if ($Once) {
            if ($alive) { exit 0 }
            Write-WatchdogLog "[watchdog] Hub liveness check failed."
            exit 1
        }
        if ($alive) {
            $failures = 0
        } else {
            $failures++
            Write-WatchdogLog "[watchdog] Hub not answering (failure $failures of $FailThreshold)."
            if ($failures -ge $FailThreshold) {
                Restart-Hub -Instance $instance -CheckPort $checkPort
                $failures = 0
                Start-Sleep -Seconds 10
            }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
