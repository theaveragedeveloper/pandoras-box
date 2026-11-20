@echo off
REM Build the Recoll index for Pandora's Box Mk1 (Dell 5424).
REM Run ONCE, plugged in, after the library folders exist — indexing the
REM ~85 GB library is a heavy one-time job. Re-run whenever folders change.
REM
REM Usage: build_recoll_index.bat [config_dir]
REM   config_dir defaults to C:\Library\recoll-conf
REM   Library folders are read from recoll.conf "topdirs". If no
REM   recoll.conf exists yet, this script copies the template from
REM   fixtures\recoll.conf.template (topdirs = the FieldManuals and
REM   MyFiles folders) — check it matches your library before building.
REM   Without a topdirs line, recollindex builds an index of nothing.
REM
REM Time budget: Windows Recoll is single-threaded over ~85-200 GB incl.
REM the TREX vault (4,215 items across collections / 3,511 distinct
REM publications — resolved round 6) — no measured time yet. Pilot first if you want a
REM number: point a throwaway config at 200 PDFs, time it, scale up.
REM This is a plugged-in job — do not run it on battery.
REM
REM NOTE (verified Nov 13, 2026, Recoll user manual 1.45.0 §3.4):
REM recollq — the CLI the hub queries with — is "not always built or
REM installed by default" on Windows ("as recoll -t works the same").
REM The hub already handles that: it uses recollq if present, otherwise
REM "recoll -t". So this script only needs recollindex, which ships in
REM the Windows package. If NEITHER recollq nor recoll is on PATH after
REM install, check the install dir (typically C:\Program Files\Recoll)
REM and add it to PATH, or the hub's Recoll source will report
REM unavailable and fall back to sample data.
REM
REM Also note: the Windows Recoll port has NO multithreading (manual),
REM so indexing is slower than on Linux — expect it to take a while.
REM Smart App Control can refuse to run the installer until SAC is
REM temporarily disabled for the install (Recoll Windows FAQ).

setlocal
set CONF=%~1
if "%CONF%"=="" set CONF=C:\Library\recoll-conf

where recollindex >nul 2>nul
if errorlevel 1 (
  echo [recoll] recollindex not found on PATH.
  echo   Install Recoll for Windows first (recoll.org), then re-run this.
  echo   If it is installed, add its folder to PATH, e.g.:
  echo     setx PATH "%%PATH%%;C:\Program Files\Recoll"
  pause
  exit /b 1
)

REM Make sure a recoll.conf exists and names folders to index.
if not exist "%CONF%\recoll.conf" (
  if not exist "%~dp0..\fixtures\recoll.conf.template" (
    echo [recoll] No recoll.conf in %CONF% and no template found.
    echo   Create one with a topdirs line pointing at the library folders.
    pause
    exit /b 1
  )
  if not exist "%CONF%" mkdir "%CONF%"
  copy "%~dp0..\fixtures\recoll.conf.template" "%CONF%\recoll.conf" >nul
  echo [recoll] Copied recoll.conf template to %CONF% — check topdirs
  echo   matches your library folders before building.
)
findstr /R /C:"^topdirs *= *[^ ]" "%CONF%\recoll.conf" >nul
if errorlevel 1 (
  echo [recoll] recoll.conf has no topdirs line — nothing would be indexed.
  echo   Edit %CONF%\recoll.conf and set topdirs to the library folders,
  echo   e.g.: topdirs = C:\Library\FieldManuals C:\Library\MyFiles
  pause
  exit /b 1
)

echo [recoll] Building index for config: %CONF%
echo [recoll] This is a plugged-in job — do not run it on battery.
echo [recoll] Press Ctrl+C to stop, or any key to start.
pause >nul

recollindex -c "%CONF%"
if errorlevel 1 (
  echo [recoll] recollindex failed (exit %errorlevel%). Check the config
  echo   dir and that the library folders in recoll.conf exist.
  pause
  exit /b 1
)

echo.
echo [recoll] Index built. Tell the hub the build date so staleness is
echo   visible: in config.json, set
echo     "library": { "recoll_index_date": "YYYY-MM-DD", ... }
echo   (or edit it through the hub later). Then restart the hub.
echo [recoll] Done.
pause
