.PHONY: help setup infra-up infra-down migrate api web dev test format \
	install-services start stop status logs backup restore-check reconcile kill-switch

help:
	@echo "AI Crypto Trading Terminal (V1.1) - Commands:"
	@echo ""
	@echo "  Development"
	@echo "    make setup            - Initialize .env and install backend/frontend dependencies"
	@echo "    make infra-up         - Start PostgreSQL and Redis via Docker Compose (dev/test only)"
	@echo "    make infra-down       - Stop Docker Compose services"
	@echo "    make migrate          - Run Alembic database migrations"
	@echo "    make api              - Start FastAPI backend on 127.0.0.1:8000"
	@echo "    make web              - Start Next.js frontend on localhost:3000"
	@echo "    make dev              - Run full local dev stack via ./infra/scripts/dev.sh"
	@echo "    make test             - Run the backend Pytest suite"
	@echo ""
	@echo "  macOS 24h residence (launchd)"
	@echo "    make install-services - Render and install the 7 launchd plists into ~/Library/LaunchAgents"
	@echo "    make start            - Bootstrap/start the supervised services"
	@echo "    make stop             - Stop services (does NOT cancel orders or flatten positions)"
	@echo "    make status           - Print /health/live, /health/ready and /metrics"
	@echo "    make logs             - Tail the API and worker logs"
	@echo ""
	@echo "  Operations"
	@echo "    make backup           - Create an encrypted, secret-free database backup"
	@echo "    make restore-check    - Verify the latest backup restores in an isolated reader"
	@echo "    make reconcile        - Trigger a full exchange reconciliation"
	@echo "    make kill-switch      - Activate the global kill switch (blocks all new entries)"

setup:
	@[ -f .env ] || cp .env.example .env
	cd services/api && python -m pip install -e ".[dev]"
	cd apps/web && npm install

infra-up:
	docker compose up -d postgres redis

infra-down:
	docker compose stop postgres redis

migrate:
	cd services/api && python -m alembic upgrade head

api:
	cd services/api && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

web:
	cd apps/web && npm run dev

dev:
	./infra/scripts/dev.sh

test:
	cd services/api && python -m pytest -v

# --------------------------------------------------------------- launchd
install-services:
	./infra/scripts/install-services.sh

start:
	./infra/scripts/start-services.sh

# Section 9.4.9: `make stop` must never cancel orders or flatten positions.
stop:
	./infra/scripts/stop-services.sh

status:
	./infra/scripts/status.sh

logs:
	@tail -n 50 logs/api.out.log logs/api.err.log 2>/dev/null || echo "No API logs yet."

# -------------------------------------------------------------- operations
backup:
	./infra/scripts/backup.sh

restore-check:
	./infra/scripts/restore-check.sh

pg-check:
	@echo "Starting embedded PostgreSQL and running the full PostgreSQL integration check..."
	python infra/scripts/pg_integration_check.py

audit-secrets:
	python infra/scripts/audit-secret-leak.py

reconcile:
	@curl -s -X POST http://127.0.0.1:8000/api/v1/portfolio/reconcile | python -m json.tool

kill-switch:
	@curl -s -X POST http://127.0.0.1:8000/api/v1/risk/kill-switch \
		-H "Content-Type: application/json" \
		-d '{"scope":"global","reason":"Manual operator emergency stop via make kill-switch"}' \
		| python -m json.tool
