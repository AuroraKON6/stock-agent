# Controlled Go/No-Go checklist for Stock Agent.
# This script never runs collection, LLM analysis, Pi model calls,
# provider approval, human approval, publication, or trading actions.

param(
    [string]$Config = "config\watchlist.local.json",
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"
$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8Encoding
[Console]::OutputEncoding = $Utf8Encoding
$OutputEncoding = $Utf8Encoding
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# Entry points must use the locked interpreter; PATH python is not a supported
# fallback because it resolves to an uncontrolled environment (e.g. Anaconda).
$PythonExe = Join-Path $ProjectRoot ".venv312\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Error "Locked interpreter not found: $PythonExe (expected .venv312; PATH python is not a supported fallback)"
    exit 2
}
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogDir = Join-Path $ProjectRoot "logs\go_live"
$SummaryPath = Join-Path $LogDir "go_live_${Stamp}.summary.json"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$StartedAt = (Get-Date).ToString("o")
$Steps = @()
$StepFailure = $false

function Run-Step {
    param([string]$Name, [string]$Command)
    $StepStarted = Get-Date
    $SafeName = $Name -replace '[^A-Za-z0-9_-]', '_'
    $StdoutPath = Join-Path $LogDir "go_live_${Stamp}.${SafeName}.out.log"
    $StderrPath = Join-Path $LogDir "go_live_${Stamp}.${SafeName}.err.log"
    $ChildScript = @"
`$ErrorActionPreference = 'Continue'
`$ProgressPreference = 'SilentlyContinue'
`$Utf8Encoding = New-Object System.Text.UTF8Encoding(`$false)
[Console]::InputEncoding = `$Utf8Encoding
[Console]::OutputEncoding = `$Utf8Encoding
`$OutputEncoding = `$Utf8Encoding
`$env:PYTHONIOENCODING = 'utf-8'
$Command
exit `$LASTEXITCODE
"@
    $EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ChildScript))
    try {
        $Child = Start-Process -FilePath "powershell.exe" `
            -ArgumentList @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $EncodedCommand) `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
        $Code = $Child.ExitCode
    } catch {
        $Code = 1
        $_.Exception.GetType().Name | Out-File -LiteralPath $StderrPath -Encoding utf8 -Append
    }
    if (Test-Path -LiteralPath $StdoutPath) {
        Get-Content -LiteralPath $StdoutPath -Encoding UTF8 | ForEach-Object { Write-Host $_ }
    }
    if (Test-Path -LiteralPath $StderrPath) {
        Get-Content -LiteralPath $StderrPath -Encoding UTF8 | ForEach-Object { Write-Warning $_ }
    }
    $StepFinished = Get-Date
    $script:Steps += @{
        name = $Name
        command = $Command
        started_at = $StepStarted.ToString("o")
        finished_at = $StepFinished.ToString("o")
        duration_seconds = [math]::Round(($StepFinished - $StepStarted).TotalSeconds, 3)
        exit_code = $Code
        stdout_path = $StdoutPath
        stderr_path = $StderrPath
    }
    if ($Code -ne 0) {
        $script:StepFailure = $true
    }
}

if (-not $SkipTests) {
    Run-Step -Name "unit_tests" -Command "& '$PythonExe' -m unittest discover -s tests"
}
Run-Step -Name "pi_runtime_validation" -Command "& '$PythonExe' -m stock_agent pi-runtime-status --config $Config --require-ready"
Run-Step -Name "pi_handoff_validation" -Command "& '$PythonExe' -m stock_agent agent-handoff --config $Config --check"
Run-Step -Name "install_dryrun" -Command "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_scheduled_tasks.ps1 -DryRun"
Run-Step -Name "production_pilot_sync" -Command "& '$PythonExe' -m stock_agent production-pilot-sync --config $Config"
Run-Step -Name "workspace_sync" -Command "& '$PythonExe' -m stock_agent workspace-sync"
Run-Step -Name "dashboard" -Command "& '$PythonExe' -m stock_agent dashboard --config $Config"
Run-Step -Name "alerts" -Command "& '$PythonExe' -m stock_agent alerts --config $Config"
Run-Step -Name "final_doctor" -Command "& '$PythonExe' -m stock_agent doctor --config $Config"
Run-Step -Name "production_pilot_status" -Command "& '$PythonExe' -m stock_agent production-pilot-status --config $Config"

