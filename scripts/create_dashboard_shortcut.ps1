# Create desktop shortcut for dashboard
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_dashboard_shortcut.ps1

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TargetPath = Join-Path $ProjectRoot "reports\dashboard\latest.html"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "StockAgentDashboard.lnk"

if (-not (Test-Path $TargetPath)) {
    Write-Host "Dashboard file not found: $TargetPath"
    Write-Host "Run first: .\.venv312\Scripts\python.exe -m stock_agent dashboard --config config/watchlist.local.json"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Stock Agent Dashboard"
$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath"
Write-Host "Target: $TargetPath"
