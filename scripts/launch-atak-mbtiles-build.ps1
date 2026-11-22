# Launch the ATAK MBTiles build detached (CIM) via its .bat file,
# the same pattern deploy-print-map.ps1 uses for the hub relaunch.
# Run only after the MA topo z13-15 download has finished
# (C:\Library\Maps\topo-ma-download.log shows DONE).
$cmd = "cmd /c C:\Library\run-atak-mbtiles-build.bat"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Host "LAUNCH-ATAK-BUILD rc=$($r.ReturnValue) pid=$($r.ProcessId)"
