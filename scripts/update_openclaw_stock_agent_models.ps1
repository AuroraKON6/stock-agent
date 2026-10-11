param(
    [string]$Primary = "deepseek/deepseek-v4-pro",
    [string]$Fallback = "deepseek/deepseek-v4-flash",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RegistryPath = Join-Path $HOME ".openclaw\openclaw.json"
$BackupDir = Join-Path $ProjectRoot "openclaw\backups"
$ExpectedIds = @(
    "stock_agent_coordinator",
    "stock_agent_source_collector",
    "stock_agent_market_monitor",
    "stock_agent_news_filing_analyst",
    "stock_agent_signal_analyst",
    "stock_agent_risk_guard",
    "stock_agent_report_writer",
    "stock_agent_review_optimizer",
    "stock_agent_public_handoff"
)

if (-not (Test-Path -LiteralPath $RegistryPath)) {
    throw "OpenClaw registry not found: $RegistryPath"
}

$Config = Get-Content -Raw -Encoding UTF8 -LiteralPath $RegistryPath | ConvertFrom-Json
$AvailableModels = @($Config.models.providers.deepseek.models | ForEach-Object { "deepseek/$($_.id)" })
if ($Primary -notin $AvailableModels -or $Fallback -notin $AvailableModels) {
    throw "Requested DeepSeek primary/fallback is not declared in the OpenClaw provider registry."
}

$Agents = @($Config.agents.list | Where-Object { $_.id -in $ExpectedIds })
$FoundIds = @($Agents | ForEach-Object { $_.id })
$MissingIds = @($ExpectedIds | Where-Object { $_ -notin $FoundIds })
if ($MissingIds.Count -gt 0) {
    throw "Missing stock-agent registry IDs: $($MissingIds -join ', ')"
}

foreach ($Agent in $Agents) {
    $Agent.model.primary = $Primary
    $Agent.model.fallbacks = @($Fallback)
}

if ($DryRun) {
    Write-Output "DRY_RUN: would update $($Agents.Count) stock_agent_ agents"
    Write-Output "primary=$Primary"
    Write-Output "fallback=$Fallback"
    exit 0
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupPath = Join-Path $BackupDir "openclaw_registry_models_$Stamp.json"
Copy-Item -LiteralPath $RegistryPath -Destination $BackupPath -Force

$Json = $Config | ConvertTo-Json -Depth 100
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($RegistryPath, $Json, $Utf8NoBom)

Write-Output "updated=$($Agents.Count)"
Write-Output "primary=$Primary"
Write-Output "fallback=$Fallback"
Write-Output "backup=$BackupPath"
