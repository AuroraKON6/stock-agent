# Install Windows Scheduled Tasks for Stock Agent
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_scheduled_tasks.ps1 -DryRun
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_scheduled_tasks.ps1

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptPath = Join-Path $ProjectRoot "scripts\run_daily_research.ps1"
$TaskPrefix = "StockAgent_"
$BackupStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $ProjectRoot "logs\scheduled_task_backups\$BackupStamp"

function Register-TaskWithSchtasks {
    param([hashtable]$Task)
    $Escape = { param([string]$Value) [Security.SecurityElement]::Escape($Value) }
    $DayXml = (($Task.DaysOfWeek | ForEach-Object { "          <$($_) />" }) -join "`r`n")
    $Boundary = (Get-Date -Hour $Task.Hour -Minute $Task.Minute -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")
    $UserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $TaskName = [string]$Task.Name
    $TaskDescription = & $Escape ([string]$Task.Description)
    $ActionArgs = & $Escape ("-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`" $($Task.Args)")
    $ActionRoot = & $Escape $ProjectRoot
    $Xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$TaskDescription</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$Boundary</StartBoundary><Enabled>true</Enabled>
      <ScheduleByWeek><DaysOfWeek>
$DayXml
      </DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$UserSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit><Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>$ActionArgs</Arguments><WorkingDirectory>$ActionRoot</WorkingDirectory></Exec></Actions>
</Task>
"@
    $StagingDir = Join-Path $ProjectRoot "logs\scheduled_task_staging\$BackupStamp"
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null
    $XmlPath = Join-Path $StagingDir "$TaskName.xml"
    $Xml | Out-File -LiteralPath $XmlPath -Encoding Unicode
    $null = [xml](Get-Content -Raw -LiteralPath $XmlPath)
    $XmlRelativePath = "logs\scheduled_task_staging\$BackupStamp\$TaskName.xml"
    $Arguments = @("/Create", "/TN", $TaskName, "/XML", $XmlRelativePath, "/F")
    Push-Location $ProjectRoot
    try {
        & schtasks.exe @Arguments | Out-Host
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks.exe failed with exit code $LASTEXITCODE"
    }
}

$Tasks = @(
    @{
        Name = "${TaskPrefix}DailyResearch"
        Description = "Stock Agent daily research run with isolated Pi Agent orchestration"
        Args = "-Mode daily -TriggerSource scheduled -Config config\watchlist.local.json"
        Hour = 8
        Minute = 30
        DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    },
    @{
        Name = "${TaskPrefix}DailyDashboard"
        Description = "Stock Agent close-of-day runtime, workspace, dashboard, and alert refresh"
        Args = "-Mode dashboard -TriggerSource scheduled -SkipPi -Config config\watchlist.local.json"
        Hour = 18
        Minute = 0
        DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    },
    @{
        Name = "${TaskPrefix}PilotReconcile"
        Description = "Stock Agent independent shadow-run reconciliation and Go-No-Go refresh"
        Args = "-Mode pilot -TriggerSource scheduled -SkipPi -Config config\watchlist.local.json"
        Hour = 8
        Minute = 45
        DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    },
    @{
        Name = "${TaskPrefix}WeeklyReview"
        Description = "Stock Agent weekly review summary"
        Args = "-Mode review -TriggerSource scheduled -Config config\watchlist.local.json"
        Hour = 9
        Minute = 0
        DaysOfWeek = @("Saturday")
    }
)

Write-Host "=== Stock Agent Scheduled Tasks ==="
Write-Host "Project root: $ProjectRoot"
Write-Host "Script: $ScriptPath"
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] The following tasks would be created:"
} else {
    Write-Host "Creating scheduled tasks..."
}

foreach ($task in $Tasks) {
    $taskName = $task.Name
    $triggerArgs = @{
        Weekly = $true
        At = (Get-Date -Hour $task.Hour -Minute $task.Minute -Second 0)
        DaysOfWeek = $task.DaysOfWeek
    }

    Write-Host ""
    Write-Host "Task: $taskName"
    Write-Host "  Description: $($task.Description)"
    Write-Host "  Schedule: $($task.DaysOfWeek -join ', ') at $($task.Hour):$($task.Minute.ToString('00'))"
    Write-Host "  Command: powershell -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`" $($task.Args)"

    if (-not $DryRun) {
        # Remove existing task if present
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing) {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
            try {
                Export-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-File `
                    -LiteralPath (Join-Path $BackupDir "$taskName.xml") -Encoding utf8
            } catch {
                & schtasks.exe /Query /TN $taskName /XML | Out-File `
                    -LiteralPath (Join-Path $BackupDir "$taskName.xml") -Encoding utf8
                if ($LASTEXITCODE -ne 0) { throw "Unable to back up existing task $taskName" }
            }
            Write-Host "  [BACKUP] existing task -> $BackupDir"
            try {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            } catch {
                & schtasks.exe /Delete /TN $taskName /F | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "Unable to remove existing task $taskName" }
            }
            Write-Host "  [REMOVED] existing task"
        }

        try {
            $action = New-ScheduledTaskAction `
                -Execute "powershell.exe" `
                -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`" $($task.Args)" `
                -WorkingDirectory $ProjectRoot -ErrorAction Stop

            $trigger = New-ScheduledTaskTrigger -Weekly `
                -At (Get-Date -Hour $task.Hour -Minute $task.Minute -Second 0) `
                -DaysOfWeek $task.DaysOfWeek -ErrorAction Stop

            $settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Hours 1) -ErrorAction Stop

            Register-ScheduledTask `
                -TaskName $taskName `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description $task.Description `
                -Force -ErrorAction Stop | Out-Null
        } catch {
            Write-Warning "ScheduledTasks cmdlets unavailable; trying current-user schtasks.exe fallback."
            Register-TaskWithSchtasks -Task $task
        }

        Write-Host "  [OK] Task registered"
    }
}

Write-Host ""
if ($DryRun) {
    Write-Host "[DRY RUN] No tasks were actually created."
    Write-Host "Run without -DryRun to install."
} else {
    Write-Host "Done. Installed $($Tasks.Count) tasks."
    if (Test-Path $BackupDir) { Write-Host "Previous task backups: $BackupDir" }
    Write-Host ""
    Write-Host "To verify: Get-ScheduledTask -TaskName '${TaskPrefix}*'"
    Write-Host "To uninstall: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall_scheduled_tasks.ps1"
}
