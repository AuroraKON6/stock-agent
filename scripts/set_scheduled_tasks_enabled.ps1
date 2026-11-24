# Explicitly disable or restore the legacy Stock Agent Windows Scheduled Tasks.
# Retain the Record path printed by -Disable and pass it to -Enable after rollback.
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Disable")]
    [switch]$Disable,
    [Parameter(Mandatory = $true, ParameterSetName = "Enable")]
    [switch]$Enable,
    [Parameter(ParameterSetName = "Disable")]
    [Parameter(Mandatory = $true, ParameterSetName = "Enable")]
    [string]$RecordPath,
    [switch]$DryRun,
    [string]$Reason = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskNames = @(
    "StockAgent_DailyResearch",
    "StockAgent_DailyDashboard",
    "StockAgent_PilotReconcile",
    "StockAgent_WeeklyReview"
)

function Get-LegacyTask {
    param([string]$Name)
    try {
        Get-ScheduledTask -TaskPath '\' -TaskName $Name -ErrorAction Stop
    } catch {
        if ($_.CategoryInfo.Category -ne 'ObjectNotFound') { throw }
    }
}

function Get-LegacyState {
    param($Task)
    if ($null -eq $Task) { return "missing" }
    if ($Task.State -eq "Disabled") { return "disabled" }
    return "enabled"
}

function Save-DisableRecord {
    param($Record)
    $TempPath = "$RecordPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Json = $Record | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($TempPath, $Json, [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($RecordPath)) {
            # File.Replace requires a backup path on some .NET implementations.
            [IO.File]::Replace($TempPath, $RecordPath, "$TempPath.backup")
        } else {
            [IO.File]::Move($TempPath, $RecordPath)
        }
    } finally {
        foreach ($Path in @($TempPath, "$TempPath.backup")) {
            if ([IO.File]::Exists($Path)) { [IO.File]::Delete($Path) }
        }
    }
}

if (-not $RecordPath) {
    $RecordDir = Join-Path $ProjectRoot "logs/scheduled_task_state"
    $RecordPath = Join-Path $RecordDir ("disable_{0}.json" -f [guid]::NewGuid().ToString("N"))
}
$RecordPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RecordPath)

# Hold a file lock through each read/modify/write sequence. The record path
# identifies one cutover and can be reused to resume a partially applied action.
if (-not $DryRun) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $RecordPath)) | Out-Null
    $Lock = [IO.File]::Open("$RecordPath.lock", 'OpenOrCreate', 'ReadWrite', 'None')
}
try {
    if ([IO.File]::Exists($RecordPath)) {
        $Record = Get-Content -Raw -LiteralPath $RecordPath -Encoding UTF8 | ConvertFrom-Json
        if ($Record.schema_version -ne 1 -or $Record.action -ne "disable" -or
            (@($Record.tasks.name) -join ',') -cne ($TaskNames -join ',')) {
            throw "Invalid disable record: $RecordPath"
        }
    } elseif ($Enable) {
        throw "Disable record missing: $RecordPath"
    } else {
        $Record = [pscustomobject]@{
            schema_version = 1
            action = "disable"
            at = (Get-Date).ToString("o")
            reason = $Reason
            restore_started_at = $null
            restore_reason = $null
            tasks = @($TaskNames | ForEach-Object {
                [pscustomobject]@{
                    name = $_
                    before = Get-LegacyState (Get-LegacyTask $_)
                    status = "unchanged"
                }
            })
        }
        if (-not $DryRun) { Save-DisableRecord $Record }
    }

    Write-Host "Record: $RecordPath"
    if ($Disable -and $Record.restore_started_at) { throw "Restore already started for this record." }
    if ($Enable -and -not $DryRun -and -not $Record.restore_started_at) {
        $Record.restore_started_at = (Get-Date).ToString("o")
        $Record.restore_reason = $Reason
        Save-DisableRecord $Record
    }

    foreach ($Entry in $Record.tasks) {
        $Task = Get-LegacyTask $Entry.name
        $State = Get-LegacyState $Task
        $Action = "leave"
        if ($Disable -and $Entry.before -eq "enabled" -and
            $State -eq "enabled" -and $Entry.status -eq "unchanged") {
            $Action = "disable"
            if (-not $DryRun) {
                $Entry.status = "disable_pending"
                Save-DisableRecord $Record
            }
        } elseif ($Disable -and $Entry.status -eq "disable_pending" -and $State -eq "enabled") {
            $Action = "disable"
        } elseif ($Enable -and $Entry.before -eq "enabled" -and
            $Entry.status -eq "disabled" -and $State -eq "disabled") {
            $Action = "enable"
        }
        Write-Host ("{0}: before={1} now={2} -> {3}" -f $Entry.name, $Entry.before, $State, $Action)
        if ($DryRun) { continue }
        if ($Action -eq "disable") {
            $Task | Disable-ScheduledTask -ErrorAction Stop | Out-Null
            $State = Get-LegacyState (Get-LegacyTask $Entry.name)
            if ($State -ne "disabled") { throw "Unable to confirm disable: $($Entry.name)" }
        } elseif ($Action -eq "enable") {
            $Task | Enable-ScheduledTask -ErrorAction Stop | Out-Null
            $State = Get-LegacyState (Get-LegacyTask $Entry.name)
            if ($State -ne "enabled") { throw "Unable to confirm restore: $($Entry.name)" }
        }
        if ($Disable -and $Entry.status -eq "disable_pending" -and $State -eq "disabled") {
            $Entry.status = "disabled"
            Save-DisableRecord $Record
        } elseif ($Enable -and $Entry.status -eq "disabled" -and $State -eq "enabled") {
            $Entry.status = "restored"
            Save-DisableRecord $Record
        }
    }
} finally {
    if ($Lock) { $Lock.Dispose() }
}
