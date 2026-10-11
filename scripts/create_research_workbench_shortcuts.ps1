# Create desktop shortcuts for starting and stopping the Research Runtime Workbench.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_research_workbench_shortcuts.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_research_workbench_shortcuts.ps1 -DesktopPath C:\path\to\desktop
#
# Creates ResearchWorkbenchStart.lnk and ResearchWorkbenchStop.lnk on the user's
# desktop (or the folder given by -DesktopPath). Each shortcut invokes the
# corresponding script through powershell.exe with the project directory as the
# working directory.
#
# Safety: a shortcut that already exists at the target name is replaced only
# when it is verifiably ours, which requires all three of:
#   * the canonical TargetPath is the expected powershell.exe,
#   * the Arguments contain the exact quoted path of our script, and
#   * the WorkingDirectory canonicalizes to this project root.
# An existing shortcut that fails any of those checks is refused and never
# overwritten.

param(
    [string]$DesktopPath = [Environment]::GetFolderPath("Desktop")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
    throw "Could not resolve the Desktop folder."
}

$PowerShellExe = Join-Path $PSHOME "powershell.exe"
$StartScript = Join-Path $ProjectRoot "scripts\start_research_workbench.ps1"
$StopScript = Join-Path $ProjectRoot "scripts\stop_research_workbench.ps1"
$StartShortcut = Join-Path $DesktopPath "ResearchWorkbenchStart.lnk"
$StopShortcut = Join-Path $DesktopPath "ResearchWorkbenchStop.lnk"

if (-not (Test-Path -LiteralPath $StartScript)) {
    throw "Start script not found: $StartScript"
}
if (-not (Test-Path -LiteralPath $StopScript)) {
    throw "Stop script not found: $StopScript"
}

# Fail-closed ownership test. Returns $true only when the existing shortcut's
# canonical target is the expected powershell.exe, its arguments contain the
# exact quoted path of our script, and its working directory canonicalizes to
# the project root. Any parse or lookup problem is treated as "not ours" so an
# ambiguous shortcut is never overwritten.
function Test-IsOurShortcut {
    param(
        [string]$ShortcutPath,
        [string]$ExpectedTarget,
        [string]$ExpectedScript
    )
    if (-not (Test-Path -LiteralPath $ShortcutPath)) {
        return $false
    }
    try {
        $WshShell = New-Object -ComObject WScript.Shell
        $sc = $WshShell.CreateShortcut($ShortcutPath)

        $target = [string]$sc.TargetPath
        if ([string]::IsNullOrWhiteSpace($target)) {
            return $false
        }
        $targetCanon = [System.IO.Path]::GetFullPath($target)
        $expectedTargetCanon = [System.IO.Path]::GetFullPath($ExpectedTarget)
        if (-not $targetCanon.Equals($expectedTargetCanon, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }

        $arguments = [string]$sc.Arguments
        if ([string]::IsNullOrWhiteSpace($arguments)) {
            return $false
        }
        $expectedScriptCanon = [System.IO.Path]::GetFullPath($ExpectedScript)
        $expectedQuoted = '"' + $expectedScriptCanon + '"'
        if (-not $arguments.ToLowerInvariant().Contains($expectedQuoted.ToLowerInvariant())) {
            return $false
        }

        $workDir = [string]$sc.WorkingDirectory
        if ([string]::IsNullOrWhiteSpace($workDir)) {
            return $false
        }
        $workDirCanon = [System.IO.Path]::GetFullPath($workDir)
        $projectRootCanon = [System.IO.Path]::GetFullPath($ProjectRoot)
        if (-not $workDirCanon.Equals($projectRootCanon, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }

        return $true
    } catch {
        return $false
    }
}

# Never overwrite unrelated shortcut names. Existing shortcuts that pass the
# strict ownership check are ours and may be refreshed; anything else is
# refused before any shortcut is written.
foreach ($pair in @(
    @{ Name = "Start"; Path = $StartShortcut; Script = $StartScript },
    @{ Name = "Stop";  Path = $StopShortcut;  Script = $StopScript }
)) {
    if (Test-Path -LiteralPath $pair.Path) {
        if (-not (Test-IsOurShortcut -ShortcutPath $pair.Path -ExpectedTarget $PowerShellExe -ExpectedScript $pair.Script)) {
            throw "Refusing to overwrite existing unrelated shortcut: $($pair.Path)"
        }
    }
}

$WshShell = New-Object -ComObject WScript.Shell

$Start = $WshShell.CreateShortcut($StartShortcut)
$Start.TargetPath = $PowerShellExe
$Start.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $StartScript + '"'
$Start.WorkingDirectory = $ProjectRoot
$Start.Description = "Start Research Runtime Workbench"
$Start.Save()

$Stop = $WshShell.CreateShortcut($StopShortcut)
$Stop.TargetPath = $PowerShellExe
$Stop.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $StopScript + '"'
$Stop.WorkingDirectory = $ProjectRoot
$Stop.Description = "Stop Research Runtime Workbench"
$Stop.Save()

Write-Host "Shortcut created: $StartShortcut"
Write-Host "Shortcut created: $StopShortcut"
Write-Host "Target: $PowerShellExe"
Write-Host "Working directory: $ProjectRoot"
