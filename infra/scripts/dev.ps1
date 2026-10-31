$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RootDir

if (-not (Test-Path ".env")) {
    Write-Host "[dev] Creating .env from .env.example..."
    Copy-Item ".env.example" ".env"
}

Write-Host "[dev] Running Alembic database migrations..."
Push-Location "services/api"
python -m alembic upgrade head
Pop-Location

Write-Host "[dev] Ready! Start API on http://127.0.0.1:8000 and Web on http://localhost:3000"
