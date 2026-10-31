#!/usr/bin/env bash
set -euo pipefail

echo "=== Health Liveness (/health/live) ==="
curl -s http://127.0.0.1:8000/health/live || echo "API offline"
echo ""
echo "=== Health Readiness (/health/ready) ==="
curl -s http://127.0.0.1:8000/health/ready || echo "API offline"
echo ""
echo "=== Internal Metrics (/metrics) ==="
curl -s http://127.0.0.1:8000/metrics || echo "API offline"
echo ""
