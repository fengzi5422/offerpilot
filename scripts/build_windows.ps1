param([switch]$Installer, [string]$CompilerPath)
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
& uv run --frozen pyinstaller --noconfirm packaging/offerpilot.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Installer) {
    if (-not $CompilerPath) {
        $Compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($Compiler) { $CompilerPath = $Compiler.Source }
        elseif (Test-Path '.tool-cache/inno/compiler/ISCC.exe') { $CompilerPath = '.tool-cache/inno/compiler/ISCC.exe' }
    }
    if (-not $CompilerPath -or -not (Test-Path -LiteralPath $CompilerPath)) { throw '请安装 Inno Setup，使用 -CompilerPath 指定 ISCC.exe，然后重试 -Installer。' }
    & $CompilerPath packaging/OfferPilot.iss
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $ReleaseVersion = & uv run --frozen python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
    & uv run --frozen python scripts/archive_release.py --version $ReleaseVersion
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
