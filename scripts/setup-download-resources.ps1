<#
.SYNOPSIS
  Set up the Pandora's Box Mk1 offline library folders and download the
  resources that can be fetched automatically.
.DESCRIPTION
  Creates the library folder structure under C:\Library (or -LibraryRoot),
  then downloads the small resources with BITS (resumable, built into
  Windows):
    - Map glyph files for place labels (under 1 MB)
    - iFixit ZIM (~3.3 GB)
    - Ham and Drones Stack Exchange ZIMs (~74 MB and ~12 MB)

  The large resources are NOT downloaded by default. They are listed
  with exact steps at the end of the run:
    - Wikipedia maxi ZIM (~119 GB) -- Kiwix library or direct link
    - T.REX Field Manual Vault (~108 GB) -- BitTorrent
    - Protomaps US and New England extracts -- pmtiles CLI
    - USGS topo quads (GeoPDF) -- National Map downloader
    - Hesperian health guides (chapter PDFs) -- hesperian.org
    - US street-address index -- Geofabrik extract + build script

  Pass -IncludeLarge to also download the two large direct downloads
  (Wikipedia maxi ZIM and the US OpenStreetMap extract) with BITS.
  They are resumable, but they are slow -- expect hours on a home
  connection. The T.REX vault and the Protomaps extracts are never
  downloaded by this script; they need a torrent client and the
  pmtiles CLI respectively.

  Nothing in this script needs an account, a login, or a personal
  credential. It only reads public download servers.

  Run from an elevated PowerShell prompt if the library folders need
  administrator rights to create. PowerShell 5.1 compatible.
  This file must stay ASCII-only.
.PARAMETER LibraryRoot
  Root folder for the offline library. Default: C:\Library
.PARAMETER HubDir
  Folder containing this repo (the search-hub code). Default:
  C:\Library\search-hub
.PARAMETER IncludeLarge
  Also download the Wikipedia maxi ZIM and the US OSM extract with
  BITS. Off by default. Slow -- see the description above.
.EXAMPLE
  .\setup-download-resources.ps1
  Create the folders, download the small resources, print the manual steps.
.EXAMPLE
  .\setup-download-resources.ps1 -IncludeLarge
  As above, plus start the large direct downloads (Wikipedia maxi, US extract).
