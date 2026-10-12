<#
.SYNOPSIS
    Register auto-audiobook as a native Windows scheduled task.

.DESCRIPTION
    Runs serve_win.py under the repo's .venv-win via pythonw.exe, so there is
    no console window to close by accident and all output lands in
    data/server.log.

    Two triggers, deliberately:

      * AtLogOn      - starts it with the desktop. The TTS server is a Docker
                       Desktop container, and Docker Desktop itself only starts
                       at logon, so a boot-time service would come up first and
                       sit unable to reach :8006.
      * Once + PT1M  - the keepalive. Task Scheduler's RestartCount/-Interval
                       does NOT reliably cover "the process died" - a killed
                       server leaves the task Ready and never returns. A plain
                       repetition plus MultipleInstances=IgnoreNew does: every
                       minute it tries to start, which is a no-op while the
                       server is healthy and a restart when it is not.
                       A repetition hung off the AtLogOn trigger will not work
                       for this - it only arms when a logon event fires, so it
                       stays dormant for the session you install from.

    ExecutionTimeLimit is unlimited; the default three days would kill a
    long-running book job partway through.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\auto-audiobook.ps1
    powershell -ExecutionPolicy Bypass -File deploy\auto-audiobook.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$TaskName = "auto-audiobook",
    [string]$RepoPath,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# Not a param default: $PSScriptRoot is still empty while defaults are bound
# under PowerShell 5.1, which yields an empty path instead of the repo root.
if (-not $RepoPath) { $RepoPath = Split-Path -Parent (Split-Path -Parent $PSCommandPath) }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "unregistered $TaskName"
    return
}

$pyw = Join-Path $RepoPath ".venv-win\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
    throw "no Windows venv at $pyw - create it first:`n" +
          "  uv venv --python 3.12 .venv-win`n" +
          "  uv pip install --python .venv-win\Scripts\python.exe -r requirements.txt`n" +
          "(3.12, not 3.14: lxml 5.3.0 ships no cp314 wheel and the source build needs libxml2)"
}

$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pyw -Argument "serve_win.py" -WorkingDirectory $RepoPath

$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$tBeat  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) `
             -RepetitionInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($tLogon, $tBeat) `
    -Principal $principal -Settings $settings `
    -Description "auto-audiobook FastAPI app on 127.0.0.1:8005" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "registered and started $TaskName - http://127.0.0.1:8005"
