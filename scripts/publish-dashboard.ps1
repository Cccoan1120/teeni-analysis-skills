param(
    [Parameter(Mandatory = $true)][string]$Workbook,
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$DataDate,
    [string]$PrimarySource,
    [string]$Url,
    [string]$OpeningPackage
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Local virtual environment is missing. Run scripts/start-local.ps1 first."
}
if ([string]::IsNullOrWhiteSpace($env:TEENI_PUBLISH_TOKEN)) {
    throw "TEENI_PUBLISH_TOKEN is not set in this shell."
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = $env:TEENI_DASHBOARD_PUBLISH_URL
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    throw "TEENI_DASHBOARD_PUBLISH_URL is not set in this shell."
}

$PreviousPublishUrl = $env:TEENI_DASHBOARD_PUBLISH_URL
try {
    $env:TEENI_DASHBOARD_PUBLISH_URL = $Url
    $PublishArgs = @('-m', 'publisher.cli', '--workbook', $Workbook, '--manifest', $Manifest, '--date', $DataDate)
    if (-not [string]::IsNullOrWhiteSpace($PrimarySource)) { $PublishArgs += @('--primary-source', $PrimarySource) }
    if (-not [string]::IsNullOrWhiteSpace($OpeningPackage)) { $PublishArgs += @('--opening-package', $OpeningPackage) }
    & $PythonExe @PublishArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    $env:TEENI_DASHBOARD_PUBLISH_URL = $PreviousPublishUrl
}
