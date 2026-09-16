param(
    [string]$PythonExe = '',
    [switch]$InstallDependencies
)

$ErrorActionPreference = 'Stop'
$buildRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$appName = '坐标生成器v22.8'
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $buildRoot 'dist'))
$releaseFolder = [System.IO.Path]::GetFullPath((Join-Path $distRoot $appName))
if (-not $releaseFolder.StartsWith($distRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output path must stay inside this project dist directory.'
}
if (-not $PythonExe) {
    $localPython = Join-Path $buildRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython) {
        $PythonExe = $localPython
    } else {
        $PythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python interpreter not found: $PythonExe"
}

Push-Location -LiteralPath $buildRoot
try {
    if ($InstallDependencies) {
        & $PythonExe -m pip install -r (Join-Path $buildRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
    & $PythonExe -m PyInstaller coordinate_v22.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed. Source files were not deleted.' }
    $releaseExe = Join-Path $releaseFolder ($appName + '.exe')
    if (-not (Test-Path -LiteralPath $releaseExe)) { throw 'Expected executable is missing.' }
    $releaseFolder = Split-Path -Parent $releaseExe
    $licenseStage = Join-Path $buildRoot ('build\licenses-' + [guid]::NewGuid().ToString('N'))
    & $PythonExe (Join-Path $buildRoot 'scripts\collect_licenses.py') --output $licenseStage
    if ($LASTEXITCODE -ne 0) { throw 'Third-party license collection failed.' }
    $licenseTarget = Join-Path $releaseFolder 'licenses'
    New-Item -ItemType Directory -Path $licenseTarget -Force | Out-Null
    Get-ChildItem -LiteralPath $licenseStage -Force | Copy-Item -Destination $licenseTarget -Recurse -Force
    foreach ($document in @('LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'CONTRIBUTING.md', 'CHANGELOG.md')) {
        Copy-Item -LiteralPath (Join-Path $buildRoot $document) -Destination (Join-Path $releaseFolder $document) -Force
    }
    $docsTarget = Join-Path $releaseFolder 'docs'
    New-Item -ItemType Directory -Path $docsTarget -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $buildRoot 'docs') -File | Copy-Item -Destination $docsTarget -Force
    $releaseStats = Get-ChildItem -LiteralPath $releaseFolder -Recurse -File | Measure-Object -Property Length -Sum
    Write-Host ('Built: ' + $releaseExe)
    Write-Host ('Files: {0}; size: {1:N1} MiB' -f $releaseStats.Count, ($releaseStats.Sum / 1MB))
    Write-Host ('Executable SHA-256: ' + (Get-FileHash -LiteralPath $releaseExe -Algorithm SHA256).Hash)
    Write-Host 'Keep the executable and its _internal folder together. Python is bundled.'
    Write-Host 'Runtime diagnostics: %LOCALAPPDATA%\CoordinateGeneratorV22\logs\startup.log'
} finally {
    Pop-Location
}
