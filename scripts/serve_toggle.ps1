# Pandora's Box Mk1 — serve / stop the hub over the Wi-Fi hotspot (F8).
#
# The Field power plan kills Wi-Fi/Bluetooth on battery — but serving
# IS Wi-Fi. This script is the mechanism the audit found missing: it
# switches plans when serving starts and back when it stops, and checks
# the hotspot network profile (Windows must classify it Private, or the
# firewall blocks the hub despite the "allow on private" prompt).
#
#   .\serve_toggle.ps1 -Start    switch to Performance, start the hotspot,
#                                check the network profile + firewall, print the URL
#   .\serve_toggle.ps1 -Stop     stop the hotspot, switch back to Field
#
# Run from an elevated PowerShell for the plan switch and the profile
# fix (powercfg /setactive and Set-NetConnectionProfile need admin).
# The hotspot toggle itself uses the WinRT tethering API; if that API
# call fails on this build, the script says so and prints the manual
# route (Settings > Network & internet > Mobile hotspot) instead of
# pretending it worked — the power-plan switch still happens either way.
#
# The hub itself must already be running (run_hub.bat --serve, or the
# logon autostart task). This script does not start the hub.

param(
    [switch]$Start,
    [switch]$Stop
)

$ErrorActionPreference = "Continue"

function Get-PlanGuid {
    param([string]$Name)
    # powercfg /list output: "Power Scheme GUID: <guid>  (<Name>) [*]"
    $lines = powercfg /list
    foreach ($line in $lines) {
        if ($line -match "Power Scheme GUID:\s*([0-9a-f-]+)\s+\(([^)]+)\)") {
            if ($Matches[2] -eq $Name) { return $Matches[1] }
        }
    }
    return $null
}

function Set-Plan {
    param([string]$Name, [string]$FallbackName)
    $guid = Get-PlanGuid -Name $Name
    if (-not $guid -and $FallbackName) {
        Write-Host "  Plan '$Name' not found — falling back to '$FallbackName'." -ForegroundColor Yellow
        $guid = Get-PlanGuid -Name $FallbackName
        $Name = $FallbackName
    }
    if (-not $guid) {
        Write-Host "  ERROR: no power plan named '$Name' found. Create it first" -ForegroundColor Red
        Write-Host "  (powercfg /duplicatescheme, or Control Panel > Power Options)."
        return $false
    }
    powercfg /setactive $guid
    Write-Host "  Active power plan: $Name"
    return $true
}

function Start-Hotspot {
    # WinRT NetworkOperatorTetheringManager — the programmatic equivalent
    # of the Settings > Mobile hotspot toggle. Needs a connection profile;
    # offline, the first profile is used (the hotspot works without
    # internet — that is the point). Any failure here is reported, not
    # hidden: the manual toggle is the fallback.
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
        $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                           $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
        $connType = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]
        $profiles = $connType::GetConnectionProfiles()
        if (-not $profiles -or $profiles.Count -eq 0) {
            throw "No connection profiles found — cannot start the hotspot this way."
        }
        $tetherType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
        $manager = $tetherType::CreateFromConnectionProfile($profiles[0])
        $op = $manager.StartTetheringAsync()
        $asTask = $asTaskGeneric.MakeGenericMethod([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
        $task = $asTask.Invoke($null, @($op))
        # Bounded wait (round 9): the unbounded $task.Wait() could hang
        # the script forever if the WinRT call never returns.
        if (-not $task.Wait(30000)) {
            throw "Tethering call timed out after 30s."
        }
        $result = $task.Result
        if ($result.Status -ne "Success") {
            throw "Tethering returned status: $($result.Status) ($($result.AdditionalErrorMessage))"
        }
        Write-Host "  Mobile hotspot started."
        return $true
    } catch {
        Write-Host "  Hotspot API call failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "  Manual route: Settings > Network & internet > Mobile hotspot > On."
        Write-Host "  Then re-run the profile check below by hand, or re-run this script."
        return $false
    }
}

function Stop-Hotspot {
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
        $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                           $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
        $connType = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]
        $profiles = $connType::GetConnectionProfiles()
        if (-not $profiles -or $profiles.Count -eq 0) { return $false }
        $tetherType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
        $manager = $tetherType::CreateFromConnectionProfile($profiles[0])
        $op = $manager.StopTetheringAsync()
        $asTask = $asTaskGeneric.MakeGenericMethod([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
        $task = $asTask.Invoke($null, @($op))
        if (-not $task.Wait(30000)) {
            throw "Tethering stop call timed out after 30s."
        }
        Write-Host "  Mobile hotspot stopped."
        return $true
    } catch {
        Write-Host "  Hotspot stop failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "  Manual route: Settings > Network & internet > Mobile hotspot > Off."
        return $false
    }
}

