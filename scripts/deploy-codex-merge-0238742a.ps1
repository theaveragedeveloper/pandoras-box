# Deploy the Codex 0238742a merge (hub polish + family plan editing/printing,
# merged with the six security-audit fixes) to Pandora's Box Mk1.
# Copies hub.py, family_plan_store.py (new), static/ui.html, static/map.html,
# tests/smoke_test.py, README.md into C:\Library\search-hub\, then restarts
# ONLY the hub's python (by command-line match - never Stop-Process -Name
# python while Recoll is indexing) and relaunches it in serving mode via CIM.
# Run from the VM: scp this + the 6 files to the laptop home dir,
# then: powershell -ExecutionPolicy Bypass -File deploy-codex-merge-0238742a.ps1
# ASCII-only.

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "hub.py"; Dst = "$Hub\hub.py" },
    @{ Src = "family_plan_store.py"; Dst = "$Hub\family_plan_store.py" },
    @{ Src = "ui.html"; Dst = "$Hub\static\ui.html" },
    @{ Src = "map.html"; Dst = "$Hub\static\map.html" },
    @{ Src = "smoke_test.py"; Dst = "$Hub\tests\smoke_test.py" },
    @{ Src = "README.md"; Dst = "$Hub\README.md" }
)

foreach ($f in $files) {
    $src = Join-Path $HomeDir $f.Src
    if (-not (Test-Path $src)) {
        Write-Host ("[deploy] MISSING source: {0} - did you scp the files first?" -f $src) -ForegroundColor Red
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
