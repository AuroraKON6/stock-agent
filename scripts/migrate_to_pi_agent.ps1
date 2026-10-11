# Controlled, repeatable cutover to the project-local Pi Agent runtime.
# Dry-run is the default. Use -Apply only after reviewing the printed checks.

param(
    [string]$Config = "config\watchlist.local.json",
    [switch]$Apply,
    [switch]$SkipNpmInstall,
    [switch]$SkipScheduledTasks
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigPath = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $ProjectRoot $Config }
$PiRoot = Join-Path $ProjectRoot "pi_runtime"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $ProjectRoot "logs\pi_migration\$Stamp"
$ExpectedPackage = "@earendil-works/pi-coding-agent"
$ExpectedVersion = "0.84.2"
$OfficialRegistry = "https://registry.npmjs.org/"
$ScheduledTasksUpdated = $false
$ScheduledTasksError = ""

function Assert-PathInsideProject {
    param([string]$Path)
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    $target = [IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside project root: $target"
    }
}

function Read-Json {
    param([string]$Path)
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json
}

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Config not found: $ConfigPath" }
if (-not (Test-Path -LiteralPath (Join-Path $PiRoot "package-lock.json"))) { throw "Pi package lock is missing" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js is not installed" }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw "npm.cmd is not installed" }

$ConfigData = Read-Json $ConfigPath
$PackageData = Read-Json (Join-Path $PiRoot "package.json")
$LockText = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PiRoot "package-lock.json")
$ConfiguredVersion = [string]$PackageData.dependencies.$ExpectedPackage
$RuntimeChecks = @(
    @{ Name = "pi_agent.enabled"; Pass = [bool]$ConfigData.pi_agent.enabled },
    @{ Name = "llm_agent.runtime=pi"; Pass = ([string]$ConfigData.llm_agent.runtime -eq "pi") },
    @{ Name = "multi_role_agent.runtime=pi"; Pass = ([string]$ConfigData.multi_role_agent.runtime -eq "pi") },
    @{ Name = "agent_handoff.enabled"; Pass = [bool]$ConfigData.agent_handoff.enabled },
    @{ Name = "OpenClaw handoff disabled"; Pass = (-not [bool]$ConfigData.openclaw_handoff.enabled) },
    @{ Name = "Pi package exact pin"; Pass = ($ConfiguredVersion -eq $ExpectedVersion) },
    @{ Name = "Official npm registry in lock"; Pass = ($LockText -match [regex]::Escape($OfficialRegistry)) },
    @{ Name = "No mirror registry in lock"; Pass = ($LockText -notmatch "npmmirror|taobao") }
)

Write-Host "=== Pi Agent Cutover ==="
Write-Host "Mode: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"
Write-Host "Project: $ProjectRoot"
Write-Host "Config: $ConfigPath"
foreach ($check in $RuntimeChecks) {
    Write-Host "[$(if ($check.Pass) { 'PASS' } else { 'FAIL' })] $($check.Name)"
}
if (($RuntimeChecks | Where-Object { -not $_.Pass }).Count -gt 0) {
    throw "Pi cutover preflight failed"
}

if (-not $Apply) {
    Write-Host "No files, registry entries, dependencies, or scheduled tasks were changed."
    Write-Host "Apply with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\migrate_to_pi_agent.ps1 -Apply"
    exit 0
}

Assert-PathInsideProject $BackupDir
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $BackupDir "watchlist.pre_pi.json")
Copy-Item -LiteralPath (Join-Path $PiRoot "package.json") -Destination $BackupDir
Copy-Item -LiteralPath (Join-Path $PiRoot "package-lock.json") -Destination $BackupDir
Copy-Item -LiteralPath (Join-Path $PiRoot "agent_home\settings.json") -Destination (Join-Path $BackupDir "pi_settings.json")

$RegistryPath = Join-Path $HOME ".openclaw\openclaw.json"
if (Test-Path -LiteralPath $RegistryPath) {
    Copy-Item -LiteralPath $RegistryPath -Destination (Join-Path $BackupDir "openclaw_registry.read_only_backup.json")
}

$TaskNames = @(
    "StockAgent_DailyResearch",
    "StockAgent_PilotReconcile",
    "StockAgent_DailyDashboard",
    "StockAgent_WeeklyReview"
)
$TaskBackupDir = Join-Path $BackupDir "scheduled_tasks"
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Task) {
        New-Item -ItemType Directory -Path $TaskBackupDir -Force | Out-Null
        Export-ScheduledTask -TaskName $TaskName | Out-File -LiteralPath (Join-Path $TaskBackupDir "$TaskName.xml") -Encoding utf8
    }
}

if (-not $SkipNpmInstall) {
    Push-Location $PiRoot
    try {
        & npm.cmd ci --ignore-scripts --audit=false --fund=false --registry=$OfficialRegistry --cache .npm-cache
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

Push-Location $ProjectRoot
try {
    $PythonExe = Join-Path $ProjectRoot ".venv312\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Locked interpreter not found: $PythonExe (expected .venv312; PATH python is not a supported fallback)" }
    & $PythonExe -m stock_agent pi-runtime-status --config $Config --require-ready
    if ($LASTEXITCODE -ne 0) { throw "Pi runtime validation failed" }
    & $PythonExe -m stock_agent agent-handoff --config $Config
    if ($LASTEXITCODE -ne 0) { throw "Governed handoff validation failed" }
    if (-not $SkipScheduledTasks) {
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_scheduled_tasks.ps1
            if ($LASTEXITCODE -ne 0) { throw "installer exit code $LASTEXITCODE" }
            $ScheduledTasksUpdated = $true
        } catch {
            $ScheduledTasksError = $_.Exception.Message
            Write-Warning "Scheduled task cutover is blocked: $ScheduledTasksError"
        }
    }
} finally {
    Pop-Location
}

$Manifest = [ordered]@{
    schema_version = "25.0"
    applied_at = (Get-Date).ToString("o")
    project_root = $ProjectRoot
    config_path = $ConfigPath
    backup_dir = $BackupDir
    runtime = "pi"
    package = $ExpectedPackage
    package_version = $ExpectedVersion
    package_lock_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PiRoot "package-lock.json")).Hash.ToLowerInvariant()
    official_registry = $OfficialRegistry
    lifecycle_scripts_executed = $false
    status = if ($ScheduledTasksError) { "PARTIAL" } else { "COMPLETE" }
    scheduled_tasks_updated = $ScheduledTasksUpdated
    scheduled_tasks_error = $ScheduledTasksError
    openclaw_registry_modified = $false
    safety_boundary = "Research only; no automatic assignment, approval, publication, trading, orders, or position changes."
    rollback = "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rollback_pi_agent.ps1 -Config $Config -Apply"
}
$Manifest | ConvertTo-Json -Depth 6 | Out-File -LiteralPath (Join-Path $BackupDir "migration_manifest.json") -Encoding utf8
Write-Host "Pi cutover complete. Backup and manifest: $BackupDir"
if ($ScheduledTasksError) {
    throw "Pi runtime cutover completed, but scheduled task registration requires an unrestricted user shell: $ScheduledTasksError"
}
