#!/usr/bin/env bash
# Load the on-prem image tarball into the local Docker daemon.
#
# Usage:
#   ./airgap-load.sh                              # auto-detect tarball in cwd
#   ./airgap-load.sh /path/to/memclaw-onprem-v1.0.0.tar.gz
#
# After this completes, `docker compose -f docker-compose.yml \
# -f docker-compose.airgap.yml up -d` will find every image locally
# under the memclaw-onprem/* namespace + the upstream bases.

set -euo pipefail

TARBALL="${1:-}"
if [ -z "$TARBALL" ]; then
  TARBALL=$(ls -1 memclaw-onprem-*.tar.gz 2>/dev/null | head -n1 || true)
fi
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
  echo "ERROR: tarball not found." >&2
  echo "Usage: $0 [path/to/memclaw-onprem-<version>.tar.gz]" >&2
  exit 1
fi

echo "==> Loading images from $TARBALL (this can take a few minutes)"
gunzip -c "$TARBALL" | docker load

echo ""
echo "==> Loaded images:"
docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E '^(memclaw-onprem/|pgvector/pgvector|redis|rabbitmq)' \
  | sort

echo ""
echo "==> Ready. Next:"
echo "    1. cp .env.example .env && edit"
echo "    2. drop your license.key into ./license/"
echo "    3. docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d"
