# Deploy the AR-15 search fix (2026-11-24) to Pandora's Box Mk1.
# Fixes "AR-15 manual" / "cleaning guide" not surfacing the resource:
# query_expand.py gains firearms aliases (AR-15 -> M16A2 / M4 carbine,
# the manuals' own vocabulary) and plugins/recoll.py dedupes the same
# manual in three formats (PDF/EPUB/MD) to one result. Copies
# query_expand.py, plugins/recoll.py and tests/smoke_test.py into
# C:\Library\search-hub\, then restarts ONLY the hub's python (by
# command-line match - never Stop-Process -Name python while Recoll
# is indexing) and relaunches it in serving mode via CIM.
# Run from the VM: scp this + query_expand.py + plugins/recoll.py +
# tests/smoke_test.py to the laptop home dir, then:
# powershell -ExecutionPolicy Bypass -File deploy-search-fix.ps1
# ASCII-only.
#
# After deploy, verify live (bypass the 600 s search cache with
# fresh=1, since the old queries may still be cached):
#   /api/search?q=AR-15+manual&fresh=1  -> recoll top 3 has
#     TM 9 1005 319 10 (the M16A2/M4 operator's manual)
#   /api/search?q=ar-15+cleaning+guide&fresh=1 -> same, top 3
# Then run the laptop smoke test (run-laptop-smoke.ps1): all checks
# pass except the 7 known environment failures, and the two new
# "live: AR-15 manual ..." checks pass (they are skipped in the
# workspace run, where there is no live Recoll index).

$ErrorActionPreference = "Stop"
$Hub = "C:\Library\search-hub"
$HomeDir = $env:USERPROFILE

$files = @(
    @{ Src = "query_expand.py"; Dst = "$Hub\query_expand.py" },
    @{ Src = "plugins\recoll.py"; Dst = "$Hub\plugins\recoll.py" },
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
