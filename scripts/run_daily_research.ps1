# Scheduled daily research runner
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode daily
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode review
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode dashboard
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode pilot
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode daily -SkipPi

param(
    [ValidateSet("daily", "review", "dashboard", "pilot")]
    [string]$Mode = "daily",
    [ValidateSet("manual", "scheduled")]
    [string]$TriggerSource = "manual",
    [Alias("SkipOpenClaw")]
    [switch]$SkipPi,
    [string]$Config = "config/watchlist.local.json"
)

$ErrorActionPreference = "Continue"
$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8Encoding
[Console]::OutputEncoding = $Utf8Encoding
$OutputEncoding = $Utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$ProxyCompatibilityApplied = $false
if ([string]::IsNullOrWhiteSpace($env:HTTPS_PROXY) -and -not [string]::IsNullOrWhiteSpace($env:HTPPS_PROXY)) {
    # Preserve the user-level HTPPS_PROXY spelling while providing the standard
    # variable required by urllib and other scheduled network clients.
    $env:HTTPS_PROXY = $env:HTPPS_PROXY
    $ProxyCompatibilityApplied = $true
}
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# Entry points must use the locked interpreter; PATH python is not a supported
# fallback because it resolves to an uncontrolled environment (e.g. Anaconda).
$PythonExe = Join-Path $ProjectRoot ".venv312\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Error "Locked interpreter not found: $PythonExe (expected .venv312; PATH python is not a supported fallback)"
    exit 2
}
$Stamp = (Get-Date -Format "yyyyMMddTHHmmss")
$LogDir = Join-Path $ProjectRoot "logs\scheduled"
$SummaryPath = Join-Path $LogDir "run_${Stamp}.summary.json"
$TextLogPath = Join-Path $LogDir "run_${Stamp}.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

try {
    Start-Transcript -LiteralPath $TextLogPath -Force | Out-Null
} catch {
    Write-Warning "Unable to start scheduled-run transcript: $($_.Exception.Message)"
}

$StartedAt = (Get-Date).ToString("o")
$Commands = @()
$Errors = @()
$ExitCode = 0
$AgentRuntime = "pi"
$UsedPi = $false
$AgentHandoffPath = ""
$AgentHandoffPublicSignalCount = 0
$AgentHandoffCallRequired = $false
$PiSkipReason = "not_applicable_for_mode"
$PiModelCallCount = 0
$PiResultPath = ""
$ReportPath = ""
$DashboardPath = ""
$NotificationPath = ""
$WorkspaceLatestPath = ""
$WorkspaceLatestJsonPath = ""
$MarketCachePath = ""
$EvidenceVaultPath = ""
$ResearchOperationsPath = ""
$ReviewQueuePath = ""
$ProductionPilotPath = ""
$BusinessOnboardingPath = ""
$RunCoordinationPath = Join-Path $ProjectRoot "reports\run_coordination\latest.json"
$RunCoordinationStatus = "UNKNOWN"
$RunCoordinationPreflightSucceeded = $false
$RunManifestPointerPath = Join-Path $ProjectRoot "reports\run_coordination\latest_manifest.json"
$RunManifestPath = ""
$RunManifestSha256 = ""
$RunManifestRunId = ""
$RunId = ""
$ScheduledDeliveryTargetSeconds = 300
$ScheduledSloStatus = "UNKNOWN"
$DurationSeconds = 0

try {
    $ConfigFullPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $ProjectRoot $Config }
    if (Test-Path $ConfigFullPath) {
        $ConfigData = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigFullPath | ConvertFrom-Json
        if ($ConfigData.model_operations.service_levels.scheduled_delivery_target_seconds) {
            $ScheduledDeliveryTargetSeconds = [double]$ConfigData.model_operations.service_levels.scheduled_delivery_target_seconds
        }
    }
} catch {
    $Errors += @{ step = "load-scheduled-slo"; error = $_.Exception.Message }
}

