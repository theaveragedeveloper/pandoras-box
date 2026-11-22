# Pandora's Box Mk1 -- install the Search Hub as a logon autostart task.
# Creates a Windows Task Scheduler task that starts the hub when the owner logs
# in, so the search box is always there without remembering to launch it.
# Also offers a desktop shortcut to run_hub.bat as a manual alternative.
#
# Run from an elevated PowerShell (right-click > Run as Administrator) --
# Task Scheduler registration needs admin. The task runs run_hub.bat from
# this folder, minimized, only when the user is logged on (the hub has a
# console window; it is not a service). Delete it any time with:
#   Unregister-ScheduledTask -TaskName "Pandora's Box Mk1 Search Hub"
#
# First-run notes (also in README.md):
# - Windows Firewall will prompt the first time the hub binds -- allow it
#   on PRIVATE networks so the phone on the laptop hotspot can reach it.
#   Public-network access is not needed and should stay blocked.
# - The hub binds localhost-only (127.0.0.1:8899) by default; --serve
#   binds 0.0.0.0. The phone opens http://<laptop-hotspot-IP>:8899/
#   (usually 192.168.137.1). If 8899 is busy the hub takes the next
#   free port up to 8908 and records it in hub.port in the hub folder.
# - Field-plan hotspot exception: the Field power plan kills Wi-Fi, but
#   serving IS Wi-Fi -- use the Performance plan while serving, then switch
#   back to Field. See scripts/setup_hygiene.ps1.
# - Reliability (round 9): the task restarts the hub on failure (3 tries,
#   1 minute apart) and has no execution time limit, so it is not killed
#   after 3 days. run_hub.bat is launched with --no-pause so the task
#   sees the hub's real exit code -- without it, the trailing "pause"
#   keeps the task "running" forever and a crash never restarts.
#   For a second layer, also install the watchdog:
#     .\scripts\watchdog.ps1 -Install
#   It polls /api/status and restarts the hub if it stops answering.

param(
    [string]$HubFolder = (Split-Path -Parent $PSScriptRoot),
    [switch]$DesktopShortcut
)

$ErrorActionPreference = "Stop"
$taskName = "Pandora's Box Mk1 Search Hub"
$launcher = Join-Path $HubFolder "run_hub.bat"

if (-not (Test-Path $launcher)) {
    Write-Host "[autostart] Cannot find run_hub.bat in $HubFolder -- run this script from the search-hub\scripts folder." -ForegroundColor Red
    exit 1
}

Write-Host "[autostart] Installing logon task '$taskName'..."
Write-Host "  Hub folder: $HubFolder"

$action = New-ScheduledTaskAction -Execute $launcher -Argument "--no-pause" -WorkingDirectory $HubFolder
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "  Task installed. The hub will start at logon (console window, minimized)."
Write-Host "  Test it now: run run_hub.bat once and open http://localhost:8899/"

if ($DesktopShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut("$env:USERPROFILE\Desktop\Pandora's Box Mk1 Search Hub.lnk")
    $link.TargetPath = $launcher
    $link.WorkingDirectory = $HubFolder
    $link.Description = "Start the Pandora's Box Mk1 offline search hub"
    $link.Save()
    Write-Host "  Desktop shortcut created."
}

Write-Host ""
Write-Host "[autostart] Firewall reminder: on first run, allow the hub on PRIVATE"
Write-Host "  networks so the hotspot can reach it. To remove the task later:"
Write-Host "    Unregister-ScheduledTask -TaskName `"$taskName`""
Write-Host "[autostart] Done."
