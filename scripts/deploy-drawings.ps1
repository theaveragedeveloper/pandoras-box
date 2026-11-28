# Deploy the map drawing + typed items feature (2026-11-24) to
# Pandora's Box Mk1.
# The map gains draw tools (Marker, Line, Area, Circle) with a label
# + type (water/food/fuel/medical/shelter/hazard/rally/other) per
# item, stored server-side (drawings.py, drawings.db, /api/drawings,
# localhost-only like pins) and managed from a drawings panel with
# edit/delete per item and hide/show by type. Copies drawings.py,
# hub.py, static/map.html and tests/smoke_test.py into
# C:\Library\search-hub\, then restarts ONLY the hub's python (by
# command-line match - never Stop-Process -Name python while Recoll
# is indexing) and relaunches it in serving mode via CIM.
# Run from the VM: scp this + drawings.py + hub.py + static/map.html
# + tests/smoke_test.py to the laptop home dir (preserving the
# static/ and tests/ subdirectories), then:
# powershell -ExecutionPolicy Bypass -File deploy-drawings.ps1
# ASCII-only.
#
# config.json is NOT copied: the live config differs from the
# workspace template, and the code defaults drawings_db to
# "drawings.db" next to hub.py.
#
# After deploy, verify live:
#   /api/drawings -> {"drawings": []} (empty store, localhost-only)
#   the served /map has draw-mode, draw-tools, the drawings panel
#   with draw-type-filters + draw-form, and the Draw button
# Then run the laptop smoke test (run-laptop-smoke.ps1): all checks
# pass except the 7 known environment failures.

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "drawings.py"; Dst = "$Hub\drawings.py" },
    @{ Src = "hub.py"; Dst = "$Hub\hub.py" },
    @{ Src = "static\map.html"; Dst = "$Hub\static\map.html" },
    @{ Src = "tests\smoke_test.py"; Dst = "$Hub\tests\smoke_test.py" }
)

foreach ($f in $files) {
    $src = Join-Path $HomeDir $f.Src
    if (-not (Test-Path $src)) {
        Write-Host "[deploy] MISSING source: $src - did you scp the files first?" -ForegroundColor Red
        exit 1
    }
    Copy-Item $src $f.Dst -Force
    $sz = (Get-Item $f.Dst).Length
    Write-Host ("[deploy] {0} -> {1} ({2} bytes)" -f $f.Src, $f.Dst, $sz)
}

# Restart ONLY the hub's python (command-line match). Recoll's filters
# also run as python - never Stop-Process -Name python.
$hubProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'hub\.py' }
if ($hubProc) {
    foreach ($p in $hubProc) {
        Write-Host ("[deploy] Stopping hub python PID {0}..." -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
    Start-Sleep -Seconds 2
} else {
    Write-Host "[deploy] No hub python running - will start it fresh."
}

# Relaunch in serving mode, detached via CIM (survives SSH teardown).
$cmd = 'cmd /c "C:\Library\search-hub\launch-hub-serve.bat"'
$rc = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($rc.ReturnValue -ne 0) {
    Write-Host ("[deploy] CIM launch failed, ReturnValue={0}" -f $rc.ReturnValue) -ForegroundColor Red
    exit 1
}
Write-Host ("[deploy] CIM launch OK, PID {0}" -f $rc.ProcessId)

# Wait for the hub to answer health (it binds 8899; the routing graph
# loads lazily on the first route, so health should come up quickly).
$base = "http://127.0.0.1:8899"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 5
        Write-Host ("[verify] /api/health ok={0} sources={1} plugins={2}" -f $h.ok, $h.sources, $h.plugins)
        $ok = $true
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ok) {
    Write-Host "[verify] /api/health FAILED after 60s" -ForegroundColor Red
    exit 1
}
