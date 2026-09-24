[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Csv,
    [Parameter(Mandatory = $true)][string]$DataDate,
    [Parameter(Mandatory = $true)][ValidateSet("M1", "M2")][string]$ProductVersion,
    [string]$OutputDir,
    [string]$Url,
    [string]$PreviousBaseManifest,
    [string]$PreviousPrimarySource
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$SceneId = switch ($ProductVersion) {
    "M1" { "488" }
    "M2" { "904" }
}

function Resolve-RequiredFile {
    param([string]$Path, [string]$Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-RequiredDirectory {
    param([string]$Path, [string]$Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SecretsFile = Join-Path $ProjectRoot ".local-secrets.ps1"
$PublishScript = Join-Path $PSScriptRoot "publish-dashboard.ps1"
$DateContractScript = Join-Path $ProjectRoot "publisher\date_contract.py"
$SkillRoot = if ($env:TEENI_CONVERSATIONS_SKILL_ROOT) {
    $env:TEENI_CONVERSATIONS_SKILL_ROOT
} else {
    Join-Path $ProjectRoot ".agents\skills\analyze-teeni-conversations"
}
$AnalysisPython = if ($env:TEENI_ANALYSIS_PYTHON) {
    $env:TEENI_ANALYSIS_PYTHON
} else {
    Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$NodeModules = if ($env:TEENI_NODE_MODULES) {
    $env:TEENI_NODE_MODULES
} else {
    throw "TEENI_NODE_MODULES must point to the Codex workspace Node dependencies."
}
$NodeExe = (Get-Command node -ErrorAction Stop).Source

$CsvPath = Resolve-RequiredFile $Csv "CSV"
if ([System.IO.Path]::GetExtension($CsvPath) -ne ".csv") {
    throw "CSV must have a .csv extension: $CsvPath"
}

$ParsedDate = [DateTime]::MinValue
$ValidDate = [DateTime]::TryParseExact(
    $DataDate,
    "yyyy-MM-dd",
    [System.Globalization.CultureInfo]::InvariantCulture,
    [System.Globalization.DateTimeStyles]::None,
    [ref]$ParsedDate
)
if (-not $ValidDate) {
    throw "DataDate must use YYYY-MM-DD: $DataDate"
}
if ($ProductVersion -eq "M2" -and $ParsedDate.Date -lt [DateTime]::ParseExact(
    "2026-09-01",
    "yyyy-MM-dd",
    [System.Globalization.CultureInfo]::InvariantCulture
)) {
    throw "M2 dashboard history starts on 2026-09-01: $DataDate"
}

$NeedsProtectedConfig = [string]::IsNullOrWhiteSpace($env:TEENI_PUBLISH_TOKEN) -or (
    [string]::IsNullOrWhiteSpace($Url) -and
    [string]::IsNullOrWhiteSpace($env:TEENI_DASHBOARD_PUBLISH_URL)
)
if ($NeedsProtectedConfig -and (Test-Path -LiteralPath $SecretsFile -PathType Leaf)) {
    . $SecretsFile
}
if ([string]::IsNullOrWhiteSpace($env:TEENI_PUBLISH_TOKEN)) {
    throw "TEENI_PUBLISH_TOKEN is not set in the protected configuration."
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = $env:TEENI_DASHBOARD_PUBLISH_URL
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    throw "TEENI_DASHBOARD_PUBLISH_URL is not set in the protected configuration."
}

$PublishUri = $null
if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$PublishUri) -or
    $PublishUri.Scheme -notin @("http", "https")) {
    throw "Url must be an absolute HTTP or HTTPS URL."
}

$SkillRoot = Resolve-RequiredDirectory $SkillRoot "analyze-teeni-conversations Skill"
$AnalysisPython = Resolve-RequiredFile $AnalysisPython "Bundled analysis Python"
$NodeModules = Resolve-RequiredDirectory $NodeModules "Bundled Node modules"
$NodeExe = Resolve-RequiredFile $NodeExe "Bundled Node.js"
$PublishScript = Resolve-RequiredFile $PublishScript "Dashboard publisher"
$DateContractScript = Resolve-RequiredFile $DateContractScript "CSV date validator"

foreach ($ResourceName in @("scripts", "references", "vendor")) {
    Resolve-RequiredDirectory (Join-Path $SkillRoot $ResourceName) "Skill $ResourceName" | Out-Null
}

if ($OutputDir) {
    if (-not (Test-Path -LiteralPath $OutputDir)) {
        New-Item -ItemType Directory -Path $OutputDir | Out-Null
    }
    $ResolvedOutputDir = Resolve-RequiredDirectory $OutputDir "Output directory"
} else {
    $ResolvedOutputDir = Split-Path -Parent $CsvPath
}

& $AnalysisPython $DateContractScript --csv $CsvPath --date $DataDate --scene $SceneId
if ($LASTEXITCODE -ne 0) {
    throw "CSV data date validation failed. Analysis and publication were not started."
}

$TempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\", "/")
$RunName = "teeni-dashboard-link-$([Guid]::NewGuid().ToString('N'))"
$RunRoot = Join-Path $TempBase $RunName
$PreviousAnalysisPython = $env:TEENI_ANALYSIS_PYTHON
$PreviousNodeModules = $env:TEENI_NODE_MODULES
$PreviousPublishUrl = $env:TEENI_DASHBOARD_PUBLISH_URL

try {
    New-Item -ItemType Directory -Path $RunRoot | Out-Null
    foreach ($ResourceName in @("scripts", "references", "vendor")) {
        Copy-Item `
            -LiteralPath (Join-Path $SkillRoot $ResourceName) `
            -Destination (Join-Path $RunRoot $ResourceName) `
            -Recurse
    }
    New-Item -ItemType Junction -Path (Join-Path $RunRoot "node_modules") -Target $NodeModules | Out-Null

    $env:TEENI_ANALYSIS_PYTHON = $AnalysisPython
    $env:TEENI_NODE_MODULES = $NodeModules

    $AnalysisArgs = @(
        (Join-Path $RunRoot "scripts\analyze-shared.mjs"),
        "--primary", $CsvPath,
        "--primary-scene", $SceneId,
        "--data-date", $DataDate,
        "--primary-label", $ProductVersion,
        "--output-dir", $ResolvedOutputDir
    )
    $AnalysisOutput = @(& $NodeExe @AnalysisArgs 2>&1)
    $AnalysisExitCode = $LASTEXITCODE
    $AnalysisOutput | ForEach-Object { Write-Host $_ }
    if ($AnalysisExitCode -ne 0) {
        throw "Teeni base analysis failed with exit code $AnalysisExitCode."
    }

    $JsonLine = $AnalysisOutput |
        ForEach-Object { [string]$_ } |
        Where-Object { $_.TrimStart().StartsWith("{") } |
        Select-Object -Last 1
    if (-not $JsonLine) {
        throw "Teeni base analysis did not return its artifact manifest."
    }
    try {
        $Artifacts = $JsonLine | ConvertFrom-Json
    } catch {
        throw "Teeni base analysis returned invalid artifact JSON."
    }

    $Workbook = Resolve-RequiredFile $Artifacts.outputPath "Generated workbook"
    $PrimaryDetail = Resolve-RequiredFile $Artifacts.primaryDetailPath "Generated primary detail"
    $EndingDetail = Resolve-RequiredFile $Artifacts.endingDetailPath "Generated ending detail"
    $Manifest = Resolve-RequiredFile $Artifacts.manifestPath "Generated manifest"
    $SafetyWorkbook = Resolve-RequiredFile $Artifacts.safetyWorkbookPath "Generated safety review workbook"
    $SafetyExclusions = Resolve-RequiredFile $Artifacts.safetyExclusionsPath "Generated game exclusion evidence"
    $RenderDir = Join-Path $RunRoot "verification-renders"

    $VerifyArgs = @(
        (Join-Path $RunRoot "scripts\verify.mjs"),
        "--workbook", $Workbook,
        "--primary-detail", $PrimaryDetail,
        "--ending-detail", $EndingDetail,
        "--manifest", $Manifest,
        "--primary-scene", $SceneId,
        "--primary-source", $CsvPath,
        "--render-dir", $RenderDir
    )
    & $NodeExe @VerifyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Teeni base verification failed with exit code $LASTEXITCODE. The dashboard was not updated."
    }
    $WorkbookSha256 = (Get-FileHash -LiteralPath $Workbook -Algorithm SHA256).Hash.ToLowerInvariant()

    $OpeningDir = Join-Path $ResolvedOutputDir "opening-cohorts-$([Guid]::NewGuid().ToString('N'))"
    $OpeningArgs = @('-m', 'publisher.opening_daily', '--manifest', $Manifest,
        '--primary-source', $CsvPath, '--output-dir', $OpeningDir)
    if ($PreviousBaseManifest) { $OpeningArgs += @('--previous-manifest', $PreviousBaseManifest) }
    if ($PreviousPrimarySource) { $OpeningArgs += @('--previous-source', $PreviousPrimarySource) }
    Push-Location $ProjectRoot
    try {
        & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') @OpeningArgs
        if ($LASTEXITCODE -ne 0) { throw 'Opening cohort generation or verification failed. The dashboard was not updated.' }
    } finally { Pop-Location }

    $env:TEENI_DASHBOARD_PUBLISH_URL = $PublishUri.AbsoluteUri
    & $PublishScript -Workbook $Workbook -Manifest $Manifest -DataDate $DataDate -PrimarySource $CsvPath -OpeningPackage $OpeningDir
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard publication failed with exit code $LASTEXITCODE."
    }

    [pscustomobject]@{
        status = "published"
        productVersion = $ProductVersion
        sceneId = $SceneId
        dataDate = $DataDate
        workbook = $Workbook
        workbookSha256 = $WorkbookSha256
        manifest = $Manifest
        safetyWorkbook = $SafetyWorkbook
        safetyExclusions = $SafetyExclusions
        openingPackage = $OpeningDir
    } | ConvertTo-Json
} finally {
    $env:TEENI_ANALYSIS_PYTHON = $PreviousAnalysisPython
    $env:TEENI_NODE_MODULES = $PreviousNodeModules
    $env:TEENI_DASHBOARD_PUBLISH_URL = $PreviousPublishUrl

    if (Test-Path -LiteralPath $RunRoot) {
        $ResolvedRunRoot = [System.IO.Path]::GetFullPath($RunRoot)
        $ResolvedParent = Split-Path -Parent $ResolvedRunRoot
        $ResolvedName = Split-Path -Leaf $ResolvedRunRoot
        if ($ResolvedParent -ne $TempBase -or $ResolvedName -notmatch '^teeni-dashboard-link-[0-9a-f]{32}$') {
            throw "Refusing to remove unexpected temporary directory: $ResolvedRunRoot"
        }
        Remove-Item -LiteralPath $ResolvedRunRoot -Recurse -Force
    }
}