function Get-StockAgentScheduledTasks {
    $Expected = @(
        "StockAgent_DailyResearch",
        "StockAgent_PilotReconcile",
        "StockAgent_DailyDashboard",
        "StockAgent_WeeklyReview"
    )
    try {
        $Registered = @(Get-ScheduledTask -TaskName StockAgent_* -ErrorAction SilentlyContinue)
    } catch {
        $Registered = @()
    }
    $Rows = @()
    foreach ($Name in $Expected) {
        $Task = $Registered | Where-Object { $_.TaskName -eq $Name } | Select-Object -First 1
        $Row = @{
            task_name = $Name
            installed = $false
            state = "missing"
            next_run_time = ""
            last_task_result = ""
        }
        if ($null -ne $Task) {
            $Row.installed = $true
            $Row.state = [string]$Task.State
            try {
                $Info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
                $Row.next_run_time = [string]$Info.NextRunTime
                $Row.last_task_result = [string]$Info.LastTaskResult
            } catch {
                $Row.last_task_result = "unavailable"
            }
        }
        $Rows += $Row
    }
    return $Rows
}

$DoctorPath = Join-Path $ProjectRoot "reports\health\latest.json"
$PilotPath = Join-Path $ProjectRoot "reports\production_pilot\latest.json"
$Doctor = @{}
$Pilot = @{}
try { $Doctor = Get-Content -Raw -Encoding UTF8 -LiteralPath $DoctorPath | ConvertFrom-Json } catch { $Doctor = @{} }
try { $Pilot = Get-Content -Raw -Encoding UTF8 -LiteralPath $PilotPath | ConvertFrom-Json } catch { $Pilot = @{} }

$DoctorOverall = if ($Doctor.overall_status) { [string]$Doctor.overall_status } else { "ERROR" }
$PilotDecision = if ($Pilot.go_live_decision) { [string]$Pilot.go_live_decision } else { "NO_GO" }
$TechnicalReadiness = if ($Pilot.technical_readiness) { [string]$Pilot.technical_readiness } else { "BLOCKED" }
$ExitCode = 0
if ($StepFailure -or $DoctorOverall -eq "FAIL" -or $DoctorOverall -eq "ERROR" -or $TechnicalReadiness -ne "READY") {
    $ExitCode = 1
} elseif ($PilotDecision -ne "GO") {
    $ExitCode = 3
}

$ScheduledTasks = @(Get-StockAgentScheduledTasks)
$ScheduledTasksInstalled = (($ScheduledTasks | Where-Object { -not $_.installed }).Count -eq 0)
$FinishedAt = (Get-Date).ToString("o")
$Summary = @{
    started_at = $StartedAt
    finished_at = $FinishedAt
    stamp = $Stamp
    project_root = $ProjectRoot
    config = $Config
    overall_status = $PilotDecision
    technical_readiness = $TechnicalReadiness
    doctor_status = $DoctorOverall
    exit_code = $ExitCode
    scheduled_tasks_installed = $ScheduledTasksInstalled
    scheduled_tasks = $ScheduledTasks
    steps = $Steps
    safety_boundary = "Internal research only; no automatic approval, activation, publication, trading, orders, or position changes."
    artifacts = @{
        doctor_report = Join-Path $ProjectRoot "reports\health\latest.md"
        production_pilot = Join-Path $ProjectRoot "reports\production_pilot\latest.md"
        production_pilot_json = $PilotPath
        provider_registry = Join-Path $ProjectRoot "reports\production_pilot\provider_registry_latest.md"
        workspace_inbox = Join-Path $ProjectRoot "reports\workspace\latest.md"
        workspace_inbox_json = Join-Path $ProjectRoot "reports\workspace\latest.json"
        dashboard = Join-Path $ProjectRoot "reports\dashboard\latest.html"
        notifications = Join-Path $ProjectRoot "reports\notifications\latest.md"
    }
}
$Summary | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $SummaryPath -Encoding utf8

Write-Host ""
Write-Host "=== Controlled Go/No-Go Summary ==="
Write-Host "Technical readiness: $TechnicalReadiness"
Write-Host "Business decision: $PilotDecision"
Write-Host "Doctor: $DoctorOverall"
Write-Host "Scheduled tasks installed: $ScheduledTasksInstalled"
Write-Host "Exit code: $ExitCode"
Write-Host "Pilot report: reports\production_pilot\latest.md"
Write-Host "Summary: $SummaryPath"
if ($PilotDecision -ne "GO") {
    Write-Host "[NO_GO] Technical health does not authorize business production use."
}

exit $ExitCode
