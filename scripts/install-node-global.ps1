<#
.SYNOPSIS
    Install a global command for the Node TUI (node_tui\run.bat).

.DESCRIPTION
    Creates per-user commands that delegate to this repository's Node TUI
    entrypoint. The commands work from any terminal directory and do not need
    administrator privileges.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$runBat = Join-Path $projectRoot "node_tui\run.bat"
if (-not (Test-Path -LiteralPath $runBat)) {
    throw "Cannot find the Node TUI entrypoint: $runBat"
}

$binDir = Join-Path $env:LOCALAPPDATA "ProofTask\bin"
New-Item -ItemType Directory -Path $binDir -Force | Out-Null

# Escape percent signs because the path is embedded in a cmd.exe variable.
$runForCmd = $runBat.Replace('%', '%%')
$launcher = @"
@echo off
setlocal
call "$runForCmd" %*
set "PROOFTASK_EXIT=%ERRORLEVEL%"
exit /b %PROOFTASK_EXIT%
"@
$launcher = $launcher.TrimStart()

foreach ($commandName in @("prooftask.cmd", "proof-task.cmd", "run.cmd", "run.bat")) {
    Set-Content -LiteralPath (Join-Path $binDir $commandName) -Value $launcher -Encoding ASCII
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ';' | Where-Object { $_.Trim() })
if (-not ($pathEntries | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') })) {
    [Environment]::SetEnvironmentVariable("Path", (($pathEntries + $binDir) -join ';'), "User")
    Write-Host "Added $binDir to the current user's PATH."
} else {
    Write-Host "$binDir is already on the current user's PATH."
}

Write-Host "Installed commands: prooftask, proof-task, run (and run.bat)"
Write-Host "Open a new terminal, then run: prooftask"
