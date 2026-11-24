param([string]$Script = "/app/scripts/set_scheduled_tasks_enabled.ps1")

$ErrorActionPreference = "Stop"
$Tasks = @{
    StockAgent_DailyResearch = "Ready"
    StockAgent_DailyDashboard = "Disabled"
    StockAgent_PilotReconcile = "Ready"
}
$Changes = [Collections.Generic.List[string]]::new()
$FailDisable = "StockAgent_PilotReconcile"

function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    if ($TaskPath -ne '\') { throw "Unexpected task path: $TaskPath" }
    if (-not $Tasks.ContainsKey($TaskName)) {
        Write-Error "No such task: $TaskName" -Category ObjectNotFound
        return
    }
    [pscustomobject]@{ TaskName = $TaskName; State = $Tasks[$TaskName] }
}

function Disable-ScheduledTask {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$Task)
    process {
        if ($Task.TaskName -eq $FailDisable) { throw "Simulated disable failure" }
        $Tasks[$Task.TaskName] = "Disabled"
        $Changes.Add("disable:$($Task.TaskName)")
    }
}

function Enable-ScheduledTask {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$Task)
    process {
        $Tasks[$Task.TaskName] = "Ready"
        $Changes.Add("enable:$($Task.TaskName)")
    }
}

$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("r25-mock-" + [guid]::NewGuid().ToString("N"))
$Record = Join-Path $TestRoot "disable.json"
try {
    & $Script -Disable -RecordPath $Record -DryRun
    if ($Changes.Count -ne 0 -or (Test-Path $Record)) { throw "Dry-run wrote state" }
    try {
        & $Script -Disable -RecordPath $Record
        throw "Expected simulated disable failure"
    } catch {
        if ($_.Exception.Message -ne "Simulated disable failure") { throw }
        Write-Host "Observed injected partial disable failure"
    }
    # A task absent at cutover may appear before rollback. It must remain disabled.
    $Tasks.StockAgent_WeeklyReview = "Disabled"
    & $Script -Enable -RecordPath $Record
    $Expected = "disable:StockAgent_DailyResearch,enable:StockAgent_DailyResearch"
    if (($Changes -join ',') -cne $Expected -or $Tasks.StockAgent_DailyResearch -ne "Ready" -or
        $Tasks.StockAgent_DailyDashboard -ne "Disabled" -or $Tasks.StockAgent_WeeklyReview -ne "Disabled" -or
        $Tasks.StockAgent_PilotReconcile -ne "Ready") {
        throw "Unexpected restore result: $($Changes -join ',')"
    }
    # A completed restore record cannot re-enable a later independent disable.
    $Tasks.StockAgent_DailyResearch = "Disabled"
    & $Script -Enable -RecordPath $Record
    if (($Changes -join ',') -cne $Expected -or $Tasks.StockAgent_DailyResearch -ne "Disabled") {
        throw "Completed restore record touched a later disable"
    }
    $Saved = Get-Content -Raw -LiteralPath $Record | ConvertFrom-Json
    $Saved.tasks | ConvertTo-Json -Compress
    Write-Host "Windows task mock PASS. Applied actions: $($Changes -join ', ')"
} finally {
    if (Test-Path -LiteralPath $TestRoot) {
        Remove-Item -LiteralPath $TestRoot -Recurse -Force
    }
}