function Run-Step {
    param([string]$Name, [string]$Command)
    $StepStartedAt = Get-Date
    $SafeName = $Name -replace '[^A-Za-z0-9_-]', '_'
    $StepStdoutPath = Join-Path $LogDir "run_${Stamp}.${SafeName}.out.log"
    $StepStderrPath = Join-Path $LogDir "run_${Stamp}.${SafeName}.err.log"
    Write-Host ">>> $Name"
    Write-Host "    $Command"
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
            -RedirectStandardOutput $StepStdoutPath -RedirectStandardError $StepStderrPath
        $code = $Child.ExitCode
    } catch {
        $code = 1
        $_.Exception.Message | Out-File -LiteralPath $StepStderrPath -Encoding utf8 -Append
    }
    if (Test-Path -LiteralPath $StepStdoutPath) {
        Get-Content -LiteralPath $StepStdoutPath -Encoding UTF8 | ForEach-Object { Write-Host $_ }
    }
    if (Test-Path -LiteralPath $StepStderrPath) {
        Get-Content -LiteralPath $StepStderrPath -Encoding UTF8 | ForEach-Object { Write-Warning $_ }
    }
    $StepFinishedAt = Get-Date
    $script:Commands += @{
        name = $Name
        command = $Command
        stdout_path = $StepStdoutPath
        stderr_path = $StepStderrPath
        started_at = $StepStartedAt.ToString("o")
        finished_at = $StepFinishedAt.ToString("o")
        duration_seconds = [math]::Round(($StepFinishedAt - $StepStartedAt).TotalSeconds, 3)
        exit_code = $code
    }
    if ($code -ne 0) {
        $script:Errors += @{ step = $Name; exit_code = $code }
        $script:ExitCode = $code
        Write-Host "    [WARN] $Name exited with code $code"
    }
}

# Daily data plane: one collection + LLM run only.
Run-Step -Name "run-coordination-preflight" -Command "& '$PythonExe' -m stock_agent run-coordination-status --config $Config --require-ready"
$RunCoordinationPreflightSucceeded = ($Commands.Count -gt 0 -and $Commands[-1].exit_code -eq 0)
if (Test-Path -LiteralPath $RunCoordinationPath) {
    try {
        $CoordinationData = Get-Content -Raw -Encoding UTF8 -LiteralPath $RunCoordinationPath | ConvertFrom-Json
        $RunCoordinationStatus = [string]$CoordinationData.technical_status
    } catch {
        $Errors += @{ step = "read-run-coordination-status"; error = $_.Exception.Message }
        $ExitCode = 1
    }
}

