param([switch]$NoBrowser, [switch]$NoTray)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location (Join-Path $ProjectRoot 'frontend')
try {
    if (-not (Test-Path 'node_modules')) { & npm ci --registry=https://registry.npmjs.org }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npm run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }
$LaunchArgs = @('run', '--frozen', 'python', 'scripts/launcher.py', '--data-dir', (Join-Path $ProjectRoot '.runtime/dev'))
if ($NoBrowser) { $LaunchArgs += '--no-browser' }
if ($NoTray) { $LaunchArgs += '--no-tray' }
& uv @LaunchArgs
exit $LASTEXITCODE
