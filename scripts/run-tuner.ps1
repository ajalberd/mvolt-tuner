# run-tuner.ps1 - launched by the scheduled task at logon.
# Reads data\run-args.txt so the sweep can be changed without re-registering.
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ArgsFile = Join-Path $Root 'data\run-args.txt'
$LogFile = Join-Path $Root 'data\task.log'

function W([string]$m) {
    $l = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $LogFile -Value $l -Encoding UTF8
}

Set-Location $Root
W "task start (pid $PID)"

if (-not (Test-Path $ArgsFile)) {
    W "no run-args.txt - nothing to do"
    exit 0
}

$extra = (Get-Content $ArgsFile -Raw).Trim()
if (-not $extra) {
    W "run-args.txt empty - nothing to do"
    exit 0
}

W "args: $extra"
$py = 'python'
if (-not (Test-Path $py)) { $py = 'python' }
$out = & $py -m tuner $extra.Split(' ') 2>&1
foreach ($line in $out) { W $line }
W "task end (exit $LASTEXITCODE)"

