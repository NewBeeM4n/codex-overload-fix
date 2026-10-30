#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the Codex overload relay and remove its autostart entry.

.DESCRIPTION
    Restores config.toml first, then removes the HKCU Run entry, terminates the
    watchdog and relay processes, and optionally deletes the generated launchers
    and logs.

    The order matters. If config.toml cannot be restored - because the relay
    entries were edited by hand since install - this stops with a non-zero exit
    and leaves the relay running. Stopping it while Codex still points at the
    port would turn a fixable problem into "everything is broken".

.PARAMETER Purge
    Also delete the state directory (launchers and logs), including the legacy location.

.PARAMETER NoConfig
    Leave config.toml alone; only stop and remove the relay.
#>
[CmdletBinding()]
param(
    [string]$StateDir = "",
    [switch]$Purge,
    [switch]$NoConfig
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }

Write-Host ""
Write-Host "Codex Overload Fix - uninstaller" -ForegroundColor Cyan
Write-Host "-------------------------------" -ForegroundColor DarkGray

$stateDir = if ($StateDir) { [IO.Path]::GetFullPath($StateDir) } else { Join-Path $env:USERPROFILE '.codex-overload-fix' }
$legacyDir = Join-Path $env:LOCALAPPDATA 'codex-overload-fix'
$stateDirs = @($stateDir, $legacyDir) | Select-Object -Unique
. (Join-Path $PSScriptRoot 'windows-autostart.ps1')
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName  = 'CodexOverloadRelay'

# ---------------------------------------------------------------- config first
# Everything below this block is destructive to the running relay, so the one
# step that can legitimately refuse runs first.
if (-not $NoConfig) {
    $configPy = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\configure_codex.py'
    $python = $null
    foreach ($candidate in @('py -3', 'python', 'python3')) {
        $parts = $candidate.Split(' ')
        $exe = $parts[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $prefix = @()
        if ($parts.Count -gt 1) { $prefix = $parts[1..($parts.Count - 1)] }
        try {
            $out = & $exe @prefix -c "import sys;print(sys.executable)" 2>$null
        } catch { continue }
        $path = ($out | Select-Object -First 1).Trim()
        if ($path -and (Test-Path $path) -and ($path -notmatch 'WindowsApps')) { $python = $path; break }
    }

    if ($python -and (Test-Path $configPy)) {
        Write-Step 'Restoring config.toml...'
        & $python $configPy remove
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "config.toml was not restored, so the relay is still running." -ForegroundColor Red
            Write-Host "Codex is still pointed at it; stopping the relay now would break Codex." -ForegroundColor Red
            Write-Host "Reconcile the entries above (or pass -NoConfig to leave config.toml alone)," -ForegroundColor Red
            Write-Host "then run this uninstaller again." -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host 'Python or configure_codex.py is missing; config.toml and the relay were left untouched.' -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------- autostart
# WMI accesses the unvirtualized current-user key even from packaged hosts.
Set-RealRelayAutostart $runName "" $true
Write-Ok "removed $runName from the real HKCU Run key (if present)"

# ---------------------------------------------------------------- processes
$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'wscript.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $line = $_.CommandLine; $line -and @($stateDirs | Where-Object { $line.IndexOf((Join-Path $_ 'watch-relay.vbs'), [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0 } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $line = $_.CommandLine; $line -and $line -like '*relay.py*' -and @($stateDirs | Where-Object { $line.IndexOf((Join-Path $_ 'relay.log'), [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0 } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

if ($killed -gt 0) {
    Write-Ok "stopped $killed process(es)"
} else {
    Write-Step 'nothing was running'
}

# ---------------------------------------------------------------- purge
if ($Purge) {
    foreach ($dir in $stateDirs) {
        $target = [IO.Path]::GetFullPath($dir).TrimEnd('\')
        if (-not $target -or $target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\')) { throw 'Refusing to purge a filesystem root' }
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
            Write-Ok "deleted $target"
        }
    }
}

Write-Host ""
Write-Host "The relay is stopped and will not start at login again." -ForegroundColor Green
Write-Host "Restart Codex to go back to a direct connection." -ForegroundColor Green
Write-Host ""
