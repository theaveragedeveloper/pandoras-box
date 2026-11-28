# Deploy the MeshCore check-in feed (2026-11-26) to Pandora's Box Mk1.
# The family tile shows live MeshCore state instead of the hardcoded
# "radios have not arrived" line. New meshcore_link.py (background
# thread talking the companion protocol to the L1 Pro over USB serial
# on the Full (USB + BLE) build, via the meshcore library), hub.py
# (Hub.meshcore_status() + GET /api/meshcore/status, localhost-only),
# static/ui.html (meshHtml + renderFamily(data, mesh), briefing job),
# tests/smoke_test.py (section 45). Also installs the meshcore
# library on the laptop via pip (the hub still starts without it,
# reporting the missing library honestly, but the radio link needs
# it). Copies the files into C:\Library\search-hub\, then restarts
# ONLY the hub's python (by command-line match - never Stop-Process
# -Name python while Recoll is indexing) and relaunches it in
# serving mode via CIM.
# Run from the VM: scp this + meshcore_link.py + hub.py +
# static/ui.html + tests/smoke_test.py to the laptop home dir
# (preserving the static/ and tests/ subdirectories), then:
# powershell -ExecutionPolicy Bypass -File deploy-meshcore.ps1
# ASCII-only.
#
# config.json is NOT copied: the live config differs from the
# workspace template, and the link auto-detects the serial port
# (config "meshcore_port" can pin it). inventory.db, pins.db and
# drawings.db are NOT touched.
#
# EXPECTED live state: the L1 Pro is probably still on the
# Bluetooth-only build (no USB serial), so /api/meshcore/status
# will most likely report connected:false with a reason naming the
# Full-build reflash - that IS the honest degraded state, and the
# tile will say so. The script asserts the payload SHAPE, not
# connected:true. After the owner reflashes to the Full build and plugs
# the radio in over USB, the same endpoint reports connected:true
# with the radio identity and any real check-ins.
#
# After deploy, verify live:
#   /api/health -> ok true, sources 6, plugins 6
#   /api/meshcore/status -> connected false/true with a reason,
#     radio/contacts/messages keys, nearby_repeaters (B.E.M.A. ANX
#     3.6 km, AE1TH among them)
#   the served Command Center no longer contains the hardcoded
#     "radios have not arrived" line and fetches /api/meshcore/status
#   /api/frequencies still returns 91 entries (Radio tile intact)
# Then run the laptop smoke test (run-laptop-smoke.ps1): all checks
# pass except the 7 known environment failures (plus the known
# rotating timing flake, which is pre-existing). Do NOT push to
# GitHub.

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "meshcore_link.py"; Dst = "$Hub\meshcore_link.py" },
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

# The meshcore library (PyPI meshcore 2.1.24) is what talks the
# companion protocol over USB serial. Install it if missing; the
# hub still starts without it, but the radio link cannot connect.
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py -c "import meshcore" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[deploy] meshcore library not installed - installing via pip..."
    & $py -m pip install meshcore
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[deploy] WARNING: pip install meshcore failed - the hub will still start, but /api/meshcore/status will report the missing library. Re-run the pip install when the laptop is online." -ForegroundColor Yellow
    } else {
        Write-Host "[deploy] meshcore library installed."
    }
} else {
    Write-Host "[deploy] meshcore library already installed."
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

# Verify the MeshCore endpoint serves a well-formed payload. The
# radio is most likely BLE-only / not plugged in, so connected:false
# is the EXPECTED live state - assert the shape, not connected:true.
try {
    $m = Invoke-RestMethod -Uri "$base/api/meshcore/status" -TimeoutSec 15
    Write-Host ("[verify] /api/meshcore/status connected={0} state={1}" -f $m.connected, $m.state)
    Write-Host ("[verify] reason: {0}" -f $m.reason)
    Write-Host ("[verify] nearby repeaters: {0}" -f $m.nearby_repeaters.Count)
    if (-not $m.reason -or $null -eq $m.nearby_repeaters -or $null -eq $m.messages) {
        Write-Host "[verify] /api/meshcore/status shape wrong - check the deploy" -ForegroundColor Red
        exit 1
    }
    if ($m.connected -eq $false) {
        Write-Host "[verify] Radio not connected over USB (expected until the Full-build reflash) - the tile will say so."
    } else {
        Write-Host ("[verify] Radio connected: {0} ({1}), {2} check-in(s)" -f $m.radio.name, $m.radio.model, $m.messages.Count)
    }
} catch {
    Write-Host "[verify] /api/meshcore/status FAILED" -ForegroundColor Red
    exit 1
}

# Verify the served Command Center fetches the feed and no longer
# carries the hardcoded line.
try {
    $page = curl.exe -s "$base/" --max-time 10
    if ($page -match "radios have not arrived") {
        Write-Host "[verify] Served homepage still has the hardcoded line - check the deploy" -ForegroundColor Red
        exit 1
    }
    if ($page -notmatch "/api/meshcore/status") {
        Write-Host "[verify] Served homepage does not fetch /api/meshcore/status - check the deploy" -ForegroundColor Red
        exit 1
    }
    Write-Host "[verify] Served homepage fetches /api/meshcore/status, hardcoded line gone."
} catch {
    Write-Host "[verify] Homepage check FAILED" -ForegroundColor Red
    exit 1
}
