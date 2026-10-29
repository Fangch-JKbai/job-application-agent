#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked
if [ ! -f .env ]; then
  docker pull postgres:16
  .venv/bin/python scripts/setup_dev_db.py --image postgres:16
fi
.venv/bin/python scripts/configure_delivery.py
docker compose --env-file .env up -d --pull never --wait
.venv/bin/python -m database.migrate
(cd web && npm ci --prefer-offline && npm run build)
.venv/bin/python -m services.worker &
worker_pid=$!
.venv/bin/python -m services.mail_scheduler &
scheduler_pid=$!
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port "${APP_PORT:-8001}" --workers 1 --no-proxy-headers &
web_pid=$!
cleanup() {
  kill "$web_pid" "$worker_pid" "$scheduler_pid" 2>/dev/null || true
  wait "$web_pid" 2>/dev/null || true
  wait "$worker_pid" 2>/dev/null || true
  wait "$scheduler_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Bash 3.2 on macOS has no wait -n; detect either child exiting.
while kill -0 "$web_pid" 2>/dev/null && kill -0 "$worker_pid" 2>/dev/null && kill -0 "$scheduler_pid" 2>/dev/null; do sleep 1; done
exit 1
