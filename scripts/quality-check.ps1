param(
    [switch]$Integration
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = @(
    (Join-Path $projectRoot ".venv/Scripts/python.exe"),
    (Join-Path $projectRoot ".venv/bin/python")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $pythonCommand) {
        throw "Python executable not found"
    }
    $python = $pythonCommand.Source
}

Push-Location $projectRoot
try {
    & $python -m ruff check backend
    if ($LASTEXITCODE -ne 0) { throw "Ruff check failed" }
    & $python -m ruff format --check backend
    if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed" }

    Push-Location backend
    try {
        & $python -m mypy
        if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
        & $python -m pytest -m "not integration" --cov --cov-report="term-missing:skip-covered" --cov-report="json:coverage.json"
        if ($LASTEXITCODE -ne 0) { throw "backend tests or quality-scope coverage gate failed" }
    } finally {
        Pop-Location
    }
    & $python scripts/check-coverage.py backend/coverage.json
    if ($LASTEXITCODE -ne 0) { throw "core coverage gate failed" }

    & npm run typecheck --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw "frontend typecheck failed" }
    & npm test --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw "frontend component tests failed" }
    & npm run build --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw "frontend production build failed" }

    if ($Integration) {
        Push-Location backend
        try {
            & $python -m pytest -m integration
            if ($LASTEXITCODE -ne 0) { throw "external integration tests failed" }
        } finally {
            Pop-Location
        }
    }
} finally {
    Pop-Location
}
