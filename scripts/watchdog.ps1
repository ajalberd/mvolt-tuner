# watchdog.ps1 - reboots if the orchestrator heartbeat goes stale.
# SAFETY: does nothing unless data\armed.flag exists.
param(
    [int]$StaleSeconds = 120,
    [int]$MaxCrashes   = 8,
    [switch]$DryRun
)

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Hb     = Join-Path $Root 'data\heartbeat.json'
$Armed  = Join-Path $Root 'data\armed.flag'
$StateF = Join-Path $Root 'data\watchdog-state.json'
$LogF   = Join-Path $Root 'data\watchdog.log'

function W([string]$m) {
    $l = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $LogF -Value $l -Encoding UTF8
    Write-Host $l
}

function Get-State {
    if (Test-Path $StateF) {
        try { return (Get-Content $StateF -Raw | ConvertFrom-Json) } catch {}
    }
    return [pscustomobject]@{ reboots = 0; lastReboot = $null; disarmed = $false }
}
function Save-State($s) { $s | ConvertTo-Json | Set-Content -Path $StateF -Encoding UTF8 }

# guard 0: never act unless a run is armed
if (-not (Test-Path $Armed)) { exit 0 }

$st = Get-State
if ($st.disarmed) { exit 0 }

# guard 1: heartbeat must exist
if (-not (Test-Path $Hb)) {
    W "armed but no heartbeat file - not acting"
    exit 0
}

# guard 2: staleness
$hbObj = try { Get-Content $Hb -Raw | ConvertFrom-Json } catch { $null }
if (-not $hbObj) { W "heartbeat unreadable - not acting"; exit 0 }

$v = $hbObj.utc
if ($v -is [datetime]) {
    $hbUtc = $v.ToUniversalTime()
} else {
    try {
        $hbUtc = [datetime]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture,
                  [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    } catch {
        W "heartbeat timestamp unparseable - not acting"
        exit 0
    }
}
$age = [math]::Round(((Get-Date).ToUniversalTime() - $hbUtc).TotalSeconds, 1)

if ($age -lt $StaleSeconds) { exit 0 }

W ("STALE heartbeat: {0}s old (threshold {1}s)" -f $age, $StaleSeconds)

# guard 2b: a tuner that is simply gone (window closed, killed) left the flag
# behind. The OS is fine, so rebooting would only destroy the user's work.
$tunerPid = 0
try { $tunerPid = [int]$hbObj.pid } catch {}
if ($tunerPid -gt 0 -and -not (Get-Process -Id $tunerPid -ErrorAction SilentlyContinue)) {
    W ("tuner process {0} is not running - disarming instead of rebooting" -f $tunerPid)
    Remove-Item $Armed -Force -ErrorAction SilentlyContinue
    exit 0
}

# guard 3: reboot budget
if ($st.reboots -ge $MaxCrashes) {
    $st.disarmed = $true
    Save-State $st
    W ("reboot budget exhausted ({0}/{1}) - DISARMED, manual attention required" -f $st.reboots, $MaxCrashes)
    Remove-Item $Armed -Force -ErrorAction SilentlyContinue
    exit 0
}

if ($DryRun) {
    W ("DRY RUN - would reboot now (reboots so far: {0}/{1})" -f $st.reboots, $MaxCrashes)
    exit 0
}

$st.reboots = $st.reboots + 1
$st.lastReboot = (Get-Date).ToString('o')
Save-State $st

W ("REBOOTING (reboot {0}/{1}) in 5s" -f $st.reboots, $MaxCrashes)
& shutdown.exe /r /t 5 /f /c "mVolt tuner watchdog: GPU heartbeat stale"

