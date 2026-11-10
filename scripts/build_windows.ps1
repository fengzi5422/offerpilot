param([switch]$Installer)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location frontend
try {
    & npm ci --registry=https://registry.npmjs.org
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npm run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }
& uv run pyinstaller --noconfirm packaging/offerpilot.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Installer) {
    $Compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $Compiler) { throw '请安装 Inno Setup 并将 ISCC.exe 加入 PATH，然后重试 -Installer。' }
    & $Compiler.Source packaging/OfferPilot.iss
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
