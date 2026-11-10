param([switch]$Installer, [string]$CompilerPath)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$BuildRoot = Join-Path (Get-Location) ('dist/build-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $BuildRoot | Out-Null
& uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location frontend
try {
    & npm.cmd ci --registry=https://registry.npmjs.org
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }
& uv run --frozen pyinstaller --noconfirm --distpath $BuildRoot packaging/offerpilot.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Installer) {
    if (-not $CompilerPath) {
        $Compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($Compiler) { $CompilerPath = $Compiler.Source }
        elseif (Test-Path '.tool-cache/inno/compiler/ISCC.exe') { $CompilerPath = '.tool-cache/inno/compiler/ISCC.exe' }
    }
    if (-not $CompilerPath -or -not (Test-Path -LiteralPath $CompilerPath)) { throw '请安装 Inno Setup，使用 -CompilerPath 指定 ISCC.exe，然后重试 -Installer。' }
    & $CompilerPath ("/DProgramSource=" + (Join-Path $BuildRoot 'OfferPilot')) ("/O" + $BuildRoot) packaging/OfferPilot.iss
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $ReleaseVersion = & uv run --frozen python -c "import sys; sys.path.insert(0,'backend'); from app.version import VERSION; print(VERSION)"
    $ReleaseTarget = Join-Path (Join-Path (Get-Location) 'dist/releases') $ReleaseVersion
    if (Test-Path -LiteralPath $ReleaseTarget) { throw '此版本已归档，请更新版本号；不会覆盖旧包。' }
    & uv run --frozen python scripts/archive_release.py --dist $BuildRoot --version $ReleaseVersion
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReleaseTarget) | Out-Null
    Move-Item -LiteralPath (Join-Path $BuildRoot ('releases/' + $ReleaseVersion)) -Destination $ReleaseTarget
}
Write-Output ("程序构建已保存在独立目录：" + $BuildRoot)
