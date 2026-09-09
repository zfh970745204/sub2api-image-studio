param(
    [Parameter(Mandatory = $true)]
    [string]$PostgresDsn,
    [Parameter(Mandatory = $true)]
    [string]$AgeRecipient,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($resolvedOutput) | Out-Null
$stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
$dumpPath = Join-Path $resolvedOutput "sub2image-$stamp.dump.tmp"
$encryptedPath = Join-Path $resolvedOutput "sub2image-$stamp.dump.age"
$manifestPath = Join-Path $resolvedOutput "sub2image-$stamp.sha256"

if (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
    throw "pg_dump is required"
}
if (-not (Get-Command age -ErrorAction SilentlyContinue)) {
    throw "age is required"
}

try {
    & pg_dump --dbname=$PostgresDsn --format=custom --file=$dumpPath
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
    & age --recipient $AgeRecipient --output $encryptedPath $dumpPath
    if ($LASTEXITCODE -ne 0) { throw "age encryption failed" }
    $hash = (Get-FileHash -LiteralPath $encryptedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([System.IO.Path]::GetFileName($encryptedPath))" | Set-Content -LiteralPath $manifestPath -Encoding ascii
} finally {
    if (Test-Path -LiteralPath $dumpPath) {
        Remove-Item -LiteralPath $dumpPath -Force
    }
}

Write-Output $encryptedPath
Write-Output $manifestPath
