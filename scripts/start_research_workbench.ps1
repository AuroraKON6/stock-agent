# Start the local Research Runtime Workbench HTTP server (loopback only).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1 -NoBrowser
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1 -Config config\watchlist.local.json -Host 127.0.0.1 -Port 8767
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1 -StateDirectory C:\temp\wb
#
# The parameter is spelled HostName in code because PowerShell reserves $Host as a
# read-only automatic variable; the -Host alias is accepted so callers can use
# either spelling.
#
# Safety properties:
#   * Fails closed unless the bind host is a loopback address (127.0.0.1,
#     localhost, ::1) -- matching the server's own loopback-only enforcement.
#   * Idempotent: a live instance for the same config/host/port is reported and
#     reused; a stale or foreign PID file is cleaned before launching.
#   * PID and metadata live under logs/workbench/ by default (override with
#     -StateDirectory); stdout/stderr are written to timestamped files in the
#     same directory. The state directory is used only for PID/metadata/log
#     storage so tests can isolate state in a temporary directory.
#   * On startup failure only the process just spawned is terminated; its PID
#     metadata is removed and both log paths are reported.
#   * No broad name-based process matching and no trading/order/approval edits.

param(
    [string]$Config = "config/watchlist.local.json",
    [Alias("Host")]
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8767,
    [switch]$NoBrowser,
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
$HealthTimeoutSeconds = 60

New-Item -ItemType Directory -Force -Path $WorkbenchDir | Out-Null

function Resolve-ProjectConfigPath {
    param([string]$ConfigValue)
    $candidate = $ConfigValue
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $Root $candidate
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Test-LoopbackHost {
    param([string]$Value)
    $h = $Value.Trim().ToLowerInvariant()
    return ($h -eq "127.0.0.1" -or $h -eq "localhost" -or $h -eq "::1")
}

# Map a validated loopback host to the host portion of a URL. 127.0.0.1 and
# localhost share the IPv4 loopback URL; only ::1 needs the bracketed form.
function Get-BaseUrlHost {
    param([string]$HostNameValue)
    if ($HostNameValue.Trim().ToLowerInvariant() -eq "::1") {
        return "[::1]"
    }
    return "127.0.0.1"
}

# Strict identity check against a live process. Returns $true only when the
# process at $ProcessId is still alive and its command line contains all of:
#   * "-m stock_agent review-workbench"  (the CLI entry point)
#   * the absolute config path for this project
#   * --host <HostNameValue> and --port <PortValue>
# A reused PID whose command line no longer matches is treated as "not ours".
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

# Locked venv interpreters created by uv are trampolines: python.exe launches
# the real cached interpreter as a child process, so the PID returned by
# Start-Process is a parent, not the process that serves /health. Return the
# descendant PIDs (bounded depth) whose command line is our workbench entry, so
# health matching, PID records, and cleanup all target processes we own.
function Get-WorkbenchDescendantPids {
    param(
        [int]$RootProcessId,
        [string]$ConfigAbs,
        [string]$HostNameValue,
        [int]$PortValue,
        [int]$MaxDepth = 4
    )
    $found = @()
    $frontier = @(,$RootProcessId)
    for ($depth = 0; $depth -lt $MaxDepth -and $frontier.Count -gt 0; $depth++) {
        $next = @()
        foreach ($parentId in $frontier) {
            $children = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId = " + $parentId) -ErrorAction SilentlyContinue)
            foreach ($child in $children) {
                $childPid = [int]$child.ProcessId
                $next += $childPid
                if (Test-LiveWorkbenchProcess -ProcessId $childPid -ConfigAbs $ConfigAbs -HostNameValue $HostNameValue -PortValue $PortValue) {
                    $found += $childPid
                }
            }
        }
        $frontier = $next
    }
    return $found
}

# -------- Validate inputs (fail closed) --------
$ConfigAbs = Resolve-ProjectConfigPath -ConfigValue $Config
if (-not (Test-LoopbackHost -Value $HostName)) {
    Write-Host "Refusing to start: Host '$HostName' is not a loopback address (127.0.0.1, localhost, ::1)."
    exit 1
}
if (-not (Test-Path -LiteralPath $ConfigAbs)) {
    Write-Host "Config not found: $ConfigAbs"
    exit 1
}

$HealthUrl = "http://{0}:{1}/health" -f (Get-BaseUrlHost $HostName), $Port
$DisplayUrl = "http://{0}:{1}/?view=runtime" -f (Get-BaseUrlHost $HostName), $Port

# -------- Idempotency: trust existing metadata only after CIM validation --------
$Existing = $null
if (Test-Path -LiteralPath $MetaFile) {
    try {
        $Existing = Get-Content -LiteralPath $MetaFile -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        $Existing = $null
    }
}

if ($null -ne $Existing) {
    $recPid = 0
    try { $recPid = [int]$Existing.pid } catch { $recPid = 0 }
    $recPort = 0
    try { $recPort = [int]$Existing.port } catch { $recPort = 0 }
    $recHost = ""
    try { $recHost = [string]$Existing.host } catch { $recHost = "" }
    $recConfigAbs = ""
    $recInstanceId = ""
    try { $recConfigAbs = Resolve-ProjectConfigPath -ConfigValue ([string]$Existing.config) } catch { $recConfigAbs = "" }
    try { $recInstanceId = [string]$Existing.instance_id } catch { $recInstanceId = "" }

    $isLiveOurs = Test-LiveWorkbenchProcess -ProcessId $recPid -ConfigAbs $recConfigAbs -HostNameValue $recHost -PortValue $recPort -InstanceId $recInstanceId
    if ($isLiveOurs) {
        if ($recPort -eq $Port -and $recHost -eq $HostName -and $recConfigAbs -eq $ConfigAbs) {
            Write-Host "Research workbench is already running. PID: $recPid"
            Write-Host "URL: $DisplayUrl"
            Write-Host "Config: $ConfigAbs"
            exit 0
        }
        # A different workbench instance is live (other config/port). Never kill
        # it and never clobber its metadata.
        Write-Host "A research workbench instance is already running for a different config/port (PID $recPid, port $recPort)."
        Write-Host "Stop it first or start with the same settings; not overwriting its metadata."
        exit 1
    }
    # Stale or reused PID: metadata no longer describes a live workbench we own.
    Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $PidFile) {
    $pidText = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction Stop).Trim()
    if ($pidText -match "^\d+$") {
        $candidatePid = [int]$pidText
        if (Test-LiveWorkbenchProcess -ProcessId $candidatePid -ConfigAbs $ConfigAbs -HostNameValue $HostName -PortValue $Port) {
            Write-Host "Research workbench is already running. PID: $candidatePid"
            Write-Host "URL: $DisplayUrl"
            Write-Host "Config: $ConfigAbs"
            exit 0
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

# -------- Launch hidden with the locked interpreter --------
# Entry points must use the locked interpreter; PATH python is not a supported
# fallback because it resolves to an uncontrolled environment (e.g. Anaconda).
$Python = Join-Path $Root ".venv312\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Locked interpreter not found: $Python (expected .venv312; PATH python is not a supported fallback)"
}
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Stdout = Join-Path $WorkbenchDir "workbench-$Stamp.out.log"
$Stderr = Join-Path $WorkbenchDir "workbench-$Stamp.err.log"

$ArgString = '-m stock_agent review-workbench --config "{0}" --host {1} --port {2}' -f $ConfigAbs, $HostName, $Port

# Windows PowerShell Start-Process can fail when a sandbox supplies both Path
# and PATH variants. Normalize the process-scoped key before launching.
$InheritedPath = $env:PATH
Remove-Item Env:PATH -ErrorAction SilentlyContinue
$env:Path = $InheritedPath
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList $ArgString `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -PassThru

Write-Host "Started research workbench launcher PID $($Process.Id). Waiting for health at $HealthUrl ..."

# -------- Bounded health poll --------
# Accept /health when its pid is the launched process OR a validated workbench
# descendant (uv venv trampolines serve from a child interpreter process).
$Deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$Healthy = $false
$ProcessExited = $false
$HealthData = $null
$WorkbenchPid = $Process.Id
while ((Get-Date) -lt $Deadline) {
    if ($Process.HasExited -and -not (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
        $ProcessExited = $true
        break
    }
    try {
        $Response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2 -ErrorAction Stop
        if ($Response.status -eq "ok" -and -not [string]::IsNullOrWhiteSpace([string]$Response.instance_id)) {
            $responsePid = [int]$Response.pid
            $descendants = @(Get-WorkbenchDescendantPids -RootProcessId $Process.Id -ConfigAbs $ConfigAbs -HostNameValue $HostName -PortValue $Port)
            if ($responsePid -eq $Process.Id -or $descendants -contains $responsePid) {
                $HealthData = $Response
                $WorkbenchPid = $responsePid
                $Healthy = $true
                break
            }
        }
    } catch {
        # Not up yet; keep polling.
    }
    Start-Sleep -Seconds 1
}

if (-not $Healthy) {
    if ($ProcessExited) {
        Write-Host "Workbench process exited before becoming healthy. See stderr log."
    } else {
        Write-Host "Workbench did not become healthy within $HealthTimeoutSeconds seconds."
    }
    # Terminate only processes we just spawned: validated workbench descendants
    # first (the real interpreter under a venv trampoline), then the launcher.
    $descendants = @(Get-WorkbenchDescendantPids -RootProcessId $Process.Id -ConfigAbs $ConfigAbs -HostNameValue $HostName -PortValue $Port)
    foreach ($descPid in $descendants) {
        Stop-Process -Id $descPid -Force -ErrorAction SilentlyContinue
        Write-Host "Terminated failed workbench child process PID $descPid."
    }
    if (-not $Process.HasExited) {
        $stillOurs = Test-LiveWorkbenchProcess -ProcessId $Process.Id -ConfigAbs $ConfigAbs -HostNameValue $HostName -PortValue $Port
        if ($stillOurs) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            Write-Host "Terminated failed workbench launcher PID $($Process.Id)."
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $MetaFile -Force -ErrorAction SilentlyContinue
    Write-Host "Stdout log: $Stdout"
    Write-Host "Stderr log: $Stderr"
    exit 1
}

# -------- Healthy: persist PID and metadata --------
# Record the process that actually serves requests (a descendant interpreter
# under venv trampolines) so stop/idempotency checks and the shutdown endpoint
# all target the same process the health endpoint authenticated.
$Meta = @{
    pid = $WorkbenchPid
    launcher_pid = $Process.Id
    project_root = $Root
    config = $ConfigAbs
    host = $HostName
    port = $Port
    url = $DisplayUrl
    health_url = $HealthUrl
    instance_id = [string]$HealthData.instance_id
    started_at = (Get-Date).ToString("o")
    stdout = $Stdout
    stderr = $Stderr
}
$MetaJson = $Meta | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($MetaFile, $MetaJson, (New-Object System.Text.UTF8Encoding($false)))
Set-Content -LiteralPath $PidFile -Value $WorkbenchPid -Encoding ASCII

Write-Host "Research workbench started."
Write-Host "PID: $WorkbenchPid"
if ($WorkbenchPid -ne $Process.Id) {
    Write-Host "Launcher PID: $($Process.Id)"
}
Write-Host "URL: $DisplayUrl"
Write-Host "Config: $ConfigAbs"
Write-Host "Stdout log: $Stdout"
Write-Host "Stderr log: $Stderr"

if (-not $NoBrowser) {
    Start-Process $DisplayUrl
}

exit 0
