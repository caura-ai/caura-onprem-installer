#!/usr/bin/env bash
# Caura on-prem restore script — pair to backup.sh.
#
# Usage:
#   ./restore.sh /path/to/memclaw-backup-<ts>.tar.gz
#
# Restores Postgres (pg_restore --clean) and Redis (RDB copy + restart).
# Does NOT overwrite .env or license.key
# unless --replace-config is passed.
set -euo pipefail

# The install root, under either spelling — old name first, CAURA_HOME
# overriding only when non-empty. See scripts/backup.sh for the full note.
CAURA_HOME="${CAURA_HOME:-${MEMCLAW_HOME:-/opt/memclaw}}"  # legacy-name-floor: floor, and the install root default — unchanged for existing installs
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

cd "$CAURA_HOME"

# Refuse before changing databases: a restored tree may lack the separately
# provisioned proof required by the current bundle. Older bundles are unchanged.
if [ -f docker-compose.yml ] && grep -q 'SETUP_TOKEN_FILE:' docker-compose.yml && \
    { [ ! -f setup-token ] || [ -L setup-token ]; }; then
  echo "Host setup-token must be a regular file before restore. Prepare it using docs/troubleshooting.md#missing-setup-token-bind-source, then rerun restore. No database changes were made." >&2
  exit 2
fi

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
  echo "==> Ignoring legacy RabbitMQ definitions (broker retired; archive preserved)"
fi

if [ "$REPLACE_CONFIG" = "true" ]; then
  echo "==> Replacing .env + license"
  cp -a "$WORK/.env" .env 2>/dev/null || true
  cp -a "$WORK/license/." license/ 2>/dev/null || true
  echo "    Restart the stack for config changes to take effect."
fi

echo "==> Restore complete."
