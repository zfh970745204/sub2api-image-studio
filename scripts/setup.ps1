param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -e ".\backend[dev]"

Push-Location -LiteralPath "frontend"
try {
    npm install
    npm run build
}
finally {
    Pop-Location
}

Write-Host "Setup complete. Run .\scripts\start.ps1 to open the studio."

