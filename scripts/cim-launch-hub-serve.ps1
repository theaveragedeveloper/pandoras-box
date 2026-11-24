# Detached launcher for the hub in serving mode (after the WikiHow config change).
# Runs launch-hub-serve.bat (hub.py --serve, full Python path) via CIM
# (Win32_Process Create), so it survives SSH teardown. ASCII-only.
$launcher = "C:\Library\search-hub\launch-hub-serve.bat"
$cmd = 'cmd /c "' + $launcher + '"'
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("CIM create returned: {0}, process id: {1}" -f $result.ReturnValue, $result.ProcessId)