function Test-HotspotProfile {
    # The hotspot network is usually 192.168.137.1. If Windows classified
    # it Public, the firewall blocks the hub even though the "allow on
    # private" prompt was answered. This is the setup-time check F8 asks
    # for: run it once after the first hotspot start and confirm Private.
    Write-Host "[serve] Checking hotspot network profile..."
    Start-Sleep -Seconds 3
    try {
        $profiles = Get-NetConnectionProfile | Where-Object {
            $_.IPv4Address -like "192.168.137.*" -or $_.Name -like "*hotspot*"
        }
        if (-not $profiles) {
            # Fall back: any profile on the Wi-Fi Direct virtual adapter.
            $profiles = Get-NetConnectionProfile | Where-Object {
                $_.InterfaceAlias -like "*Wi-Fi Direct*" -or $_.InterfaceAlias -like "*Hotspot*"
            }
        }
        if (-not $profiles) {
            Write-Host "  Could not find the hotspot network profile — check by hand:" -ForegroundColor Yellow
            Write-Host "  Settings > Network & internet > Wi-Fi > the hotspot network"
            Write-Host "  must say Private, not Public."
            return
        }
        foreach ($p in $profiles) {
            Write-Host "  Network '$($p.Name)' on '$($p.InterfaceAlias)': $($p.NetworkCategory)"
            if ($p.NetworkCategory -ne "Private") {
                Write-Host "  WARNING: hotspot network is $($p.NetworkCategory), not Private." -ForegroundColor Red
                Write-Host "  The firewall will block the hub. Fix (elevated):"
                Write-Host "    Set-NetConnectionProfile -InterfaceAlias '$($p.InterfaceAlias)' -NetworkCategory Private"
            } else {
                Write-Host "  Private — the hub is reachable from the phone."
            }
        }
    } catch {
        Write-Host "  Profile check failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Get-HubPort {
    # The hub writes its actual port to hub.port at startup (round 9) —
    # it may have fallen back from 8899 if that port was busy. Fall
    # back to config.json, then to the default 8899.
    $hubFolder = Split-Path -Parent $PSScriptRoot
    $portFile = Join-Path $hubFolder "hub.port"
    if (Test-Path $portFile) {
        $port = (Get-Content $portFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($port -match '^\d+$' -and [int]$port -gt 0) { return [int]$port }
    }
    $configFile = Join-Path $hubFolder "config.json"
    if (Test-Path $configFile) {
        try {
            $cfg = Get-Content $configFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($cfg.port -and [int]$cfg.port -gt 0) { return [int]$cfg.port }
        } catch { }
    }
    return 8899
}

function Test-HubUp {
    $port = Get-HubPort
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/status" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        Write-Host "  Hub is up on 127.0.0.1:$port."
        return $true
    } catch {
        Write-Host "  Hub is NOT answering on 127.0.0.1:$port." -ForegroundColor Yellow
        Write-Host "  Start it first: run_hub.bat --serve (or the logon autostart task)."
        return $false
    }
}

function Test-HubOnHotspot {
    # Audit 2026-11-20 (F17): Test-HubUp only proves localhost. The phone
    # URL is only real if the hub answers on the hotspot IP — a
    # localhost-bound hub prints a dead phone URL. Probe the hotspot
    # address and say so plainly when it does not answer there.
    $port = Get-HubPort
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "*Wi-Fi Direct*" -ErrorAction SilentlyContinue |
           Select-Object -First 1 -ExpandProperty IPAddress)
    if (-not $ip) { $ip = "192.168.137.1" }
    try {
        $resp = Invoke-WebRequest -Uri "http://${ip}:${port}/api/status" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        Write-Host "  Hub answers on the hotspot address ${ip}:${port}."
        return $true
    } catch {
        Write-Host "  Hub does NOT answer on the hotspot address ${ip}:${port}." -ForegroundColor Yellow
        Write-Host "  It is localhost-only. Relaunch it with --serve so the phone can reach it:"
        Write-Host "    run_hub.bat --serve"
        return $false
    }
}

if ($Start -and $Stop) {
    Write-Host "Pick one: -Start or -Stop, not both." -ForegroundColor Red
    exit 1
}
if (-not $Start -and -not $Stop) {
    Write-Host "Usage: .\serve_toggle.ps1 -Start   (serve the hub to the phone)"
    Write-Host "       .\serve_toggle.ps1 -Stop    (stop serving, back to Field)"
    exit 1
}

if ($Start) {
    Write-Host "[serve] Starting serving mode..." -ForegroundColor Yellow
    Set-Plan -Name "Performance" -FallbackName "High performance" | Out-Null
    $hotspotOk = Start-Hotspot
    Test-HotspotProfile
    $hubOk = Test-HubUp
    $hotspotHubOk = $false
    if ($hubOk) { $hotspotHubOk = Test-HubOnHotspot }
    Write-Host ""
    if ($hotspotOk -and $hubOk -and $hotspotHubOk) {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "*Wi-Fi Direct*" -ErrorAction SilentlyContinue |
               Select-Object -First 1 -ExpandProperty IPAddress)
        if (-not $ip) { $ip = "192.168.137.1" }
        $port = Get-HubPort
        Write-Host "[serve] On the phone, open:  http://${ip}:${port}/" -ForegroundColor Yellow
    } else {
        Write-Host "[serve] Serving is not fully up — see the warnings above." -ForegroundColor Yellow
    }
    Write-Host "[serve] When done: .\serve_toggle.ps1 -Stop  (hotspot off, back to Field)."
} else {
    Write-Host "[serve] Stopping serving mode..." -ForegroundColor Yellow
    Stop-Hotspot | Out-Null
    Set-Plan -Name "Field" -FallbackName "Balanced" | Out-Null
    Write-Host "[serve] Hotspot off, back to the Field plan."
}
