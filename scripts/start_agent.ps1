Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root "data"
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $DataDir "stock_agent.pid"
$ConfigLocal = Join-Path $Root "config\watchlist.local.json"
$ConfigExample = Join-Path $Root "config\watchlist.example.json"

New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($ExistingPid -match "^\d+$") {
        $ExistingProcess = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($null -ne $ExistingProcess) {
            Write-Host "Stock agent is already running. PID: $ExistingPid"
            Start-Sleep -Seconds 3
            exit 0
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$ConfigRel = "config/watchlist.example.json"
if (Test-Path -LiteralPath $ConfigLocal) {
    $ConfigRel = "config/watchlist.local.json"
} elseif (-not (Test-Path -LiteralPath $ConfigExample)) {
    throw "Config file not found: $ConfigExample"
}

# Entry points must use the locked interpreter; PATH python is not a supported
# fallback because it resolves to an uncontrolled environment (e.g. Anaconda).
$Python = Join-Path $Root ".venv312\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Locked interpreter not found: $Python (expected .venv312; PATH python is not a supported fallback)"
}
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Stdout = Join-Path $LogDir "stock-agent-$Stamp.out.log"
$Stderr = Join-Path $LogDir "stock-agent-$Stamp.err.log"

$Args = @(
    "-m", "stock_agent",
    "watch",
    "--config", $ConfigRel,
    "--interval-minutes", "30"
)

$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList $Args `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -PassThru

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII
Write-Host "Stock agent started. PID: $($Process.Id)"
Write-Host "Log: $Stdout"
Start-Sleep -Seconds 3
