# Deploy the daily briefing tiles (2026-11-24) to Pandora's Box Mk1.
# The Command Center gains a Daily briefing section - weather (cached
# NOAA forecast), supplies, family, comms, map, and today's priorities
# - each tile hideable on its own. New weather.py (NOAA fetch + cache),
# hub.py (GET /api/weather + POST /api/weather/refresh, localhost-only),
# static/ui.html (briefing section + tiles + editor entry), and
# tests/smoke_test.py (section 43). Copies the four files into
# C:\Library\search-hub\, then restarts ONLY the hub's python (by
# command-line match - never Stop-Process -Name python while Recoll
# is indexing) and relaunches it in serving mode via CIM.
# Run from the VM: scp this + weather.py + hub.py + static/ui.html +
# tests/smoke_test.py to the laptop home dir (preserving the static/
# and tests/ subdirectories), then:
# powershell -ExecutionPolicy Bypass -File deploy-briefing.ps1
# ASCII-only.
#
# config.json is NOT copied: the live config differs from the
# workspace template, and the code defaults weather_cache to
# "weather.json" next to hub.py. weather.json is runtime data (created
# on the first forecast refresh) and is NOT copied either.
#
# After deploy, verify live:
#   /api/weather -> {"weather": null} (no forecast saved yet)
#   the served / has the briefing section with the six tiles
#     (brief-weather, brief-supplies, brief-family, brief-comms,
#     brief-map, brief-priorities) and the Refresh button
#   POST /api/weather/refresh from the laptop fetches the NOAA
#     forecast for Braintree and the weather tile shows it
# Then run the laptop smoke test (run-laptop-smoke.ps1): all checks
# pass except the 7 known environment failures (plus the known
# rotating timing flake, which is pre-existing).

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "weather.py"; Dst = "$Hub\weather.py" },
    @{ Src = "hub.py"; Dst = "$Hub\hub.py" },
    @{ Src = "static\ui.html"; Dst = "$Hub\static\ui.html" },
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
