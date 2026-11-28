# Deploy the repeater layer (2026-11-25) to Pandora's Box Mk1.
# The map gains a clustered repeater layer - ham/GMRS (74 plotted)
# + public MeshCore (996 plotted) - with per-type filter chips, a
# Repeaters panel, and detail cards; GET /api/repeaters serves the
# data to localhost and hotspot clients alike. New hub.py (Hub.
# repeaters() + /api/repeaters, shareable like /api/frequencies),
# static/map.html (layer + panel + cards), tests/smoke_test.py
# (section 44), and the two fetched fixture files (fetched
# 2026-11-25; the laptop does not have them yet). Copies the files
# into C:\Library\search-hub\, then restarts ONLY the hub's python
# (by command-line match - never Stop-Process -Name python while
# Recoll is indexing) and relaunches it in serving mode via CIM.
# Run from the VM: scp this + hub.py + static/map.html +
# tests/smoke_test.py + fixtures/repeaters-ham-gmrs.json +
# fixtures/repeaters-meshcore.json to the laptop home dir
# (preserving the static/, tests/ and fixtures/ subdirectories), then:
# powershell -ExecutionPolicy Bypass -File deploy-repeaters.ps1
# ASCII-only.
#
# config.json is NOT copied: the live config differs from the
# workspace template, and the repeater loader reads the fixtures
# from BASE_DIR/fixtures by default. inventory.db, pins.db and
# drawings.db are NOT touched.
#
# After deploy, verify live:
#   /api/health -> ok true, sources 6, plugins 6
#   /api/repeaters -> counts {ham 74, gmrs 0, meshcore 996,
#     plottable 1070, list_only 10}; AE1TH output 442.5, input
#     447.5, offset 5.0, tone 118.8; B.E.M.A. ANX 3.6 km, 910.525
#   the served /map has the Repeaters panel, Ham/GMRS/MeshCore
#     chips, clustered markers, and the detail cards
#   /api/frequencies still returns 91 entries (Radio tile intact)
# Then run the laptop smoke test (run-laptop-smoke.ps1): all checks
# pass except the 7 known environment failures (plus the known
# rotating timing flake, which is pre-existing). Do NOT push to
# GitHub.

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "hub.py"; Dst = "$Hub\hub.py" },
    @{ Src = "static\map.html"; Dst = "$Hub\static\map.html" },
    @{ Src = "tests\smoke_test.py"; Dst = "$Hub\tests\smoke_test.py" },
    @{ Src = "fixtures\repeaters-ham-gmrs.json"; Dst = "$Hub\fixtures\repeaters-ham-gmrs.json" },
    @{ Src = "fixtures\repeaters-meshcore.json"; Dst = "$Hub\fixtures\repeaters-meshcore.json" }
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

# Verify the repeater endpoint serves the full layer.
try {
    $r = Invoke-RestMethod -Uri "$base/api/repeaters" -TimeoutSec 10
    Write-Host ("[verify] /api/repeaters plottable={0} (ham {1}, gmrs {2}, meshcore {3}) list_only={4}" -f $r.counts.plottable, $r.counts.ham, $r.counts.gmrs, $r.counts.meshcore, $r.counts.list_only)
    $ae = $r.repeaters | Where-Object { $_.callsign -eq "AE1TH" }
    Write-Host ("[verify] AE1TH output={0} input={1} offset={2} tone={3}" -f $ae.output_mhz, $ae.input_mhz, $ae.offset_mhz, $ae.tone)
    if ($r.counts.plottable -ne 1070 -or $ae.output_mhz -ne 442.5) {
        Write-Host "[verify] /api/repeaters counts or AE1TH wrong - check the deploy" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "[verify] /api/repeaters FAILED" -ForegroundColor Red
    exit 1
}
