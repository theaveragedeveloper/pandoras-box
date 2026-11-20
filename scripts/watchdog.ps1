# Pandora's Box Mk1 — Search Hub watchdog (round 9).
#
# The autostart task restarts the hub when it *exits* with an error,
# but a hub can also die silently — a hung process, a killed python,
# a port conflict after a bad shutdown — without the task noticing.
# The watchdog is the second layer: it polls /api/status and restarts
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
# How it restarts: it stops any python process running hub.py, then
# launches run_hub.bat --no-pause minimized (which also restarts
# kiwix-serve first). It never touches the config or the library.
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

function Test-HubAlive {
    param([int]$CheckPort)
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$CheckPort/api/status" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Restart-Hub {
    Write-WatchdogLog "[watchdog] Hub not answering — restarting it."
    # Stop any python running hub.py (the hung or orphaned hub). Only
    # processes whose command line mentions hub.py are touched.
    try {
        $hubProcs = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'hub\.py' }
        foreach ($p in $hubProcs) {
            Write-WatchdogLog "[watchdog] Stopping hub process $($p.ProcessId)."
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
    } catch {
        Write-WatchdogLog "[watchdog] Could not enumerate hub processes: $($_.Exception.Message)"
    }
    $launcher = Join-Path $HubFolder "run_hub.bat"
    if (-not (Test-Path $launcher)) {
        Write-WatchdogLog "[watchdog] ERROR: cannot find run_hub.bat in $HubFolder — not restarting."
        return
    }
    try {
        Start-Process -FilePath $launcher -ArgumentList "--no-pause" -WorkingDirectory $HubFolder -WindowStyle Minimized -ErrorAction Stop
        Write-WatchdogLog "[watchdog] Hub relaunched (run_hub.bat --no-pause)."
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

$checkPort = if ($Port -gt 0) { $Port } else { Get-HubPort }
Write-WatchdogLog "[watchdog] Watching the hub on 127.0.0.1:$checkPort (every ${IntervalSeconds}s, restart after $FailThreshold failed checks)."
$failures = 0
while ($true) {
    if (Test-HubAlive -CheckPort $checkPort) {
        if ($failures -gt 0) {
            Write-WatchdogLog "[watchdog] Hub is answering again after $failures failed check(s)."
        }
        $failures = 0
    } else {
        $failures++
        Write-WatchdogLog "[watchdog] Hub not answering (failure $failures of $FailThreshold)."
        if ($failures -ge $FailThreshold) {
            Restart-Hub
            $failures = 0
            # Give the relaunch time to come up before the next check.
            Start-Sleep -Seconds 10
        }
    }
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
$mutex.ReleaseMutex()
