# Pandora's Box Mk1 — hash the library so silent corruption is caught.
# There is no second copy (round 7, 2026-11-14: WD Blue 2TB is the only
# storage) — this script verifies the library where it sits, against
# hashes recorded when the files were fresh. A hash mismatch means
# re-download that file from its source in files/rebuild-manifest.md.
#
# Usage (PowerShell, on the 5424, plugged in):
#   .\hash_library.ps1 -Write    record SHA-256 of every ZIM + PDF pack
#                                into C:\Library\hashes.txt
#   .\hash_library.ps1 -Verify   re-hash and compare against hashes.txt
#                                (run quarterly, and after any re-download)
# Run -Write right after the library is built, and again after any
# library change. Copy hashes.txt OFF the laptop (to the Mac mini) the
# same day — hashes that die with the drive prove nothing.
param(
    [switch]$Write,
    [switch]$Verify,
    [string]$Library = "C:\Library",
    [string]$HashFile = "C:\Library\hashes.txt"
)

$ErrorActionPreference = "Stop"
if (-not $Write -and -not $Verify) { Write-Host "Use -Write or -Verify."; exit 1 }
if (-not (Test-Path $Library)) { Write-Host "Library folder not found: $Library"; exit 1 }

$files = Get-ChildItem -Path $Library -Recurse -File -Include *.zim, *.pdf, *.pmtiles |
    Where-Object { $_.FullName -notmatch 'recoll-conf' } | Sort-Object FullName
if (-not $files) { Write-Host "No ZIM/PDF/pmtiles files under $Library yet."; exit 1 }

if ($Write) {
    $lines = foreach ($f in $files) {
        $h = (Get-FileHash -Path $f.FullName -Algorithm SHA256).Hash
        "$h  $($f.FullName)"
    }
    $lines | Out-File -FilePath $HashFile -Encoding utf8
    Write-Host "Wrote $($lines.Count) hashes to $HashFile"
    exit 0
}

# -Verify
if (-not (Test-Path $HashFile)) { Write-Host "No hash file: $HashFile (run -Write first)"; exit 1 }
$bad = 0; $missing = 0; $checked = 0
foreach ($line in Get-Content $HashFile) {
    if ($line -notmatch '^([0-9A-Fa-f]{64})\s+(.+)$') { continue }
    $want, $path = $Matches[1], $Matches[2]
    if (-not (Test-Path $path)) { Write-Host "MISSING: $path"; $missing++; continue }
    $got = (Get-FileHash -Path $path -Algorithm SHA256).Hash
    $checked++
    if ($got -ne $want) { Write-Host "MISMATCH: $path"; $bad++ }
}
Write-Host "Checked $checked, $bad mismatched, $missing missing."
if ($bad -gt 0 -or $missing -gt 0) { exit 1 }
Write-Host "All hashes match."
