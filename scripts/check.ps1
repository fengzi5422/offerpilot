$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& uv run --frozen pytest backend/tests scripts/test_runtime.py scripts/test_desktop.py scripts/test_release.py -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& uv run --frozen ruff check backend scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location frontend
try {
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }
