# Uninstall Stock Agent Windows Scheduled Tasks
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall_scheduled_tasks.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $ProjectRoot "logs\scheduled_task_backups\uninstall_$Stamp"
$TaskNames = @(
    "StockAgent_DailyResearch",
    "StockAgent_PilotReconcile",
    "StockAgent_DailyDashboard",
    "StockAgent_WeeklyReview"
)

Write-Host "=== Uninstall Stock Agent Scheduled Tasks ==="
Write-Host ""

$tasks = @()
foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -ne $task) { $tasks += $task }
}

if (-not $tasks) {
    Write-Host "No StockAgent_ tasks found."
    exit 0
}

foreach ($task in $tasks) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-File `
        -LiteralPath (Join-Path $BackupDir "$($task.TaskName).xml") -Encoding utf8
    Write-Host "Backed up: $($task.TaskName)"
    Write-Host "Removing: $($task.TaskName)"
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
}

Write-Host ""
Write-Host "Done. Removed $($tasks.Count) task(s)."
Write-Host "Backup: $BackupDir"
