# Launch the WikiHow ZIM download (51 GB) detached via aria2c.
# Run this script via CIM (see cim-launch-wikihow.ps1) so the download
# survives SSH teardown. ASCII-only.
$aria2 = "$env:USERPROFILE\AppData\Local\Microsoft\WinGet\Links\aria2c.exe"
$dest = "C:\Library\kiwix\wikihow_en_maxi_2023-03.zim"
$url = "https://archive.download.kiwix.org/zim/wikihow/wikihow_en_maxi_2023-03.zim"
$log = "C:\Library\kiwix\wikihow-download.log"
$expected = 51256446526

if (Test-Path $dest) {
    $len = (Get-Item $dest).Length
    if ($len -eq $expected) {
        Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') already complete ($len bytes), nothing to do."
        exit 0
    }
    Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') resuming partial file ($len of $expected bytes)."
}
$args = @("--dir=C:\Library\kiwix", "--out=wikihow_en_maxi_2023-03.zim", "--log=$log", "--file-allocation=none", "-x", "4", "-s", "4", "--check-certificate=true", $url)
Start-Process -FilePath $aria2 -ArgumentList $args -WindowStyle Hidden
