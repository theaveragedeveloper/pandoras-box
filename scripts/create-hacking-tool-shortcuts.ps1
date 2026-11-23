# Create desktop shortcuts for the Mk1 hacking tools.
# The tools were installed 2026-11-21 but the installers never put
# icons on the desktop. Run via SSH; fast, no need to survive teardown.
# ASCII-only.
$desktop = "$env:USERPROFILE\Desktop"
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($Name, $Target, $WorkDir, $Args, $Icon) {
    $path = Join-Path $desktop ($Name + ".lnk")
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = $Target
    if ($WorkDir) { $sc.WorkingDirectory = $WorkDir }
    if ($Args) { $sc.Arguments = $Args }
    if ($Icon) { $sc.IconLocation = $Icon }
    $sc.Save()
    Write-Output ("created {0}" -f $path)
}

# GUI tools: open the app directly.
New-Shortcut "Wireshark" "C:\Program Files\Wireshark\Wireshark.exe" "" "" ""
New-Shortcut "Zenmap (Nmap)" "C:\Program Files (x86)\Nmap\zenmap.exe" "" "" ""
New-Shortcut "PuTTY" "C:\Program Files\PuTTY\putty.exe" "" "" ""

# Command-line tools: open a command prompt in the tool's folder,
# so the tool is ready to run. Icon set to the tool's exe.
New-Shortcut "Hashcat" "C:\Windows\System32\cmd.exe" "C:\Library\apps\hashcat" "/k" "C:\Library\apps\hashcat\hashcat.exe"
New-Shortcut "John the Ripper" "C:\Windows\System32\cmd.exe" "C:\Library\apps\john\run" "/k" "C:\Library\apps\john\run\john.exe"

# Kali Linux via WSL: opens a terminal running Kali.
New-Shortcut "Kali Linux" "C:\Windows\System32\wsl.exe" "" "-d kali-linux" ""
