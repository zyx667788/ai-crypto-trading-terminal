#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "[ERROR] Do NOT install or run trading LaunchAgents as root (Section 9.4.1 / 9.4.4)."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR" "$ROOT_DIR/logs"

SERVICES=(
  "com.aicrypto.postgres"
  "com.aicrypto.redis"
  "com.aicrypto.api"
  "com.aicrypto.web"
  "com.aicrypto.market-worker"
  "com.aicrypto.execution-worker"
  "com.aicrypto.recovery-watchdog"
)

for svc in "${SERVICES[@]}"; do
  src="$ROOT_DIR/infra/launchd/${svc}.plist"
  dst="$LAUNCH_AGENTS_DIR/${svc}.plist"
  sed "s|__PROJECT_ROOT__|${ROOT_DIR}|g" "$src" > "$dst"
  echo "[install-services] Installed $dst"
done

echo "[install-services] Done. Use 'make start' to bootstrap LaunchAgents."
