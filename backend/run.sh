#!/usr/bin/env bash
# Start the FitCheck API on 0.0.0.0:${PORT:-8000}. Usage: ./run.sh            (foreground)
#                                                        ./run.sh --bg       (nohup, logs to server.log, pid in server.pid)
# Stop a background server with ./stop.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -qE "[:.]${PORT}\b"; then
  echo "Port ${PORT} is already in use; set PORT=... to use another one." >&2
  exit 1
fi
[ -f ../.env ] && set -a && . ../.env && set +a
[ -f .env ] && set -a && . ./.env && set +a
export HF_HOME="${HF_HOME:-$PWD/models/hf}"
export TOKENIZERS_PARALLELISM=false
CMD=(.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1)
if [ "${1:-}" = "--bg" ]; then
  nohup "${CMD[@]}" >> server.log 2>&1 &
  echo $! > server.pid
  echo "FitCheck API started in background (pid $!) on :$PORT, logging to $(pwd)/server.log"
else
  exec "${CMD[@]}"
fi
