#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "[dev] Creating .env from .env.example..."
  cp .env.example .env
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "[dev] Starting PostgreSQL and Redis containers..."
  docker compose up -d postgres redis
else
  echo "[dev] Docker not running; using local persistent SQLite + in-process Redis adapter."
fi

echo "[dev] Running Alembic migrations..."
(cd services/api && alembic upgrade head)

echo "[dev] Starting FastAPI backend on http://127.0.0.1:8000 ..."
(cd services/api && uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload) &
API_PID=$!

echo "[dev] Starting Next.js frontend on http://localhost:3000 ..."
(cd apps/web && npm run dev) &
WEB_PID=$!

trap 'kill $API_PID $WEB_PID 2>/dev/null || true' EXIT INT TERM
wait
