# Starts the backend and the frontend dev server in two windows.
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv (Join-Path $root ".venv")
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -r (Join-Path $root "backend\requirements.txt")
}

if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Push-Location (Join-Path $root "frontend")
    npm install
    Pop-Location
}

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$root\backend'; & '$python' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
)
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$root\frontend'; npm run dev"
)

Write-Host ""
Write-Host "Backend : http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "Frontend: http://127.0.0.1:5173" -ForegroundColor Green
