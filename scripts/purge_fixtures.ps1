# Pandora's Box Mk1 — purge the hub's sample fixtures (F15).
#
# The hub ships with sample data so it can be demoed before the library
# exists: fixtures/demo_kiwix.json, fixtures/demo_recoll.json, and the
# sample docs in fixtures/myfiles/ (including a SAMPLE emergency
# plan with plausible meeting points and first-aid instructions). Once
# the owner's real plan and real backends exist, those files are dead weight
# with a live hazard: if the myfiles folder is ever pointed at the
# fixtures, or the sample plan is copied instead of replaced, the hub
# serves a fake emergency plan.
#
# This script MOVES the sample files out of the searchable tree into
# fixtures/_archived-samples/ (kept, clearly labelled, not deleted —
# the demo still works on a dev machine by moving them back). It does
# NOT delete anything.
#
# GUARD: the script refuses to run while config.json still points the
# myfiles source at fixtures/myfiles — point myfiles at the real docs
# folder first, then purge. Purging before the real folder exists would
# leave My Files with nothing to search and no way to demo the hub.
#
# Run once at setup, after the real docs folder exists and the hub
# config points at it. Also recorded as a step in the arrival checklist.

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$HubFolder = Split-Path -Parent $PSScriptRoot
$Fixtures = Join-Path $HubFolder "fixtures"
$Archive = Join-Path $Fixtures "_archived-samples"

$configPath = Join-Path $HubFolder "config.json"
try {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
} catch {
    Write-Host "[purge] Cannot read config.json: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$myfiles = $config.sources | Where-Object { $_.plugin -eq "myfiles" } | Select-Object -First 1
$folder = ""
if ($myfiles) { $folder = [string]$myfiles.folder }
if (-not $Force -and ($folder -eq "fixtures/myfiles" -or $folder -eq "fixtures\myfiles" -or $folder -eq "")) {
    Write-Host "[purge] REFUSING: config.json still points My Files at '$folder'." -ForegroundColor Red
    Write-Host "  Point the myfiles source at the real docs folder first"
    Write-Host "  (config.json, or the hub UI's 'Add a source'), then re-run this."
    Write-Host "  (Or pass -Force if you really mean it.)"
    exit 1
}

if (-not (Test-Path $Archive)) { New-Item -ItemType Directory -Path $Archive -Force | Out-Null }

$moved = @()
foreach ($name in @("demo_kiwix.json", "demo_recoll.json")) {
    $src = Join-Path $Fixtures $name
    if (Test-Path $src) {
        Move-Item $src (Join-Path $Archive $name) -Force
        $moved += $name
        Write-Host "  archived: fixtures/$name"
    }
}
$myfilesDir = Join-Path $Fixtures "myfiles"
if (Test-Path $myfilesDir) {
    $dest = Join-Path $Archive "myfiles"
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $myfilesDir $dest -Force
    $moved += "myfiles/ (sample docs)"
    Write-Host "  archived: fixtures/myfiles/ (sample docs)"
}

if ($moved.Count -eq 0) {
    Write-Host "[purge] Nothing to move — fixtures already purged."
} else {
    Write-Host ""
    Write-Host "[purge] Done. Sample data moved to fixtures/_archived-samples/."
    Write-Host "  The hub's demo fallback is OFF by default, so nothing changes"
    Write-Host "  for real searches. To demo the hub again, move the files back."
}
