#!/usr/bin/env bash
# Stop the background FitCheck API started by ./run.sh --bg
cd "$(dirname "$0")"
if [ -f server.pid ] && kill -0 "$(cat server.pid)" 2>/dev/null; then
  kill "$(cat server.pid)" && echo "stopped $(cat server.pid)"; rm -f server.pid
else
  echo "no running server.pid"; rm -f server.pid
fi
