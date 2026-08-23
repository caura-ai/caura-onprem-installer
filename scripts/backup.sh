#!/usr/bin/env bash
# MemClaw on-prem backup script.
#
# Dumps Postgres (pg_dump -Fc), snapshots Redis (BGSAVE), and optionally
# exports RabbitMQ definitions. Bundles everything + the .env +
# license.key into a tar.gz under $BACKUP_DIR.
#
# Usage:
#   ./backup.sh               # defaults: $MEMCLAW_HOME/backups/<ts>.tar.gz
#   ./backup.sh /some/path
#
# Safe to run while the stack is live — pg_dump uses a consistent snapshot.
set -euo pipefail

# The install root, under either spelling. The old name resolves first (the
# line below is byte-identical to what it has always been, and pinned by
# scripts/do_not_touch_sentinel.py because a flipped default sends backups to an
# empty directory and reports success); CAURA_HOME then overrides it when set to
# something non-empty. Blank never wins — on this script that would mean backing
# up "/backups" instead of the customer's install.
MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
BACKUP_DIR="${1:-$MEMCLAW_HOME/backups}"
mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$MEMCLAW_HOME"

echo "==> Postgres dump"
docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-memclaw}" -Fc "${POSTGRES_DB:-memclaw}" > "$WORK/postgres.dump"

echo "==> Redis BGSAVE"
docker compose exec -T redis redis-cli BGSAVE >/dev/null
# Wait for BGSAVE to finish
for _ in $(seq 1 60); do
  status=$(docker compose exec -T redis redis-cli LASTSAVE)
  sleep 1
  newstatus=$(docker compose exec -T redis redis-cli LASTSAVE)
  [ "$status" != "$newstatus" ] && break
done
docker compose cp redis:/data/dump.rdb "$WORK/redis.rdb"

echo "==> RabbitMQ definitions"
docker compose exec -T rabbitmq rabbitmqctl export_definitions /tmp/defs.json >/dev/null 2>&1 || true
docker compose cp rabbitmq:/tmp/defs.json "$WORK/rabbitmq-defs.json" 2>/dev/null || true

echo "==> Bundle config + license"
cp -a .env "$WORK/.env" 2>/dev/null || true
cp -a license "$WORK/license" 2>/dev/null || true

TARBALL="$BACKUP_DIR/memclaw-backup-$STAMP.tar.gz"
tar -C "$WORK" -czf "$TARBALL" .
echo "==> Backup written: $TARBALL"
