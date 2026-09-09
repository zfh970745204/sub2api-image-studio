param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path -LiteralPath "$projectRoot\.venv\Scripts\python.exe")) {
    throw "Dependencies are missing. Run .\scripts\setup.ps1 first."
}

Push-Location -LiteralPath "$projectRoot\backend"
try {
    & "..\.venv\Scripts\python.exe" -m arq app.workers.scheduler.SchedulerSettings
}
finally {
    Pop-Location
}
