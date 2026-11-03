#!/bin/sh
# Migrate the database, then serve. Alembic takes a Postgres advisory lock, so several replicas can start at once.
set -e
alembic upgrade head
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --workers 1 \
  --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
