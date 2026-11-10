# install-tasks.ps1 - register the resume task and the SYSTEM watchdog.
# Run elevated. Idempotent (uses -Force).
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$User = "$env:COMPUTERNAME\$env:USERNAME"

# ---- 1. resume at logon ----
$resumeAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\run-tuner.ps1`""
$resumeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$resumeTrigger.Delay = 'PT30S'
$resumePrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$resumeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'mVoltTuner-Resume' -Action $resumeAction `
    -Trigger $resumeTrigger -Principal $resumePrincipal -Settings $resumeSettings -Force | Out-Null

# ---- 2. SYSTEM watchdog, every minute ----
$wdAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\watchdog.ps1`" -StaleSeconds 120"
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$wdPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'mVoltTuner-Watchdog' -Action $wdAction `
    -Trigger $wdTrigger -Principal $wdPrincipal -Settings $wdSettings -Force | Out-Null

# ---- 3. ThrottleStop at logon (CPU state must be constant across trials) ----
$tsExe = Join-Path $env:USERPROFILE 'Desktop\ThrottleStop.exe'
if (Test-Path $tsExe) {
    $tsAction = New-ScheduledTaskAction -Execute $tsExe -WorkingDirectory (Split-Path $tsExe)
    $tsTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $tsTrigger.Delay = 'PT10S'
    $tsPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    $tsSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName 'mVoltTuner-ThrottleStop' -Action $tsAction `
        -Trigger $tsTrigger -Principal $tsPrincipal -Settings $tsSettings -Force | Out-Null
} else {
    Write-Warning "ThrottleStop.exe not found at $tsExe - task not registered"
}

# ---- report ----
Get-ScheduledTask -TaskName 'mVoltTuner-*' | Select-Object TaskName, State |
    Format-Table -AutoSize
Get-ScheduledTaskInfo -TaskName 'mVoltTuner-Watchdog' |
    Select-Object NextRunTime, LastRunTime | Format-List