if ($Mode -eq "daily") {
    Run-Step -Name "market-cache-seed" -Command "& '$PythonExe' -m stock_agent market-cache-seed --config $Config"
    $MarketCachePath = Join-Path $ProjectRoot "data\cache\market_quotes\latest.json"
    Run-Step -Name "agent-run" -Command "& '$PythonExe' -m stock_agent agent-run --config $Config --trigger-source $TriggerSource"
    $AgentRunSucceeded = ($Commands.Count -gt 0 -and $Commands[-1].exit_code -eq 0)

    # Bind this summary to the EXACT run identity: the manifest pointer written
    # by the agent-run must name the same run_id as the coordination projection
    # refreshed by that same run. A stale or mismatched pointer is recorded as
    # an error instead of silently binding a previous run's manifest.
    if ($AgentRunSucceeded) {
        $PointerRunId = ""
        try {
            if (Test-Path -LiteralPath $RunManifestPointerPath) {
                $PointerData = Get-Content -Raw -Encoding UTF8 -LiteralPath $RunManifestPointerPath | ConvertFrom-Json
                $PointerRunId = [string]$PointerData.run_id
                $RunManifestRunId = $PointerRunId
                $PointerManifestPath = [string]$PointerData.manifest_path
                if (-not [string]::IsNullOrWhiteSpace($PointerManifestPath)) {
                    $PointerManifestAbs = if ([System.IO.Path]::IsPathRooted($PointerManifestPath)) { $PointerManifestPath } else { Join-Path $ProjectRoot $PointerManifestPath }
                    if (Test-Path -LiteralPath $PointerManifestAbs) {
                        $RunManifestPath = $PointerManifestAbs
                        $RunManifestSha256 = [string]$PointerData.manifest_sha256
                    }
                }
            }
        } catch {
            $Errors += @{ step = "read-run-manifest-pointer"; error = $_.Exception.Message }
        }
        $CoordinationRunId = ""
        try {
            if (Test-Path -LiteralPath $RunCoordinationPath) {
                $CoordDataNow = Get-Content -Raw -Encoding UTF8 -LiteralPath $RunCoordinationPath | ConvertFrom-Json
                $CoordinationRunId = [string]$CoordDataNow.latest_run_id
                if ([string]::IsNullOrWhiteSpace($CoordinationRunId)) {
                    $CoordinationRunId = [string]$CoordDataNow.run_id
                }
            }
        } catch {
            $Errors += @{ step = "read-coordination-run-id"; error = $_.Exception.Message }
        }
        $RunId = if (-not [string]::IsNullOrWhiteSpace($PointerRunId)) { $PointerRunId } else { $CoordinationRunId }
        if ([string]::IsNullOrWhiteSpace($RunManifestPath)) {
            $Errors += @{ step = "run-manifest-binding"; error = "agent-run succeeded but no run manifest pointer was written" }
        } elseif ((-not [string]::IsNullOrWhiteSpace($PointerRunId)) -and
                  (-not [string]::IsNullOrWhiteSpace($CoordinationRunId)) -and
                  ($PointerRunId -ne $CoordinationRunId)) {
            $Errors += @{ step = "run-manifest-binding"; error = "manifest pointer run_id ($PointerRunId) does not match coordination run_id ($CoordinationRunId)" }
        }
    }

    if ($AgentRunSucceeded) {
        Run-Step -Name "evidence-vault-status" -Command "& '$PythonExe' -m stock_agent evidence-vault-status --config $Config --require-ready"
        $EvidenceVaultSucceeded = ($Commands.Count -gt 0 -and $Commands[-1].exit_code -eq 0)
        $EvidenceVaultPath = Join-Path $ProjectRoot "reports\evidence_vault\latest.md"
    } else {
        $EvidenceVaultSucceeded = $false
    }
    if ($AgentRunSucceeded -and $EvidenceVaultSucceeded) {
        if ($SkipPi) {
            Run-Step -Name "agent-handoff" -Command "& '$PythonExe' -m stock_agent agent-handoff --config $Config"
        } else {
            Run-Step -Name "pi-handoff-run" -Command "& '$PythonExe' -m stock_agent pi-handoff-run --config $Config"
        }
    }
    $AgentHandoffPath = Join-Path $ProjectRoot "reports\agent_handoff\latest.json"
    $PiResultPath = Join-Path $ProjectRoot "reports\pi_agent\public_handoff\latest.json"
    $HandoffStepSucceeded = (
        $AgentRunSucceeded -and $Commands.Count -gt 0 -and
        $Commands[-1].name -in @("agent-handoff", "pi-handoff-run") -and $Commands[-1].exit_code -eq 0
    )
    if ($HandoffStepSucceeded -and (Test-Path -LiteralPath $AgentHandoffPath)) {
        try {
            $HandoffData = Get-Content -Raw -Encoding UTF8 -LiteralPath $AgentHandoffPath | ConvertFrom-Json
            $AgentHandoffPublicSignalCount = [int]$HandoffData.public_signal_count
            $AgentHandoffCallRequired = [bool]$HandoffData.external_llm_call_required
            $PiSkipReason = [string]$HandoffData.call_decision_reason
        } catch {
            $Errors += @{ step = "read-agent-handoff"; error = $_.Exception.Message }
            $ExitCode = 1
            $PiSkipReason = "handoff_unreadable"
        }
    } else {
        $PiSkipReason = if (-not $AgentRunSucceeded) {
            "agent_run_failed"
        } elseif (-not $EvidenceVaultSucceeded) {
            "evidence_vault_technical_check_failed"
        } else {
            "handoff_generation_failed"
        }
    }
    if ($HandoffStepSucceeded -and -not $SkipPi -and (Test-Path -LiteralPath $PiResultPath)) {
        try {
            $PiResultData = Get-Content -Raw -Encoding UTF8 -LiteralPath $PiResultPath | ConvertFrom-Json
            if ([string]$PiResultData.handoff_payload_sha256 -ne [string]$HandoffData.payload_sha256) {
                throw "Pi result handoff hash does not match the current handoff"
            }
            $UsedPi = [bool]$PiResultData.called
            $PiModelCallCount = [int]$PiResultData.model_call_count
            $PiSkipReason = [string]$PiResultData.skip_reason
        } catch {
            $Errors += @{ step = "read-pi-result"; error = $_.Exception.Message }
            $ExitCode = 1
            $PiSkipReason = "pi_result_unreadable_or_stale"
        }
    } elseif ($SkipPi -and $AgentHandoffCallRequired) {
        $PiSkipReason = "operator_skip_pi"
    }
    Run-Step -Name "review-queue-sync" -Command "& '$PythonExe' -m stock_agent review-queue-sync --config $Config"
    $ReviewQueuePath = Join-Path $ProjectRoot "reports\review_queue\latest.md"
} elseif ($Mode -eq "review") {
    Run-Step -Name "review-summary" -Command "& '$PythonExe' -m stock_agent review-summary"
    Run-Step -Name "review-queue-sync" -Command "& '$PythonExe' -m stock_agent review-queue-sync --config $Config"
    $ReviewQueuePath = Join-Path $ProjectRoot "reports\review_queue\latest.md"
    Run-Step -Name "research-runtime-sync" -Command "& '$PythonExe' -m stock_agent research-runtime-sync --config $Config"
} elseif ($Mode -eq "dashboard") {
    Run-Step -Name "review-queue-sync" -Command "& '$PythonExe' -m stock_agent review-queue-sync --config $Config"
    $ReviewQueuePath = Join-Path $ProjectRoot "reports\review_queue\latest.md"
    Run-Step -Name "research-runtime-sync" -Command "& '$PythonExe' -m stock_agent research-runtime-sync --config $Config"
}

