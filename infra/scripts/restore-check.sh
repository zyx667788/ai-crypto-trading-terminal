#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/services/api"
python -c "from app.core.watchdog import watchdog_service; print(watchdog_service.verify_latest_backup_restore())"
