param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    throw "Dependencies are missing. Run .\scripts\setup.ps1 first."
}

$env:APP_ENV = "development"
$env:DEPENDENCY_CHECKS_ENABLED = "false"
$env:LEGACY_SYNC_API_ENABLED = "true"

& ".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port $Port
