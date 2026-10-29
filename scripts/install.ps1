#Requires -Version 5.1
<#
.SYNOPSIS
    Install the Codex overload relay as a hidden, auto-restarting background service.

.DESCRIPTION
    Finds Python, generates hidden launchers, registers a watchdog in HKCU\...\Run
    (no admin rights required) and points Codex at the relay by editing only this
    project's two entries in config.toml. The pre-install file is backed up and
    scripts\uninstall.ps1 puts it back byte for byte.

.PARAMETER Port
    Local port for the relay. Default 8787. The config entry follows it.

.PARAMETER Proxy
    Upstream proxy, e.g. http://127.0.0.1:7897. Omit if your system proxy already
    covers chatgpt.com.

.PARAMETER NoAutostart
    Generate the launchers but do not register the watchdog to start at login.

.PARAMETER NoConfig
    Do not edit config.toml; print the snippet to paste instead.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Proxy http://127.0.0.1:7897
#>
[CmdletBinding()]
param(
    [int]$Port = 8787,
    [string]$Proxy = "",
    [switch]$NoAutostart,
    [switch]$NoConfig
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [!!] $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "Codex Overload Fix - installer" -ForegroundColor Cyan
Write-Host "-----------------------------" -ForegroundColor DarkGray

# ---------------------------------------------------------------- locations
$root      = Split-Path -Parent $PSScriptRoot
$relayPy   = Join-Path $root 'relay\relay.py'
$configPy  = Join-Path $root 'scripts\configure_codex.py'
$stateDir  = Join-Path $env:LOCALAPPDATA 'codex-overload-fix'
$logPath   = Join-Path $stateDir 'relay.log'
$startVbs  = Join-Path $stateDir 'start-relay.vbs'
$watchVbs  = Join-Path $stateDir 'watch-relay.vbs'
$runKey    = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName   = 'CodexOverloadRelay'

if (-not (Test-Path $relayPy)) {
    throw "relay.py not found at $relayPy - run this from the repository's scripts\ folder."
}

# ---------------------------------------------------------------- find python
Write-Step 'Looking for Python...'
$python = $null
foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    # A one-word candidate has no arguments. Slicing $parts[1..0] would hand the
    # executable its own name as first argument, and `python python -c ...`
    # fails, which used to make this candidate silently unusable.
    $prefix = @()
    if ($parts.Count -gt 1) { $prefix = $parts[1..($parts.Count - 1)] }
    try {
        $out = & $exe @prefix -c "import sys;print(sys.executable)" 2>$null
    } catch { continue }
    if (-not $out) { continue }
    $path = ($out | Select-Object -First 1).Trim()
    if (-not $path -or -not (Test-Path $path)) { continue }
    if ($path -match 'WindowsApps') { continue }   # Store stub
    $ver = & $path -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if (-not $ver) { continue }
    $major, $minor = $ver.Trim().Split('.')
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 9)) {
        Write-Warn2 "$path is Python $ver (need 3.9+)"
        continue
    }
    $python = $path
    Write-Ok "Python $ver at $python"
    break
}
if (-not $python) {
    throw 'No usable Python 3.9+ found. Install from https://python.org and re-run.'
}

