#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "[ERROR] Refusing to start trading services as root."
  exit 1
fi

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICES=(
  "com.aicrypto.postgres"
  "com.aicrypto.redis"
  "com.aicrypto.api"
  "com.aicrypto.web"
  "com.aicrypto.market-worker"
  "com.aicrypto.execution-worker"
  "com.aicrypto.recovery-watchdog"
)

if command -v launchctl >/dev/null 2>&1; then
  for svc in "${SERVICES[@]}"; do
    plist="$LAUNCH_AGENTS_DIR/${svc}.plist"
    if [ -f "$plist" ]; then
      launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || true
      launchctl kickstart -k "gui/$(id -u)/${svc}" 2>/dev/null || true
      echo "[start] Bootstrapped ${svc}"
    fi
  done
else
  echo "[start] Non-macOS host detected; starting via ./infra/scripts/dev.sh"
  exec "$(dirname "${BASH_SOURCE[0]}")/dev.sh"
fi
