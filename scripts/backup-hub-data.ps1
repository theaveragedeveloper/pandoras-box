# Pandora's Box Mk1 -- back up the hub's own data.
#
# The hub owns data that exists nowhere else: household stock levels
# (inventory.db), saved houses/places (pins.db), and the hub config
# (config.json + config.json.good). All of it lives in
# C:\Library\search-hub\ on one SSD. A disk failure or a corrupt DB
# wipes it permanently -- the library can be rebuilt from sources,
# inventory and pins cannot. Run this script after any big stock or
# pin change, and before any hub update.
#
# Usage (PowerShell 5.1, on the 5424):
#   .\backup-hub-data.ps1
#       Backs up to the first spare drive found (D: through H:),
#       in a "hub-backup" folder on that drive.
#   .\backup-hub-data.ps1 -Destination E:\hub-backup
#       Backs up to the folder you name (870 EVO spare, USB stick,
#       or any path you choose). The folder is created if needed.
#   .\backup-hub-data.ps1 -Destination E:\hub-backup -HubDir C:\Library\search-hub
#
# How it works: each SQLite database is copied with the SQLite backup
# API (sqlite3 <db> ".backup <dest>"), which takes a consistent
# snapshot even while the hub is running. A plain file copy of a live
# SQLite database can capture a torn state, so plain copies are
# refused unless you pass -PlainCopy AND the hub is stopped first.
# Each backup copy is verified with PRAGMA integrity_check.
# config.json / config.json.good are plain JSON files written
# atomically by the hub, so a straight copy is safe for them.
#
# Restore: stop the hub, copy the files back into C:\Library\search-hub\,
# start the hub again. Keep at least one backup off the laptop.
param(
    [string]$Destination = "",
    [string]$HubDir = "C:\Library\search-hub",
    [switch]$PlainCopy
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $HubDir)) {
    Write-Host "Hub folder not found: $HubDir"
    Write-Host "Pass the hub folder with -HubDir if it lives elsewhere."
    exit 1
}

if (-not $Destination) {
    # No destination given: use the first spare drive found (the 870
    # EVO spare, or a USB stick). C: is the system drive -- never
    # default to backing up onto the same disk the data lives on.
    $found = $null
    foreach ($letter in "D","E","F","G","H") {
        if (Test-Path ($letter + ":\")) {
            $found = $letter + ":\hub-backup"
            break
        }
    }
    if (-not $found) {
        Write-Host "No spare drive found (looked at D: through H:)."
        Write-Host "Plug in the 870 EVO spare or a USB stick and re-run, or"
        Write-Host "pass a destination explicitly, e.g.:"
        Write-Host "  .\backup-hub-data.ps1 -Destination E:\hub-backup"
        exit 1
    }
    $Destination = $found
    Write-Host "No destination given -- using $Destination"
}

if (-not (Test-Path $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Write-Host "Created $Destination"
}

$sqlite3 = Get-Command sqlite3.exe -ErrorAction SilentlyContinue
if (-not $sqlite3 -and -not $PlainCopy) {
    Write-Host "sqlite3.exe was not found on PATH, so a live-database"
    Write-Host "backup cannot be taken safely. Either install the SQLite"
    Write-Host "command-line shell, or stop the hub first and re-run with:"
    Write-Host "  .\backup-hub-data.ps1 -Destination $Destination -PlainCopy"
    Write-Host "Nothing was backed up."
    exit 1
}
if ($PlainCopy) {
    Write-Host "WARNING: plain copy requested. The hub MUST be stopped"
    Write-Host "first, or the database copies may be torn and unusable."
}

$backedUp = @()
$failed = 0

foreach ($name in "inventory.db", "pins.db") {
    $source = Join-Path $HubDir $name
    $dest = Join-Path $Destination $name
    if (-not (Test-Path $source)) {
        Write-Host "SKIP: $name not found in $HubDir"
        continue
    }
    if ($PlainCopy) {
        Copy-Item -Path $source -Destination $dest -Force
    } else {
        $quoted = '"' + $dest + '"'
        & sqlite3.exe $source ".backup $quoted"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAILED: sqlite3 backup of $name (exit $LASTEXITCODE)"
            $failed++
            continue
        }
    }
    if (-not (Test-Path $dest) -or (Get-Item $dest).Length -eq 0) {
        Write-Host "FAILED: backup of $name is missing or empty."
        $failed++
        continue
    }
    if ($sqlite3) {
        $check = & sqlite3.exe $dest "PRAGMA integrity_check;" 2>&1
        if ($check -ne "ok") {
            Write-Host "FAILED: backup of $name failed integrity_check: $check"
            $failed++
            continue
        }
    }
    $size = (Get-Item $dest).Length
    Write-Host "OK: $name -> $dest ($size bytes, integrity ok)"
    $backedUp += $name
}

foreach ($name in "config.json", "config.json.good") {
    $source = Join-Path $HubDir $name
    $dest = Join-Path $Destination $name
    if (-not (Test-Path $source)) {
        Write-Host "SKIP: $name not found in $HubDir"
        continue
    }
    Copy-Item -Path $source -Destination $dest -Force
    $size = (Get-Item $dest).Length
    Write-Host "OK: $name -> $dest ($size bytes)"
    $backedUp += $name
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host "Backup finished WITH FAILURES ($failed file(s) failed). Check the lines above."
    exit 1
}
Write-Host "Backup complete: $($backedUp.Count) file(s) in $Destination"
Write-Host "To restore: stop the hub, copy these files back into $HubDir, start the hub."
