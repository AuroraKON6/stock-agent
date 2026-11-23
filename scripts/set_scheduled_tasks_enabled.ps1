# Explicitly disable or re-enable the Stock Agent legacy Windows Scheduled Tasks.
#
# R25 cutover contract (docs/GLM_REFACTOR_TASKBOOK_20261020.md R25):
#   - The old PowerShell entry is only ever EXPLICITLY disabled/restored.
#     This script is that explicit step; it never unregisters or overwrites
#     a task definition (uninstall_scheduled_tasks.ps1 stays the removal path).
#   - Forward cutover:  run with -Disable BEFORE `stock_agent cutover-switch
#     --to container-worker`, and record the same fact in the ownership
#     registry (disable_legacy / the cutover runbook does that).
#   - Rollback:         run with -Enable AFTER the registry switch back to
#     legacy-powershell and the increment reconciliation recorded.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_scheduled_tasks_enabled.ps1 -Disable -DryRun
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_scheduled_tasks_enabled.ps1 -Disable
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_scheduled_tasks_enabled.ps1 -Enable

param(
    [Parameter(Mandatory = $true, ParameterSetName = "Disable")]
    [switch]$Disable,
    [Parameter(Mandatory = $true, ParameterSetName = "Enable")]
    [switch]$Enable,
    [switch]$DryRun,
    [string]$Reason = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RecordDir = Join-Path $ProjectRoot "logs\scheduled_task_state"
$TaskNames = @(
    "StockAgent_DailyResearch",
    "StockAgent_DailyDashboard",
    "StockAgent_PilotReconcile",
    "StockAgent_WeeklyReview"
)
$Action = if ($Disable) { "disable" } else { "enable" }

Write-Host "=== Stock Agent Scheduled Tasks: $Action ==="
Write-Host "Project root: $ProjectRoot"
Write-Host ""

$changed = 0
$missing = 0
foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "[MISSING] $name (nothing to $Action)"
        $missing++
        continue
    }
    $already = if ($Disable) { $task.State -eq "Disabled" } else { $task.State -ne "Disabled" }
    if ($already) {
        Write-Host "[SKIP] $name already in the requested state ($($task.State))"
        continue
    }
    if ($DryRun) {
        Write-Host "[DRY RUN] would $Action $name (state: $($task.State))"
        continue
    }
    if ($Disable) {
        $task | Disable-ScheduledTask -ErrorAction Stop | Out-Null
    } else {
        $task | Enable-ScheduledTask -ErrorAction Stop | Out-Null
    }
    Write-Host "[OK] $name -> $((Get-ScheduledTask -TaskName $name).State)"
    $changed++
}

if (-not $DryRun) {
    New-Item -ItemType Directory -Path $RecordDir -Force | Out-Null
    $RecordPath = Join-Path $RecordDir "${Action}_$Stamp.json"
    [pscustomobject]@{
        action    = $Action
        at        = (Get-Date).ToString("o")
        reason    = $Reason
        changed   = $changed
        missing   = $missing
        task_names = $TaskNames
        states    = ($TaskNames | ForEach-Object {
            $t = Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
            if ($null -eq $t) { "$_=missing" } else { "$_=$($t.State)" }
        })
    } | ConvertTo-Json | Out-File -LiteralPath $RecordPath -Encoding utf8
    Write-Host ""
    Write-Host "Record: $RecordPath"
}

Write-Host ""
if ($DryRun) {
    Write-Host "[DRY RUN] No task state was changed."
} else {
    Write-Host "Done. $changed task(s) $Action`d, $missing missing."
    if ($Disable) {
        Write-Host "Next (forward cutover): record the disable marker in the ownership registry and run cutover-switch --to container-worker."
    } else {
        Write-Host "Rollback complete: the legacy entry may resume scheduling."
    }
}
