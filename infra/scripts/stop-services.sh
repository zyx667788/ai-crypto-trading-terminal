#!/usr/bin/env bash
set -euo pipefail

# Section 9.4.9: make stop MUST NOT auto-cancel orders or auto-flatten positions.
# It pauses new entries first, checks protective orders, and informs the operator.

API_URL="${API_URL:-http://127.0.0.1:8000/api/v1}"

echo "[stop] Step 1: Pausing new entries via automation pause..."
curl -s -X POST "${API_URL}/automation/pause" -H "Content-Type: application/json" || true
echo ""

echo "[stop] Step 2: Checking open positions and exchange-native protective orders..."
curl -s "${API_URL}/positions" || true
echo ""
echo "[NOTICE] Open positions are NOT automatically closed on service stop. Exchange-native TP/SL remain active on exchange."

if command -v launchctl >/dev/null 2>&1; then
  LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
  for svc in com.aicrypto.recovery-watchdog com.aicrypto.market-worker com.aicrypto.execution-worker com.aicrypto.web com.aicrypto.api; do
    launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENTS_DIR/${svc}.plist" 2>/dev/null || true
    echo "[stop] Stopped ${svc}"
  done
fi