# Shared presentation plane. agent-run already exports review for new signals;
# do not call export-review again and create duplicate pending records.
if ($Mode -ne "pilot") {
    Run-Step -Name "research-ops-plan" -Command "& '$PythonExe' -m stock_agent research-ops-plan --config $Config"
    $ResearchOperationsPath = Join-Path $ProjectRoot "reports\research_operations\latest.md"

    Run-Step -Name "workspace-sync" -Command "& '$PythonExe' -m stock_agent workspace-sync"
    $WorkspaceLatestPath = Join-Path $ProjectRoot "reports\workspace\latest.md"
    $WorkspaceLatestJsonPath = Join-Path $ProjectRoot "reports\workspace\latest.json"
}

# Independent control plane. The pilot reconcile FIRST verifies that the
# newest DAILY summary binds to its exact immutable run manifest; only a
# passed binding may let production-pilot-sync record anything. It never
# repeats collection or LLM calls.
$ManifestBindSucceeded = $false
$LatestDailySummary = ""
if ($Mode -eq "pilot") {
    # Select the newest summary whose JSON declares mode=daily; newer
    # dashboard/review/pilot summaries must never shadow the daily identity.
    $candidates = @()
    foreach ($file in @(Get-ChildItem -Path (Join-Path $ProjectRoot "logs\scheduled") -Filter "run_*.summary.json" -ErrorAction SilentlyContinue | Sort-Object Name)) {
        try {
            $data = Get-Content -Raw -Encoding UTF8 -LiteralPath $file.FullName | ConvertFrom-Json
            if ([string]$data.mode -eq "daily") { $candidates += $file }
        } catch { }
    }
    if ($candidates.Count -gt 0) { $LatestDailySummary = $candidates[-1].FullName }
    if (-not [string]::IsNullOrWhiteSpace($LatestDailySummary)) {
        Run-Step -Name "run-manifest-bind" -Command "& '$PythonExe' -m stock_agent run-manifest-bind --config $Config --summary '$LatestDailySummary'"
        $ManifestBindSucceeded = ($Commands.Count -gt 0 -and $Commands[-1].exit_code -eq 0)
        if (-not $ManifestBindSucceeded) {
            $Errors += @{ step = "run-manifest-bind"; error = "binding failed; pilot reconciliation side effects are blocked" }
            $ExitCode = 1
        }
    } else {
        $Errors += @{ step = "run-manifest-bind"; error = "no daily-mode scheduled summary found to reconcile" }
        $ExitCode = 1
    }
}
if ($Mode -ne "pilot" -or $ManifestBindSucceeded) {
    Run-Step -Name "production-pilot-sync" -Command "& '$PythonExe' -m stock_agent production-pilot-sync --config $Config --summary '$LatestDailySummary'"
    $ProductionPilotPath = Join-Path $ProjectRoot "reports\production_pilot\latest.md"
} else {
    $ProductionPilotPath = Join-Path $ProjectRoot "reports\production_pilot\latest.md"
}

