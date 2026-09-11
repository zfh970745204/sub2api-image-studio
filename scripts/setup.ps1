param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -e ".\backend[dev]"
if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed" }

# Prepare the model before accepting image tasks; Docker images include it.
$modelDirectory = Join-Path $projectRoot "backend/data/models"
& ".venv\Scripts\python.exe" -m app.services.background_models --directory $modelDirectory
if ($LASTEXITCODE -ne 0) { throw "Background model provisioning failed" }

Push-Location -LiteralPath "frontend"
try {
    npm install
    npm run build
}
finally {
    Pop-Location
}

Write-Host "Setup complete. Run .\scripts\start.ps1 to open the studio."
