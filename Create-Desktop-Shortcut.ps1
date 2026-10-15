# Create-Desktop-Shortcut.ps1 - Creates a Windows Desktop shortcut for SizeScanner
$desktop = [System.Environment]::GetFolderPath('Desktop')
$scriptDir = $PSScriptRoot
$shortcutPath = Join-Path $desktop "SizeScanner.lnk"

$exePath = Join-Path $scriptDir "dist\SizeScanner.exe"
$batPath = Join-Path $scriptDir "Run-SizeScanner.bat"
$iconPath = Join-Path $scriptDir "icon.ico"

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)

if (Test-Path $exePath) {
    $shortcut.TargetPath = $exePath
} else {
    $shortcut.TargetPath = $batPath
}

$shortcut.WorkingDirectory = $scriptDir
$shortcut.Description = "SizeScanner - Windows Folder Size Explorer"

if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}

$shortcut.Save()
Write-Host "Created Desktop shortcut at: $shortcutPath"
