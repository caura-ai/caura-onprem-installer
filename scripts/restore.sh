#!/usr/bin/env bash
# Caura on-prem restore script — pair to backup.sh.
#
# Usage:
#   ./restore.sh /path/to/memclaw-backup-<ts>.tar.gz
#
# Restores Postgres (pg_restore --clean), Redis (RDB copy + restart), and
# optionally RabbitMQ definitions. Does NOT overwrite .env or license.key
# unless --replace-config is passed.
set -euo pipefail

# The install root, under either spelling — old name first, CAURA_HOME
# overriding only when non-empty. See scripts/backup.sh for the full note.
MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
REPLACE_CONFIG="false"

if [[ "${1:-}" == "--replace-config" ]]; then
  REPLACE_CONFIG="true"
  shift
fi

TARBALL="${1:-}"
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
  echo "Usage: $0 [--replace-config] <backup.tar.gz>" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xzf "$TARBALL"

cd "$MEMCLAW_HOME"

echo "==> Replacing Postgres contents (pg_restore --clean --if-exists)"
docker compose exec -T postgres \
  pg_restore --clean --if-exists -U "${POSTGRES_USER:-memclaw}" \
    -d "${POSTGRES_DB:-memclaw}" < "$WORK/postgres.dump"

if [ -f "$WORK/redis.rdb" ]; then
  echo "==> Restoring Redis RDB"
  docker compose stop redis
  docker compose cp "$WORK/redis.rdb" redis:/data/dump.rdb
  docker compose start redis
fi

if [ -f "$WORK/rabbitmq-defs.json" ]; then
  echo "==> Importing RabbitMQ definitions"
  docker compose cp "$WORK/rabbitmq-defs.json" rabbitmq:/tmp/defs.json
  docker compose exec -T rabbitmq rabbitmqctl import_definitions /tmp/defs.json || true
fi

if [ "$REPLACE_CONFIG" = "true" ]; then
  echo "==> Replacing .env + license"
  cp -a "$WORK/.env" .env 2>/dev/null || true
  cp -a "$WORK/license/." license/ 2>/dev/null || true
  echo "    Restart the stack for config changes to take effect."
fi

echo "==> Restore complete."
