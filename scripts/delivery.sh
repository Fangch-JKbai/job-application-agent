#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Deliberately retain the database volume and secrets on stop.
compose=(docker compose --env-file .env -f compose.delivery.yaml)
case "${1:-status}" in
  up) "${compose[@]}" up -d --build --wait ;;
  stop) "${compose[@]}" stop app scheduler
        "${compose[@]}" stop worker ;;
  status) "${compose[@]}" ps ;;
  logs) "${compose[@]}" logs --tail 100 "${@:2}" ;;
  *) echo '用法: bash scripts/delivery.sh up|stop|status|logs [service]' >&2; exit 2 ;;
esac
