# Detached launcher for the WikiHow download.
# Creates a detached PowerShell running launch-wikihow-download.ps1
# via CIM (Win32_Process Create), so it survives SSH teardown.
# ASCII-only.
$launcher = "C:\Library\search-hub\scripts\launch-wikihow-download.ps1"
$cmd = 'powershell.exe -ExecutionPolicy Bypass -File "' + $launcher + '"'
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("CIM create returned: {0}, process id: {1}" -f $result.ReturnValue, $result.ProcessId)
