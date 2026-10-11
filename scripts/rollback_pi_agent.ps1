# Fail-closed Pi rollback. This switches LLM stages off and keeps the
# deterministic research pipeline available. It never re-enables OpenClaw or
# an HTTP provider automatically.

param(
    [string]$Config = "config\watchlist.local.json",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigPath = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $ProjectRoot $Config }
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Config not found: $ConfigPath" }

$ConfigData = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
Write-Host "=== Pi Agent Fail-Closed Rollback ==="
Write-Host "Config: $ConfigPath"
Write-Host "Resulting mode: deterministic rule pipeline only"
Write-Host "Pi, multi-role LLM, public handoff, OpenClaw, and automatic external fallback: disabled"

if (-not $Apply) {
    Write-Host "[DRY RUN] No configuration was changed. Add -Apply to continue."
    exit 0
}

$FullRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
$FullConfig = [IO.Path]::GetFullPath($ConfigPath)
if (-not $FullConfig.StartsWith($FullRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Config must be inside the project root"
}
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $ProjectRoot "logs\pi_rollback\$Stamp"
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$BackupPath = Join-Path $BackupDir "watchlist.before_rule_only.json"
Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath

$ConfigData.pi_agent.enabled = $false
$ConfigData.pi_agent.allow_legacy_http_fallback = $false
$ConfigData.llm_agent.enabled = $false
$ConfigData.multi_role_agent.enabled = $false
$ConfigData.agent_handoff.enabled = $false
$ConfigData.openclaw_handoff.enabled = $false
$TempPath = "$ConfigPath.tmp"
$ConfigData | ConvertTo-Json -Depth 100 | Out-File -LiteralPath $TempPath -Encoding utf8
$null = Get-Content -Raw -Encoding UTF8 -LiteralPath $TempPath | ConvertFrom-Json
Move-Item -LiteralPath $TempPath -Destination $ConfigPath -Force

Write-Host "Rollback applied. Original config: $BackupPath"
Write-Host "Restore explicitly with: Copy-Item -LiteralPath `"$BackupPath`" -Destination `"$ConfigPath`" -Force"