#>
[CmdletBinding()]
param(
    [string]$LibraryRoot = "C:\Library",
    [string]$HubDir = "C:\Library\search-hub",
    [switch]$IncludeLarge
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

$script:step = 0
function Write-Step($Text) {
    $script:step++
    Write-Host ""
    Write-Host ("[{0}] {1}" -f $script:step, $Text) -ForegroundColor Cyan
}

function Write-Note($Text) {
    Write-Host ("      {0}" -f $Text)
}

function Write-Warn($Text) {
    Write-Host ("      WARNING: {0}" -f $Text) -ForegroundColor Yellow
}

# Find the newest .zim file with a given prefix in a Kiwix download
# directory listing. Returns the full URL, or $null if none is found.
# Parses the raw HTML (not the DOM) so it works with -UseBasicParsing
# on PowerShell 5.1, where the response has no .Links property.
function Get-LatestZimUrl($BaseUrl, $Prefix) {
    try {
        $resp = Invoke-WebRequest -Uri $BaseUrl -UseBasicParsing -TimeoutSec 60
    } catch {
        Write-Warn ("Could not list {0}: {1}" -f $BaseUrl, $_.Exception.Message)
        return $null
    }
    $names = @()
    $found = [regex]::Matches($resp.Content, 'href="([^"]*\.zim)"')
    foreach ($m in $found) {
        $leaf = ($m.Groups[1].Value -split "/")[-1]
        if ($leaf -and $leaf.StartsWith($Prefix)) {
            $names += $leaf
        }
    }
    if ($names.Count -eq 0) { return $null }
    $latest = ($names | Sort-Object | Select-Object -Last 1)
    return ($BaseUrl.TrimEnd("/") + "/" + $latest)
}

# Download a file with BITS (resumable). Skips the download if the
# destination already exists and is non-empty.
function Download-File($Url, $Destination, $Label) {
    if (Test-Path $Destination) {
        $existing = Get-Item $Destination
        if ($existing.Length -gt 0) {
            Write-Note ("{0}: already present ({1:N0} bytes), skipping." -f $Label, $existing.Length)
            return $true
        }
    }
    Write-Note ("{0}: downloading from {1}" -f $Label, $Url)
    Write-Note "      (BITS: resumable. Close this window and re-run to resume.)"
    try {
        Start-BitsTransfer -Source $Url -Destination $Destination -DisplayName $Label -Description $Label -ErrorAction Stop
    } catch {
        Write-Warn ("BITS download failed for {0}: {1}" -f $Label, $_.Exception.Message)
        Write-Note "      Try the direct link in a browser or with aria2 / a torrent client."
        return $false
    }
    $done = Get-Item $Destination
    Write-Note ("{0}: done ({1:N0} bytes)." -f $Label, $done.Length)
    return $true
}

# ---------------------------------------------------------------------------
# 1. Library folders
# ---------------------------------------------------------------------------

Write-Step "Creating the library folders under $LibraryRoot"
$folders = @(
    (Join-Path $LibraryRoot "kiwix"),
    (Join-Path $LibraryRoot "FieldManuals"),
    (Join-Path $LibraryRoot "Maps"),
    (Join-Path $LibraryRoot "Maps\quads"),
    (Join-Path $LibraryRoot "MyFiles"),
    (Join-Path $LibraryRoot "geocode"),
    (Join-Path $LibraryRoot "recoll-conf"),
    (Join-Path $LibraryRoot "drivers")
)
foreach ($f in $folders) {
    if (-not (Test-Path $f)) {
        New-Item -ItemType Directory -Path $f -Force | Out-Null
        Write-Note ("created {0}" -f $f)
    } else {
        Write-Note ("exists  {0}" -f $f)
    }
}

# ---------------------------------------------------------------------------
# 2. Map glyph files (small, always downloaded)
# ---------------------------------------------------------------------------

Write-Step "Downloading map glyph files (place labels)"
$glyphDir = Join-Path $HubDir "static\vendor\glyphs\Noto Sans Regular"
if (-not (Test-Path $glyphDir)) {
    New-Item -ItemType Directory -Path $glyphDir -Force | Out-Null
}
$glyphBase = "https://demotiles.maplibre.org/font/Noto%20Sans%20Regular"
Download-File "$glyphBase/0-255.pbf" (Join-Path $glyphDir "0-255.pbf") "Glyphs 0-255" | Out-Null
Download-File "$glyphBase/256-511.pbf" (Join-Path $glyphDir "256-511.pbf") "Glyphs 256-511" | Out-Null
Write-Note "Without these the map works but place labels do not render."

# ---------------------------------------------------------------------------
# 3. Small ZIMs (always downloaded)
# ---------------------------------------------------------------------------

Write-Step "Downloading the small ZIM files (iFixit, Ham, Drones)"
$kiwixDir = Join-Path $LibraryRoot "kiwix"

$ifixitUrl = Get-LatestZimUrl "https://download.kiwix.org/zim/ifixit/" "ifixit_en_all"
if ($ifixitUrl) {
    $file = Split-Path $ifixitUrl -Leaf
    Download-File $ifixitUrl (Join-Path $kiwixDir $file) "iFixit ZIM (~3.3 GB)" | Out-Null
} else {
    Write-Warn "Could not find the iFixit ZIM. Get it from https://download.kiwix.org/zim/ifixit/"
}

$hamUrl = Get-LatestZimUrl "https://download.kiwix.org/zim/stackexchange/" "ham.stackexchange.com_en_all"
if ($hamUrl) {
    $file = Split-Path $hamUrl -Leaf
    Download-File $hamUrl (Join-Path $kiwixDir $file) "Ham Stack Exchange ZIM (~74 MB)" | Out-Null
} else {
    Write-Warn "Could not find the Ham Stack Exchange ZIM. Get it from https://download.kiwix.org/zim/stackexchange/"
}

$dronesUrl = Get-LatestZimUrl "https://download.kiwix.org/zim/stackexchange/" "drones.stackexchange.com_en_all"
if ($dronesUrl) {
    $file = Split-Path $dronesUrl -Leaf
    Download-File $dronesUrl (Join-Path $kiwixDir $file) "Drones Stack Exchange ZIM (~12 MB)" | Out-Null
} else {
    Write-Warn "Could not find the Drones Stack Exchange ZIM. Get it from https://download.kiwix.org/zim/stackexchange/"
}

# ---------------------------------------------------------------------------
# 4. Large direct downloads (only with -IncludeLarge)
# ---------------------------------------------------------------------------

if ($IncludeLarge) {
    Write-Step "Downloading the large direct files (-IncludeLarge)"
    Write-Warn "These are slow. BITS resumes if interrupted; re-run this script to continue."

    $wikiUrl = Get-LatestZimUrl "https://download.kiwix.org/zim/wikipedia/" "wikipedia_en_all_maxi"
    if ($wikiUrl) {
        $file = Split-Path $wikiUrl -Leaf
        Download-File $wikiUrl (Join-Path $kiwixDir $file) "Wikipedia maxi ZIM (~119 GB)" | Out-Null
        Write-Note "Record the snapshot date (the YYYY-MM in the file name) in the hub config.json under library.zim_snapshots."
    } else {
        Write-Warn "Could not find the Wikipedia maxi ZIM. Get it from https://library.kiwix.org or https://download.kiwix.org/zim/wikipedia/"
    }

    $usPbf = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"
    Download-File $usPbf (Join-Path (Join-Path $LibraryRoot "geocode") "us-latest.osm.pbf") "US OpenStreetMap extract (~11.4 GB)" | Out-Null
    Write-Note "After it finishes, build the address index (about 2.5 hours):"
    Write-Note "  cd C:\Library\geocode"
    Write-Note "  pip install osmium"
    Write-Note ("  python {0}\scripts\build-address-index.py us-latest.osm.pbf addresses.db" -f $HubDir)
    Write-Note "Verify the download first: Geofabrik publishes an MD5 file next to the extract."
} else {
    Write-Step "Large direct downloads skipped (re-run with -IncludeLarge to fetch them)"
    Write-Note "Skipped: Wikipedia maxi ZIM (~119 GB), US OpenStreetMap extract (~11.4 GB)."
    Write-Note "See the manual steps below for the other large resources."
}

# ---------------------------------------------------------------------------
# 5. Manual steps (these need a torrent client, the pmtiles CLI, or a
#    browser -- this script prints the exact steps instead of doing them)
# ---------------------------------------------------------------------------

Write-Step "Manual steps -- do these by hand (listed in order)"
Write-Host ""

Write-Host "  A. Kiwix tools (kiwix-serve + kiwix-manage)" -ForegroundColor Green
Write-Host "     Get kiwix-tools for Windows from https://download.kiwix.org/release/kiwix-tools/"
Write-Host "     (or install Kiwix Desktop from https://download.kiwix.org/release/kiwix-desktop/)."
Write-Host "     Put kiwix-serve on PATH, then build the library file:"
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\wikipedia_en_all_maxi_*.zim" -f $LibraryRoot)
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\ifixit_en_all_*.zim" -f $LibraryRoot)
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\ham.stackexchange.com_en_all_*.zim" -f $LibraryRoot)
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\drones.stackexchange.com_en_all_*.zim" -f $LibraryRoot)
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\wikihow_en_maxi_*.zim" -f $LibraryRoot)
Write-Host "     run_hub.bat starts kiwix-serve from this library file on port 8080."
Write-Host ""

Write-Host "  B. Wikipedia maxi ZIM (~119 GB) -- if you did not use -IncludeLarge" -ForegroundColor Green
Write-Host "     Easiest: open Kiwix Desktop, go to the Library, search for"
Write-Host "     'wikipedia_en_all_maxi', and download it into the kiwix folder."
Write-Host "     Or use a torrent client / aria2 with the direct link from"
Write-Host "     https://download.kiwix.org/zim/wikipedia/ (resumable)."
Write-Host "     Record the snapshot date (YYYY-MM in the file name) in"
Write-Host "     config.json under library.zim_snapshots."
Write-Host ""

Write-Host "  C. T.REX Field Manual Vault (~108 GB) -- torrent only" -ForegroundColor Green
Write-Host "     1. Go to https://www.trex-arms.com/field-manual-vault/download"
Write-Host "     2. Download FieldManualVault_2026-11.torrent (or the newest release)."
Write-Host "     3. Open it in qBittorrent (or another torrent client) and"
Write-Host "        download into a scratch folder. It contains the PDFs,"
Write-Host "        EPUBs, and text-only MD files."
Write-Host "     4. Verify integrity: sha256sum -c SHA256SUMS --ignore-missing"
Write-Host "        (the --ignore-missing skips files you chose not to download)."
Write-Host ("     5. Unzip the manuals into {0}\FieldManuals\" -f $LibraryRoot)
Write-Host "     There is also a 21.8 GB single-file ZIM version of the vault"
Write-Host "     on the same page if you prefer to read it in Kiwix instead."
Write-Host ""

Write-Host "  D. Protomaps extracts (US ~17.6 GB, New England ~1.2 GB)" -ForegroundColor Green
Write-Host "     1. Get the pmtiles CLI (a single binary, no account) from"
Write-Host "        https://github.com/protomaps/go-pmtiles/releases"
Write-Host "     2. Find a recent build date at https://maps.protomaps.com/builds/"
Write-Host "     3. Cut the areas you need (replace the date with a recent one):"
Write-Host "        pmtiles extract https://build.protomaps.com/20261117.pmtiles conus.pmtiles --bbox=-125,24.5,-66.9,49.4 --maxzoom=15"
Write-Host "        pmtiles extract https://build.protomaps.com/20261117.pmtiles ne.pmtiles --bbox=-73.73,40.98,-66.95,47.46 --maxzoom=15"
Write-Host ("     4. Move the finished .pmtiles files into {0}\Maps\" -f $LibraryRoot)
Write-Host "        and set maps_dir in config.json to that folder."
Write-Host ""

Write-Host "  E. USGS topo quads (GeoPDF, ~5.7 GB for Massachusetts)" -ForegroundColor Green
Write-Host "     1. Download the GeoPDFs for your area from the National Map"
Write-Host "        downloader: https://apps.nationalmap.gov/downloader/"
Write-Host ("        (US Topo, GeoPDF format) into {0}\Maps\quads\" -f $LibraryRoot)
Write-Host "     2. Build the hub quad index:"
Write-Host ("        python {0}\scripts\build-quad-index.py" -f $HubDir)
Write-Host "        (writes fixtures/usgs-quads-ma.json; the shipped index covers"
Write-Host "        Massachusetts, 369 quads, newest edition each.)"
Write-Host ""

Write-Host "  F. Hesperian health guides (46 chapter PDFs)" -ForegroundColor Green
Write-Host "     Download the chapter PDFs from https://hesperian.org/"
Write-Host "     (the 'Where There Is No Doctor' family of titles) into"
Write-Host ("     {0}\MyFiles\" -f $LibraryRoot)
Write-Host "     Recoll indexes them alongside the manuals."
Write-Host ""

Write-Host "  G. US street-address index (~7.1 GB database, about 2.5 hours)" -ForegroundColor Green
Write-Host "     1. Download the US extract (11.4 GB) -- re-run this script"
Write-Host "        with -IncludeLarge, or fetch it directly:"
Write-Host "        https://download.geofabrik.de/north-america/us-latest.osm.pbf"
Write-Host "     2. Install the Python package: pip install osmium"
Write-Host "     3. Build the index from the geocode folder:"
Write-Host "        cd C:\Library\geocode"
Write-Host ("        python {0}\scripts\build-address-index.py us-latest.osm.pbf addresses.db" -f $HubDir)
Write-Host "        (Run it from C:\Library\geocode -- it writes temporary files"
Write-Host "        into the current directory. Safe to re-run; it rebuilds"
Write-Host "        the database from scratch each time.)"
Write-Host ""

Write-Host "  H. Recoll index (build once, plugged in)" -ForegroundColor Green
Write-Host "     Install Recoll for Windows, then run:"
Write-Host ("     {0}\scripts\build_recoll_index.bat {1}\recoll-conf" -f $HubDir, $LibraryRoot)
Write-Host "     Point the Recoll config topdirs at the FieldManuals and"
Write-Host "     MyFiles folders. After the build, record the date in"
Write-Host "     config.json under library.recoll_index_date."
Write-Host ""

Write-Host "  I. Finish setup" -ForegroundColor Green
Write-Host "     - Run the hub: double-click run_hub.bat in the hub folder,"
Write-Host "       then open http://127.0.0.1:8899/"
Write-Host "     - Run the tests: python3 tests/smoke_test.py"
Write-Host "     - Run the hygiene script as Administrator:"
Write-Host ("       {0}\scripts\setup_hygiene.ps1" -f $HubDir)
Write-Host "     - Optional, for a hub that starts at logon:"
Write-Host ("       {0}\scripts\install_hub_autostart.ps1" -f $HubDir)
Write-Host ""

Write-Host "  J. WikiHow ZIM (~51 GB) -- optional, general how-to guides" -ForegroundColor Green
Write-Host "     Kiwix stopped publishing new English WikiHow ZIMs, so the"
Write-Host "     latest snapshot is 2023-03 and it lives in the archive, not"
Write-Host "     the current library. Download it with a resumable client"
Write-Host "     (aria2 or a torrent client) -- it is 51 GB, expect hours:"
Write-Host "     https://archive.download.kiwix.org/zim/wikihow/wikihow_en_maxi_2023-03.zim"
Write-Host ("     Move it into {0}\kiwix\ and add it to the library file:" -f $LibraryRoot)
Write-Host ("     kiwix-manage {0}\kiwix\library.xml add {0}\kiwix\wikihow_en_maxi_*.zim" -f $LibraryRoot)
Write-Host "     Record the snapshot date (2023-03) in config.json under"
Write-Host "     library.zim_snapshots. The core how-to content is stable,"
Write-Host "     but anything published after March 2023 is missing."
Write-Host ""

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------

Write-Step "Summary"
Write-Host "  Library root: $LibraryRoot"
Write-Host "  Hub folder:   $HubDir"
Write-Host ""
Write-Host "  Downloaded now (small resources):"
$kiwixDir = Join-Path $LibraryRoot "kiwix"
foreach ($f in @("0-255.pbf", "256-511.pbf")) {
    $p = Join-Path $HubDir "static\vendor\glyphs\Noto Sans Regular\$f"
    if (Test-Path $p) { Write-Host ("    [ok] glyphs {0}" -f $f) } else { Write-Host ("    [missing] glyphs {0}" -f $f) }
}
if (Test-Path $kiwixDir) {
    $zims = Get-ChildItem $kiwixDir -Filter *.zim -ErrorAction SilentlyContinue
    if ($zims) {
        foreach ($z in $zims) { Write-Host ("    [ok] {0} ({1:N0} bytes)" -f $z.Name, $z.Length) }
    } else {
        Write-Host "    [none] no ZIM files in the kiwix folder yet"
    }
}
Write-Host ""
Write-Host "  Still to do by hand: steps A-H above."
Write-Host "  Full list with sources and sizes: see README.md, 'Recommended resources'."
Write-Host ""
Write-Host "  Done."
