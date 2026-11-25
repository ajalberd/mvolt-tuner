# run-tuner.ps1 - launched by the scheduled task at logon.
# Reads data\run-args.json (a JSON array written by the tuner) so the sweep can be
# changed without re-registering. Arguments are passed as an array, so values
# containing spaces (e.g. a profile name) survive intact.
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ArgsFile = Join-Path $Root 'data\run-args.json'
$LogFile = Join-Path $Root 'data\task.log'

function W([string]$m) {
    $l = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $LogFile -Value $l -Encoding UTF8
}

Set-Location $Root
W "task start (pid $PID)"

if (-not (Test-Path $ArgsFile)) {
    W "no run-args.json - nothing to do"
    exit 0
}

try {
    $argList = @(Get-Content $ArgsFile -Raw | ConvertFrom-Json)
} catch {
    W "run-args.json unreadable - nothing to do"
    exit 0
}
if ($argList.Count -eq 0) {
    W "run-args.json empty - nothing to do"
    exit 0
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { W "python not found on PATH"; exit 1 }

W ("args: " + ($argList -join ' '))
$out = & $py -m tuner @argList 2>&1
foreach ($line in $out) { W $line }
W "task end (exit $LASTEXITCODE)"
