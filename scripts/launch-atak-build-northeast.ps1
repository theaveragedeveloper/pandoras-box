# Launch the Northeast ATAK MBTiles build detached (CIM) via its .bat file.
# Run only after the Northeast topo z12-15 download has finished
# (C:\Library\Maps\topo-northeast-download.log shows DONE).
# ASCII-only.
$cmd = "cmd /c C:\Library\run-atak-build-northeast.bat"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Host "LAUNCH-ATAK-BUILD-NE rc=$($r.ReturnValue) pid=$($r.ProcessId)"
