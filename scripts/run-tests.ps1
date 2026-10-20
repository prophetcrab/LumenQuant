$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"

Push-Location (Join-Path $root "backend")
Write-Host "== Unit + integration + system ==" -ForegroundColor Cyan
& $python -m pytest -q
Write-Host "== Frontend typecheck and build ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "frontend")
npx tsc --noEmit
npx vite build
Pop-Location
Pop-Location
