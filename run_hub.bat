@echo off
REM Search Hub launcher for Windows 11 (Dell 5424).
REM Run from this folder. Default bind is localhost-only
REM (127.0.0.1:8899) — the hub is only reachable on the LAN / hotspot
REM in serving mode:  run_hub.bat --serve
REM First serving run: allow the Windows Firewall prompt on private
REM networks so the phone on the laptop hotspot can reach it.
REM
REM Starts kiwix-serve first (if installed and the library exists),
REM waits for its port, then starts the hub — so after a reboot the
REM search box answers from the real ZIMs instead of sample data.
REM If kiwix-serve is not installed yet, the hub still starts and its
REM Kiwix source reports unavailable until the server is up.
REM
REM Reliability (round 9):
REM - If 8899 is busy, hub.py tries 8899..8908 and prints the port it
REM   took; the actual port is also written to hub.port in this folder
REM   (the watchdog and serve_toggle.ps1 read it).
REM - run_hub.bat --no-pause: no "press any key" wait at the end, and
REM   the hub's exit code is passed through — the autostart task uses
REM   this so Task Scheduler can see a crash and restart it. Run
REM   without --no-pause (double-click) and the window stays open on
REM   an error so you can read it.
REM
REM Python must be on PATH: when installing Python on the laptop, tick
REM "Add Python to PATH" (audit flaw #14). If this script reports
REM "python not found", re-run the Python installer and tick that box.
cd /d "%~dp0"

set NOPAUSE=
if /i "%~1"=="--no-pause" set NOPAUSE=1

where python >nul 2>nul
if errorlevel 1 (
  echo [hub] ERROR: python not found on PATH.
  echo   Re-run the Python installer and tick "Add Python to PATH",
  echo   then run this again.
  if not defined NOPAUSE pause
  exit /b 1
)

set KIWIX_LIB=C:\Library\kiwix\library.xml
where kiwix-serve >nul 2>nul
if errorlevel 1 (
  echo [hub] kiwix-serve not found on PATH — starting the hub alone.
  echo   Install Kiwix and build the library, then re-run this.
  goto :hub
)
if not exist "%KIWIX_LIB%" (
  echo [hub] No Kiwix library at %KIWIX_LIB% — starting the hub alone.
  echo   Build the library first, then re-run this.
  goto :hub
)
echo [hub] Starting kiwix-serve on port 8080 (localhost-only)...
REM --address=127.0.0.1 (audit 2026-11-20, F18): without it kiwix-serve
REM binds all interfaces, so a reboot/autostart would expose port 8080
REM to the hotspot. The phone never talks to kiwix-serve directly —
REM the hub proxies Kiwix articles via /kiwix/ — so localhost-only is
REM correct in every mode, including --serve.
start "kiwix-serve" /min kiwix-serve --address=127.0.0.1 --port 8080 --library "%KIWIX_LIB%"
where powershell >nul 2>nul
if errorlevel 1 (
  echo [hub] powershell not found — cannot wait for kiwix-serve; starting the hub anyway.
  goto :hub
)
powershell -NoProfile -Command "$ok=$false; for ($i=0; $i -lt 30; $i++) { try { $c = New-Object System.Net.Sockets.TcpClient('127.0.0.1',8080); $c.Close(); $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if (-not $ok) { Write-Host '[hub] WARNING: kiwix-serve did not answer on 8080 within 30s — the hub will still start, Kiwix results will be unavailable until it is up.' }"
echo [hub] kiwix-serve is up.

:hub
python hub.py %*
set HUB_EXIT=%ERRORLEVEL%
if defined NOPAUSE exit /b %HUB_EXIT%
pause
exit /b %HUB_EXIT%
