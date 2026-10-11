# Remove only this project's stock_agent_* registrations from the user's
# OpenClaw registry. Other agents and all project audit history are preserved.

param(
    [switch]$Apply,
    [string]$RegistryPath = (Join-Path $HOME ".openclaw\openclaw.json")
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path -LiteralPath $RegistryPath)) {
    Write-Host "OpenClaw registry not found; nothing to retire: $RegistryPath"
    exit 0
}
$Registry = Get-Content -Raw -Encoding UTF8 -LiteralPath $RegistryPath | ConvertFrom-Json
$AllAgents = @($Registry.agents.list)
$Targets = @($AllAgents | Where-Object { [string]$_.id -like "stock_agent_*" })
$Preserved = @($AllAgents | Where-Object { [string]$_.id -notlike "stock_agent_*" })

Write-Host "=== OpenClaw Stock Agent Retirement ==="
Write-Host "Registry: $RegistryPath"
Write-Host "Target count: $($Targets.Count)"
foreach ($Agent in $Targets) { Write-Host "  - $($Agent.id)" }
Write-Host "Preserved non-project agents: $($Preserved.Count)"

if (-not $Apply) {
    Write-Host "[DRY RUN] No registry entry was changed. Add -Apply after Pi acceptance."
    exit 0
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $ProjectRoot "openclaw\backups"
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$BackupPath = Join-Path $BackupDir "registry_before_pi_retirement_$Stamp.json"
Copy-Item -LiteralPath $RegistryPath -Destination $BackupPath
$Registry.agents.list = $Preserved
$TempPath = "$RegistryPath.stock_agent_pi_migration.tmp"
$Registry | ConvertTo-Json -Depth 100 | Out-File -LiteralPath $TempPath -Encoding utf8
$Verified = Get-Content -Raw -Encoding UTF8 -LiteralPath $TempPath | ConvertFrom-Json
if (@($Verified.agents.list | Where-Object { [string]$_.id -like "stock_agent_*" }).Count -ne 0) {
    throw "Retirement verification failed; registry was not replaced"
}
if (@($Verified.agents.list).Count -ne $Preserved.Count) {
    throw "Retirement verification failed; preserved agent count changed"
}
Move-Item -LiteralPath $TempPath -Destination $RegistryPath -Force
Write-Host "Retired $($Targets.Count) stock_agent_* registrations. Backup: $BackupPath"

