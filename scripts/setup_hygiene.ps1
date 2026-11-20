# Pandora's Box Mk1 — Windows setup hygiene (run once at setup, on the 5424).
# - Excludes the library folders from Windows Search indexing (Recoll is the
#   indexer here; Windows Search churning over hundreds of PDFs burns CPU
#   on battery for nothing).
# - Sets a DURABLE offline posture (audit flaw #6, round 4): the 7-day
#   update pause alone expires, so this also marks the connection metered
#   and sets Windows Update to "notify before download" (AUOptions=2).
#   The machine is offline by design; updates happen only when the owner
#   deliberately goes online for them, never in the background.
# - Prints the Field-plan hotspot reminder (serving needs Wi-Fi ON).
#
# ORDER (round 5): run this AFTER the library folders exist — step 6 of
# the install order creates C:\Library and its subfolders first, then
# this script runs. If a folder is missing the script FAILS LOUDLY
# (exit 1) instead of skipping quietly, so a "(skip)" can never hide a
# folder that was never excluded. Re-run it after the library download
# (step 7) to confirm the exclusions took; a clean exit proves it.
#
# Run from an elevated PowerShell (right-click > Run as Administrator) —
# the update registry keys need admin. The indexing exclusion works
# per-user and does not need admin, but run elevated anyway so both steps
# land in one pass. Nothing here downloads anything.
#
# Honest limits: the indexing exclusion uses the documented
# ISearchCrawlScopeManager COM API; if it fails on this build, the script
# says so and prints the manual route (Settings > Privacy & security >
# Searching Windows > Add an excluded folder). The update pause uses the
# documented UX Settings registry values; verify in Settings > Windows
# Update afterwards — if the pause did not take, set it there by hand.
# The metered-Ethernet key (DefaultMediaCost) is the documented
# per-connection cost table; verify in Settings > Network & internet >
# Ethernet (or Wi-Fi) > Metered connection afterwards.

param(
    [string[]]$LibraryFolders = @("C:\Library", "C:\Library\kiwix", "C:\Library\maps", "C:\Library\manuals"),
    [string]$MeteredWifiSsid = ""
)

$ErrorActionPreference = "Stop"

Write-Host "[hygiene] Pandora's Box Mk1 setup hygiene" -ForegroundColor Yellow
Write-Host ""

# --- 0. Library folders must exist first (fail loudly, round 5) ----------
Write-Host "[hygiene] Checking library folders exist..."
$missing = @()
foreach ($folder in $LibraryFolders) {
    if (-not (Test-Path $folder)) {
        $missing += $folder
        Write-Host "  MISSING: $folder" -ForegroundColor Red
    }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "[hygiene] ERROR: $($missing.Count) library folder(s) do not exist yet." -ForegroundColor Red
    Write-Host "  Create them first (install order step 6), or re-run this script"
    Write-Host "  after the library download (step 7). Refusing to continue —"
    Write-Host "  a silently skipped folder would be indexed by Windows Search."
    exit 1
}
Write-Host "  All library folders exist."
Write-Host ""

# --- 1. Exclude library folders from Windows Search indexing -------------
Write-Host "[hygiene] Excluding library folders from Windows Search indexing..."
$failed = @()
try {
    $manager = New-Object -ComObject "Microsoft.Windows.Search.CrawlScopeManager" -ErrorAction Stop
    foreach ($folder in $LibraryFolders) {
        try {
            # 0 = exclude. Rule is per-folder; subfolders inherit it.
            $manager.AddUserScopeRule($folder, 0, $true, $true)
            Write-Host "  excluded: $folder"
        } catch {
            $failed += $folder
            Write-Host "  FAILED to exclude: $folder ($($_.Exception.Message))" -ForegroundColor Red
        }
    }
    # Persist the rules.
    $manager.SaveAll()
} catch {
    Write-Host "  CrawlScopeManager unavailable on this build: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Manual route: Settings > Privacy & security > Searching Windows >"
    Write-Host "  'Add an excluded folder' — add each library folder by hand."
    $failed += $LibraryFolders
}
if ($failed.Count -eq 0) {
    Write-Host "  Windows Search will no longer index the library folders."
    Write-Host "  (Recoll is the indexer — see scripts/build_recoll_index.bat.)"
}
Write-Host ""

