# Edith - installer for Windows 10/11.
# Installs uv, Python 3.12 and packages into .venv (nothing system-wide), then opens the
# setup wizard (python -m bot.cli setup). Usage: double-click setup.bat
# Flags: -SkipDeps (don't reinstall packages)
param([switch]$SkipDeps)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!] $m" -ForegroundColor Yellow }

Step 'Checking uv (Python manager)'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { $uv = $uv.Source }
else {
    $localUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $localUv) { $uv = $localUv }
}
if (-not $uv) {
    Warn 'uv not found - installing it from astral.sh (official installer)'
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uv)) { throw 'uv install failed. Install it from https://docs.astral.sh/uv/ and re-run setup.' }
}
Ok "uv: $(& $uv --version)"

$Py = Join-Path $Root '.venv\Scripts\python.exe'
$needVenv = $true
if (Test-Path $Py) {
    $ver = & $Py -c "import sys;print('%d.%d' % sys.version_info[:2])"
    if ($ver -eq '3.12') { $needVenv = $false } else { Warn "existing .venv is Python $ver - recreating with 3.12" }
}
if ($needVenv) {
    Step 'Creating Python 3.12 environment (.venv) - uv downloads Python if needed'
    if (Test-Path (Join-Path $Root '.venv')) { Remove-Item -Recurse -Force (Join-Path $Root '.venv') }
    & $uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { throw 'uv venv failed' }
}
Ok "Python: $(& $Py --version)"

if (-not $SkipDeps) {
    Step 'Installing packages (first run downloads ~1 GB, takes a few minutes)'
    & $uv pip sync requirements.txt --python $Py
    if ($LASTEXITCODE -ne 0) { throw 'package install failed - see errors above' }
    Ok 'packages installed'
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        Step 'NVIDIA GPU found - installing CUDA libraries for fast speech-to-text (~1 GB)'
        & $uv pip install -r requirements-gpu.txt --python $Py
        if ($LASTEXITCODE -eq 0) { Ok 'GPU speech-to-text enabled' } else { Warn 'GPU libraries failed - voice will use the CPU (slower, still works)' }
    }
}

Step 'Opening the setup wizard'
& $Py -m bot.cli setup
exit $LASTEXITCODE
