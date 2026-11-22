# Launch the MA topo z13-15 download detached (CIM) via its .bat file,
# the same pattern deploy-print-map.ps1 uses for the hub relaunch.
$cmd = "cmd /c C:\Library\run-ma-topo-download.bat"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Host "LAUNCH-MA-TOPO rc=$($r.ReturnValue) pid=$($r.ProcessId)"
