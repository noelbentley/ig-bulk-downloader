param([string]$OutputDirectory = 'dist')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install requirements-desktop-lock.txt first; see DESKTOP.md.'
}
$resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
if (-not $resolvedOutput.StartsWith($projectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output must be inside this project.'
}
$targetExe = Join-Path $resolvedOutput 'IG-Bulk-Downloader.exe'
$runningTarget = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and [string]::Equals($_.ExecutablePath, $targetExe, [System.StringComparison]::OrdinalIgnoreCase)
}
if ($runningTarget) { throw 'The target executable is running. Use -OutputDirectory with an unused subfolder under dist to build separately.' }
& $pythonPath -m PyInstaller --clean --noconfirm --distpath $resolvedOutput IG-Bulk-Downloader.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
Write-Host "Built $targetExe"