Run-Step -Name "business-onboarding-sync" -Command "& '$PythonExe' -m stock_agent business-onboarding-sync --config $Config"
$BusinessOnboardingPath = Join-Path $ProjectRoot "reports\business_onboarding\latest.md"

Run-Step -Name "dashboard" -Command "& '$PythonExe' -m stock_agent dashboard --config $Config"
$DashboardPath = Join-Path $ProjectRoot "reports\dashboard\latest.html"

Run-Step -Name "alerts" -Command "& '$PythonExe' -m stock_agent alerts --config $Config"
$NotificationPath = Join-Path $ProjectRoot "reports\notifications\latest.md"

# Final coordination projection is read-only and never performs stale recovery.
Run-Step -Name "run-coordination-status" -Command "& '$PythonExe' -m stock_agent run-coordination-status --config $Config"
if (Test-Path -LiteralPath $RunCoordinationPath) {
    try {
        $CoordinationData = Get-Content -Raw -Encoding UTF8 -LiteralPath $RunCoordinationPath | ConvertFrom-Json
        $RunCoordinationStatus = [string]$CoordinationData.technical_status
    } catch {
        $Errors += @{ step = "read-final-run-coordination-status"; error = $_.Exception.Message }
        $ExitCode = 1
    }
}

# Find report path: prefer the exact json_report pinned by this run's manifest
# over sorting directory names, which cannot distinguish concurrent writers.
$ReportPath = ""
if (-not [string]::IsNullOrWhiteSpace($RunManifestPath) -and (Test-Path -LiteralPath $RunManifestPath)) {
    try {
        $ManifestData = Get-Content -Raw -Encoding UTF8 -LiteralPath $RunManifestPath | ConvertFrom-Json
        $JsonRef = @($ManifestData.artifacts | Where-Object { $_.name -eq "json_report" })[0]
        if ($JsonRef) {
            $Candidate = Join-Path $ProjectRoot ([string]$JsonRef.path)
            if (Test-Path -LiteralPath $Candidate) { $ReportPath = $Candidate }
        }
    } catch {
        $Errors += @{ step = "resolve-report-from-manifest"; error = $_.Exception.Message }
    }
}
if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $jsonDir = Join-Path $ProjectRoot "reports\json"
    if (Test-Path $jsonDir) {
        $latest = Get-ChildItem -Path $jsonDir -Filter "*.json" | Sort-Object Name | Select-Object -Last 1
        if ($latest) { $ReportPath = $latest.FullName }
    }
}

