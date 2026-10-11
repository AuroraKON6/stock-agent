# Stop the local Research Runtime Workbench started by start_research_workbench.ps1.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_research_workbench.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_research_workbench.ps1 -StateDirectory C:\temp\wb
#
# Safety properties:
#   * Idempotent: if no workbench metadata exists, or the recorded process is
#     gone, the script cleans stale metadata and exits 0.
#   * Stops only the exact PID recorded in logs/workbench/workbench.json after
#     strict CIM command-line validation (same module/config/host/port identity
#     used by the start script). Never uses broad name-based kills.
#   * If the recorded PID has been reused by an unrelated process, stale metadata
#     is removed but the unrelated process is never killed.
#   * State lives under logs/workbench/ by default (override with -StateDirectory,
#     used only for PID/metadata/log storage so tests can isolate state).

param(
    [string]$StateDirectory = "logs/workbench"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($StateDirectory)) {
    $WorkbenchDir = [System.IO.Path]::GetFullPath($StateDirectory)
} else {
    $WorkbenchDir = [System.IO.Path]::GetFullPath((Join-Path $Root $StateDirectory))
}
$PidFile = Join-Path $WorkbenchDir "workbench.pid"
$MetaFile = Join-Path $WorkbenchDir "workbench.json"

function Resolve-ProjectConfigPath {
    param([string]$ConfigValue)
    $candidate = $ConfigValue
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $Root $candidate
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

# Same strict identity check as the start script.
function Test-LiveWorkbenchProcess {
    param(
        [int]$ProcessId,
        [string]$ConfigAbs,
        [string]$HostNameValue,
        [int]$PortValue,
        [string]$InstanceId = ""
    )
    if ($ProcessId -le 0) {
        return $false
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -ne $proc) {
        $cmd = [string]$proc.CommandLine
        if (-not [string]::IsNullOrWhiteSpace($cmd)) {
            $cmdNorm = $cmd.ToLowerInvariant()
            $configSlash = $ConfigAbs.ToLowerInvariant().Replace('\', '/')
            $configBackslash = $ConfigAbs.ToLowerInvariant()
            $hostLower = $HostNameValue.Trim().ToLowerInvariant()
            $portText = $PortValue.ToString()
            $hasModule = $cmdNorm.Contains("-m stock_agent review-workbench")
            $hasConfig = ($cmdNorm.Contains($configSlash) -or $cmdNorm.Contains($configBackslash))
            $hasHost = ($cmdNorm.Contains("--host " + $hostLower) -or $cmdNorm.Contains("--host=" + $hostLower))
            $hasPort = ($cmdNorm.Contains("--port " + $portText) -or $cmdNorm.Contains("--port=" + $portText))
            if ($hasModule -and $hasConfig -and $hasHost -and $hasPort) { return $true }
        }
    }
    if ([string]::IsNullOrWhiteSpace($InstanceId)) { return $false }
    $healthHost = if ($HostNameValue.Trim().ToLowerInvariant() -eq "::1") { "[::1]" } else { "127.0.0.1" }
    try {
        $health = Invoke-RestMethod -Uri ("http://{0}:{1}/health" -f $healthHost, $PortValue) -TimeoutSec 2 -ErrorAction Stop
        return ([string]$health.instance_id -eq $InstanceId -and [int]$health.pid -eq $ProcessId)
    } catch {
        return $false
    }
}

$Existing = $null
if (Test-Path -LiteralPath $MetaFile) {
    try {
        $Existing = Get-Content -LiteralPath $MetaFile -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        $Existing = $null
    }
}

if ($null -eq $Existing) {
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "No workbench metadata found; removed stale PID file."
    } else {
        Write-Host "Research workbench is not running."
    }
    exit 0
}

$recPid = 0
$recPort = 0
$recHost = ""
$recConfigAbs = ""
$recInstanceId = ""
try {
    $recPid = [int]$Existing.pid
    $recPort = [int]$Existing.port
    $recHost = [string]$Existing.host
    $recConfigAbs = Resolve-ProjectConfigPath -ConfigValue ([string]$Existing.config)
    try { $recInstanceId = [string]$Existing.instance_id } catch { $recInstanceId = "" }
} catch {
    Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Workbench metadata was invalid; cleaned stale metadata."
    exit 0
}

if ($recPid -le 0) {
    Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Workbench metadata had no valid PID; cleaned stale metadata."
    exit 0
}

$isLiveOurs = Test-LiveWorkbenchProcess -ProcessId $recPid -ConfigAbs $recConfigAbs -HostNameValue $recHost -PortValue $recPort -InstanceId $recInstanceId

if (-not $isLiveOurs) {
    # Either the process is gone or the PID was reused by another process.
    # Remove our metadata but never kill anything.
    Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $procNow = Get-CimInstance Win32_Process -Filter "ProcessId = $recPid" -ErrorAction SilentlyContinue
    if ($null -ne $procNow) {
        Write-Host "Recorded PID $recPid is now a different process; refusing to kill it. Removed stale workbench metadata."
    } else {
        Write-Host "Recorded workbench process (PID $recPid) is no longer running; cleaned stale metadata."
    }
    exit 0
}

# Prefer the authenticated loopback shutdown endpoint. A force-stop remains a
# bounded fallback only after the exact instance was validated above.
$shutdownHost = if ($recHost.Trim().ToLowerInvariant() -eq "::1") { "[::1]" } else { "127.0.0.1" }
$shutdownOk = $false
if (-not [string]::IsNullOrWhiteSpace($recInstanceId)) {
    try {
        $response = Invoke-RestMethod -Method Post -Uri ("http://{0}:{1}/shutdown" -f $shutdownHost, $recPort) `
            -Headers @{ "X-Workbench-Instance" = $recInstanceId } -TimeoutSec 3 -ErrorAction Stop
        $shutdownOk = ([string]$response.status -eq "stopping")
    } catch {
        $shutdownOk = $false
    }
}
if ($shutdownOk) {
    for ($i = 0; $i -lt 20; $i++) {
        if ($null -eq (Get-Process -Id $recPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 100
    }
}
if ($null -ne (Get-Process -Id $recPid -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $recPid -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Write-Host "Research workbench stopped. PID: $recPid"
Write-Host "URL was: $($Existing.url)"
exit 0
