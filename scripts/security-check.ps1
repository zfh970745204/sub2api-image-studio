param(
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found at $python"
}

Push-Location $projectRoot
try {
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "pip check failed" }

    & npm audit --prefix frontend --omit=dev --audit-level=high
    if ($LASTEXITCODE -ne 0) { throw "npm audit found a high-severity runtime issue" }

    $pipAudit = Get-Command pip-audit -ErrorAction SilentlyContinue
    if ($pipAudit) {
        & $pipAudit.Source --requirement backend/constraints.lock
        if ($LASTEXITCODE -ne 0) { throw "pip-audit found a vulnerable dependency" }
    } elseif ($Strict) {
        throw "pip-audit is required in strict mode"
    } else {
        Write-Warning "pip-audit is not installed; CI performs the authoritative scan"
    }

    $pipLicenses = Get-Command pip-licenses -ErrorAction SilentlyContinue
    if ($pipLicenses) {
        & $pipLicenses.Source --fail-on="GPL-3.0-only;AGPL-3.0-only"
        if ($LASTEXITCODE -ne 0) { throw "Python license policy failed" }
    } elseif ($Strict) {
        throw "pip-licenses is required in strict mode"
    } else {
        Write-Warning "pip-licenses is not installed; CI performs the authoritative scan"
    }

    $gitleaks = Get-Command gitleaks -ErrorAction SilentlyContinue
    if ($gitleaks) {
        & $gitleaks.Source detect --source . --no-banner --redact
        if ($LASTEXITCODE -ne 0) { throw "gitleaks found a potential secret" }
    } elseif ($Strict) {
        throw "gitleaks is required in strict mode"
    } else {
        Write-Warning "gitleaks is not installed; CI performs the authoritative scan"
    }
} finally {
    Pop-Location
}
