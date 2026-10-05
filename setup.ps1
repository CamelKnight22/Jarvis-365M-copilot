# One-time setup on a new Windows PC. Run from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1            (auto-detects GPU)
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -VSCode    (also installs the VS Code extension)
param([switch]$VSCode, [switch]$Cpu)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Step 'Checking prerequisites'
& py -3.12 --version 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 not found. Install it from python.org or the Microsoft Store (tick "py launcher"), then rerun.' }
git --version | Out-Null  # needed to install Chatterbox from GitHub

$gpu = -not $Cpu -and [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$torchIndex = if ($gpu) { 'https://download.pytorch.org/whl/cu124' } else { 'https://download.pytorch.org/whl/cpu' }
Write-Host ("Torch build: " + $(if ($gpu) { 'CUDA (NVIDIA GPU found)' } else { 'CPU only' }))

if (-not (Test-Path .venv)) { Step 'Creating virtual environment'; py -3.12 -m venv .venv }
$py = '.\.venv\Scripts\python.exe'
function Pip { & $py -m pip @args; if ($LASTEXITCODE -ne 0) { throw "pip $args failed" } }

Step 'Installing PyTorch (~200 MB CPU / ~2.5 GB CUDA)'
Pip install --upgrade pip --quiet
Pip install torch==2.6.0 torchaudio==2.6.0 --index-url $torchIndex

Step 'Installing packages'
Pip install -r requirements.txt "setuptools<81"
Pip install --no-deps "git+https://github.com/resemble-ai/chatterbox.git@5de7a54aa4e5e2baadb0182dde554908b48b85c2"

Step 'Downloading models (Chatterbox Nano ~1.85 GB, Whistle ~17 MB) and testing them'
$env:NEEDLE_TELEMETRY = '0'; $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
& $py -W ignore server.py --check
if ($LASTEXITCODE -ne 0) { throw 'Model download/test failed (see above). Behind a corporate proxy? Set HTTPS_PROXY and rerun.' }

if (-not (Test-Path config.json)) {
    Copy-Item config.example.json config.json
    Write-Host "`nCreated config.json - fill in tenant_id and client_id from IT." -ForegroundColor Yellow
}

if ($VSCode) {
    Step 'Linking VS Code extension'
    $link = "$env:USERPROFILE\.vscode\extensions\jarvis.jarvis-voice-0.1.0"
    if (-not (Test-Path $link)) { New-Item -ItemType Junction -Path $link -Target "$PSScriptRoot\vscode-jarvis" | Out-Null }
    Write-Host 'Reload VS Code, then press Ctrl+Alt+J.'
}

Write-Host "`nDone. Start Jarvis with .\Jarvis.bat" -ForegroundColor Green