# --- 2. Pause Windows Update for 7 days -----------------------------------
Write-Host "[hygiene] Pausing Windows Update for 7 days..."
try {
    $uxKey = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    $expiry = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    if (-not (Test-Path $uxKey)) { New-Item -Path $uxKey -Force | Out-Null }
    Set-ItemProperty -Path $uxKey -Name "PauseUpdatesExpiryTime" -Value $expiry -Type String
    Set-ItemProperty -Path $uxKey -Name "PauseFeatureUpdatesStartTime" -Value (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") -Type String
    Set-ItemProperty -Path $uxKey -Name "PauseQualityUpdatesStartTime" -Value (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") -Type String
    Write-Host "  Updates paused until $expiry (UTC)."
    Write-Host "  Verify in Settings > Windows Update — if the pause did not take, set it there by hand."
} catch {
    Write-Host "  Could not set the update pause (need admin?): $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Manual route: Settings > Windows Update > Pause updates (1 week)."
}
Write-Host ""

# --- 3. Durable offline posture (round 5) ---------------------------------
# The 7-day pause expires. These two settings survive it:
#  (a) Windows Update "notify before download" (AUOptions=2) — updates
#      never download on their own; the owner chooses when to fetch them.
#  (b) Metered connection — Ethernet marked metered in the DefaultMediaCost
#      table (2 = metered), and the Wi-Fi profile if an SSID is given.
#      Metered connections do not auto-download updates or Store updates.
Write-Host "[hygiene] Setting durable offline posture (metered + notify-before-download)..."
try {
    $auKey = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    if (-not (Test-Path $auKey)) { New-Item -Path $auKey -Force | Out-Null }
    # AUOptions 2 = notify before download. NoAutoUpdate 0 = updates on,
    # but AUOptions controls the behaviour.
    Set-ItemProperty -Path $auKey -Name "AUOptions" -Value 2 -Type DWord
    Set-ItemProperty -Path $auKey -Name "NoAutoUpdate" -Value 0 -Type DWord
    Write-Host "  Windows Update: notify before download (AUOptions=2)."
} catch {
    Write-Host "  Could not set the update policy (need admin?): $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Manual route: gpedit.msc > Computer Configuration > Administrative"
    Write-Host "  Templates > Windows Components > Windows Update > Configure Automatic"
    Write-Host "  Updates > Enabled > '2 - Notify for download and auto install'."
}
try {
    $costKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\DefaultMediaCost"
    if (-not (Test-Path $costKey)) { New-Item -Path $costKey -Force | Out-Null }
    # 1 = unmetered, 2 = metered. Ethernet is the connection the laptop
    # uses when plugged in at home for drivers/updates.
    Set-ItemProperty -Path $costKey -Name "Ethernet" -Value 2 -Type DWord
    Write-Host "  Ethernet marked metered (DefaultMediaCost)."
    Write-Host "  Verify in Settings > Network & internet > Ethernet > Metered connection."
} catch {
    Write-Host "  Could not mark Ethernet metered (need admin?): $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Manual route: Settings > Network & internet > Ethernet > Metered connection > On."
}
if ($MeteredWifiSsid) {
    try {
        netsh wlan set profileparameter name="$MeteredWifiSsid" cost=Fixed | Out-Null
        Write-Host "  Wi-Fi profile '$MeteredWifiSsid' marked metered (cost=Fixed)."
    } catch {
        Write-Host "  Could not mark Wi-Fi profile metered: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  Manual route: Settings > Network & internet > Wi-Fi > the network > Metered connection > On."
    }
} else {
    Write-Host "  (No Wi-Fi SSID given — pass -MeteredWifiSsid 'YourNetwork' to mark"
    Write-Host "   the home Wi-Fi metered too, or set it by hand in Settings.)"
}
Write-Host ""

# --- 4. Field-plan hotspot reminder ----------------------------------------
Write-Host "[hygiene] Field-plan hotspot note:" -ForegroundColor Yellow
Write-Host "  The 'Field' power plan kills Wi-Fi/Bluetooth on battery — but the"
Write-Host "  hotspot IS Wi-Fi. When you serve the hub to your phone, use"
Write-Host "  scripts\serve_toggle.ps1 -Start (it switches to the Performance"
Write-Host "  plan and starts the hotspot), then serve_toggle.ps1 -Stop to go"
Write-Host "  back to Field. Serving is a deliberate mode, not the default."
Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "[hygiene] Done WITH FAILURES — $($failed.Count) folder(s) could not be excluded." -ForegroundColor Red
    Write-Host "  Fix them by hand (Settings > Privacy & security > Searching Windows),"
    Write-Host "  then re-run this script."
    exit 1
}
Write-Host "[hygiene] Done."
