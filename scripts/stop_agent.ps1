Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "data\stock_agent.pid"

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "Stock agent is not running."
    Start-Sleep -Seconds 3
    exit 0
}

$PidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
if ($PidText -notmatch "^\d+$") {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Invalid PID file cleaned."
    Start-Sleep -Seconds 3
    exit 0
}

$AgentPid = [int]$PidText
$Process = Get-Process -Id $AgentPid -ErrorAction SilentlyContinue
if ($null -eq $Process) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Process no longer exists. PID file cleaned."
    Start-Sleep -Seconds 3
    exit 0
}

Stop-Process -Id $AgentPid -Force
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Write-Host "Stock agent stopped. PID: $AgentPid"
Start-Sleep -Seconds 3