$FinishedAt = (Get-Date).ToString("o")
$DurationSeconds = [math]::Round(((Get-Date) - [datetime]$StartedAt).TotalSeconds, 3)
$ScheduledSloStatus = if ($DurationSeconds -le $ScheduledDeliveryTargetSeconds) { "PASS" } else { "BREACH" }

# Write summary JSON
$Summary = @{
    started_at = $StartedAt
    finished_at = $FinishedAt
    mode = $Mode
    trigger_source = $TriggerSource
    config = $Config
    agent_runtime = $AgentRuntime
    used_pi = $UsedPi
    agent_handoff_path = $AgentHandoffPath
    agent_handoff_public_signal_count = $AgentHandoffPublicSignalCount
    agent_handoff_call_required = $AgentHandoffCallRequired
    pi_skip_reason = $PiSkipReason
    pi_model_call_count = $PiModelCallCount
    pi_result_path = $PiResultPath
    used_openclaw = $false
    openclaw_skip_reason = "retired_runtime"
    exit_code = $ExitCode
    duration_seconds = $DurationSeconds
    scheduled_delivery_target_seconds = $ScheduledDeliveryTargetSeconds
    scheduled_slo_status = $ScheduledSloStatus
    proxy_compatibility_applied = $ProxyCompatibilityApplied
    proxy_compatibility_source = if ($ProxyCompatibilityApplied) { "HTPPS_PROXY" } else { "" }
    commands = $Commands
    report_path = $ReportPath
    market_cache_path = $MarketCachePath
    evidence_vault_path = $EvidenceVaultPath
    research_operations_path = $ResearchOperationsPath
    review_queue_path = $ReviewQueuePath
    workspace_latest_path = $WorkspaceLatestPath
    workspace_latest_json_path = $WorkspaceLatestJsonPath
    dashboard_path = $DashboardPath
    notification_path = $NotificationPath
    production_pilot_path = $ProductionPilotPath
    business_onboarding_path = $BusinessOnboardingPath
    run_coordination_path = $RunCoordinationPath
    run_coordination_status = $RunCoordinationStatus
    run_coordination_preflight_succeeded = $RunCoordinationPreflightSucceeded
    run_id = $RunId
    run_manifest_path = $RunManifestPath
    run_manifest_sha256 = $RunManifestSha256
    run_manifest_run_id = $RunManifestRunId
    text_log_path = $TextLogPath
    errors = $Errors
}

$Summary | ConvertTo-Json -Depth 5 | Out-File -FilePath $SummaryPath -Encoding utf8

Write-Host ""
Write-Host "=== Run Summary ==="
Write-Host "Mode: $Mode"
Write-Host "Trigger source: $TriggerSource"
Write-Host "Exit code: $ExitCode"
Write-Host "Commands: $($Commands.Count)"
Write-Host "Errors: $($Errors.Count)"
Write-Host "Delivery SLO: $ScheduledSloStatus ($DurationSeconds sec / $ScheduledDeliveryTargetSeconds sec)"
Write-Host "Proxy compatibility applied: $ProxyCompatibilityApplied"
Write-Host "Pi public handoff: eligible=$AgentHandoffPublicSignalCount, called=$UsedPi, model_calls=$PiModelCallCount, skip_reason=$PiSkipReason"
Write-Host "Market cache: $MarketCachePath"
Write-Host "Official evidence vault: $EvidenceVaultPath"
Write-Host "Research operations: $ResearchOperationsPath"
Write-Host "Review queue: $ReviewQueuePath"
Write-Host "Production pilot: $ProductionPilotPath"
Write-Host "Business onboarding: $BusinessOnboardingPath"
Write-Host "Workspace: $WorkspaceLatestPath"
Write-Host "Summary: $SummaryPath"
Write-Host "Log: $TextLogPath"

try {
    Stop-Transcript | Out-Null
} catch {
    # The transcript may be unavailable in a restricted scheduler host.
}

exit $ExitCode