# ---------------------------------------------------------------- config check
# Runs before a single file is written. configure_codex.py refuses anything it
# does not fully understand - a provider table nested under a header, a
# hand-edited relay table, a file it cannot parse - and if it refuses, nothing
# should have been installed. A relay running with Codex still pointed at the
# old route is worse than no install at all: it looks like it worked.
if (-not $NoConfig) {
    Write-Step 'Checking config.toml...'
    & $python $configPy check --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "config.toml was not changed (see above). Fix that, or run this script with -NoConfig to install the relay only."
    }
    Write-Ok 'config.toml can be edited safely'
}

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# ---------------------------------------------------------------- build command
$argList = @(
    "--port $Port",
    "--log `"$logPath`"",
    '--max-attempts 30',
    '--backoff-base 1',
    '--backoff-max 8',
    '--total-budget 300',
    '--commit-deadline 120'
)
if ($Proxy) { $argList += "--proxy $Proxy" }
$innerCmd = '"' + $python + '" "' + $relayPy + '" ' + ($argList -join ' ')

# VBS strings escape a double quote by doubling it.
$vbsCmd = $innerCmd.Replace('"', '""')

# ---------------------------------------------------------------- launcher
$startContent = @"
' Codex Overload Fix - hidden launcher. Generated by install.ps1, do not edit.
Set sh = CreateObject("WScript.Shell")
sh.Run "$vbsCmd", 0, False
"@
Set-Content -Path $startVbs -Value $startContent -Encoding ASCII
Write-Ok "launcher  $startVbs"

# ---------------------------------------------------------------- watchdog
$checkCmd = "cmd /c netstat -ano | findstr ""LISTENING"" | findstr "":$Port "" >nul 2>&1"
$watchContent = @"
' Codex Overload Fix - watchdog. Generated by install.ps1, do not edit.
' Runs forever with no window (wscript.exe is a GUI-subsystem host).
Set sh = CreateObject("WScript.Shell")
check = "$($checkCmd.Replace('"','""'))"
cmd   = "$vbsCmd"

Do
  rc = sh.Run(check, 0, True)
  If rc <> 0 Then
    sh.Run cmd, 0, True
    WScript.Sleep 3000
  Else
    WScript.Sleep 30000
  End If
Loop
"@
Set-Content -Path $watchVbs -Value $watchContent -Encoding ASCII
Write-Ok "watchdog  $watchVbs"

# ---------------------------------------------------------------- autostart
$registered = $false
if (-not $NoAutostart) {
    $entry = 'wscript.exe "' + $watchVbs + '"'
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name $runName -Value $entry
    $registered = $true
    Write-Ok "registered $runName in HKCU Run"
}

# ---------------------------------------------------------------- start now
Write-Step 'Starting the relay...'

# Launch the watchdog through WMI rather than Start-Process. A Start-Process
# child is a member of the caller's Job Object, so any host that holds a
# kill-on-close job (packaged apps and some editors do) takes the watchdog - and
# the relay under it - down the moment the call returns. Win32_Process.Create is
# serviced by WmiPrvSE.exe, so what it creates sits outside the caller's tree;
# and when the caller is packaged the new process carries no package identity,
# which is what lets it resolve the real %LOCALAPPDATA% instead of a virtualised
# view of it. It is still wscript.exe that runs, so the no-console-window rule
# is untouched.
$launched = $false
try {
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = 'wscript.exe "' + $watchVbs + '"'
    } -ErrorAction Stop
    $launched = ($created.ReturnValue -eq 0)
    if (-not $launched) {
        Write-Warn2 "WMI would not start the watchdog (return code $($created.ReturnValue))"
    }
} catch {
    Write-Warn2 'WMI is not available here'
}
if (-not $launched) {
    # Fallback only. This watchdog dies with a kill-on-close caller, which is
    # exactly the case the WMI launch above exists to avoid.
    Write-Warn2 'falling back to a plain launch - the relay may not outlive this shell'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $watchVbs + '"') -WindowStyle Hidden
}

# Give it up to 30s: a cold Python start behind an antivirus scan is slow, and a
# false failure here would undo a good install.
$listening = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        $listening = [bool](netstat -ano | Select-String ":$Port\s" | Select-String 'LISTENING')
    }
    if ($listening) { break }
}

if (-not $listening) {
    # Never point Codex at a port nothing is listening on: a config that looks
    # installed but cannot answer is worse than no install at all.
    if ($registered) {
        Remove-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
        Write-Warn2 'removed the autostart entry again'
    }
    Write-Warn2 "nothing is listening on 127.0.0.1:$Port"
    Write-Host ''
    Write-Host 'config.toml was not changed.' -ForegroundColor Yellow
    Write-Host "Log: $logPath" -ForegroundColor DarkGray
    Write-Host ''
    exit 1
}
Write-Ok "listening on 127.0.0.1:$Port"

# ---------------------------------------------------------------- config
# CODEX_HOME is what Codex itself honours; fall back to the default location.
if ($env:CODEX_HOME) {
    $configPath = Join-Path $env:CODEX_HOME 'config.toml'
} else {
    $configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
}
$manual = [bool]$NoConfig
Write-Host ""
if (-not $manual) {
    Write-Step 'Pointing Codex at the relay...'
    & $python $configPy install --port $Port
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'could not edit config.toml automatically; add the block below by hand'
        $manual = $true
    }
}

if ($manual) {
    Write-Host "Add this to $configPath" -ForegroundColor Cyan
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    $snippet = @"
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:$Port"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10
"@
    Write-Host $snippet -ForegroundColor White
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Keep a backup of config.toml before editing." -ForegroundColor Yellow
}

Write-Host "Then fully restart Codex." -ForegroundColor Yellow
Write-Host ""
Write-Host "Log:     $logPath" -ForegroundColor DarkGray
Write-Host "Stop:    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1" -ForegroundColor DarkGray
Write-Host ""
