# Detached launcher for kiwix-serve (after the WikiHow ZIM was added).
# Starts kiwix-serve on 127.0.0.1:8080 with the updated library.xml,
# via CIM (Win32_Process Create), so it survives SSH teardown.
# ASCII-only.
$kiwixServe = "C:\Library\apps\kiwix-tools\kiwix-serve.exe"
$lib = "C:\Library\kiwix\library.xml"
$cmd = '"' + $kiwixServe + '" --address=127.0.0.1 --port=8080 --library "' + $lib + '"'
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("CIM create returned: {0}, process id: {1}" -f $result.ReturnValue, $result.ProcessId)
