#!/usr/bin/env bash
# Stop FitCheck on the box (data volumes are kept). Pass --tunnel to stop the quick tunnel too.
cd "$(dirname "$0")/.."
export PATH=/workspace/dockerbin/docker:/workspace/dockerbin:$PATH
sudo env PATH=$PATH docker compose -f docker-compose.yml -f docker-compose.box.yml stop
if [ "${1:-}" = "--tunnel" ] && [ -s /workspace/fitcheck-run/tunnel.pid ]; then
  kill "$(cat /workspace/fitcheck-run/tunnel.pid)" 2>/dev/null; rm -f /workspace/fitcheck-run/tunnel.pid
fi
