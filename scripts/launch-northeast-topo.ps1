# Launch the Northeast topo z12-15 download detached (CIM) via its .bat file,
# the same pattern deploy-print-map.ps1 uses for the hub relaunch.
# ASCII-only.
$cmd = "cmd /c C:\Library\run-northeast-topo-download.bat"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Host "LAUNCH-NORTHEAST-TOPO rc=$($r.ReturnValue) pid=$($r.ProcessId)"
