#!/usr/bin/env bash
# End-to-end smoke test for a connected on-prem install.
# Run after install.sh completes. Exits non-zero on any failure.
set -euo pipefail

HOST="${PUBLIC_HOSTNAME:-caura.local}"
BASE="${BASE_URL:-https://$HOST}"
ADMIN_JWT="${ADMIN_JWT:-}"

echo "==> 1. /api/version responds"
curl -fsS "$BASE/api/version" | grep -q '"version"' || { echo "FAIL"; exit 1; }

echo "==> 2. /api/license/status reports configured=true"
if [ -n "$ADMIN_JWT" ]; then
  curl -fsS "$BASE/api/license/status" -H "Authorization: Bearer $ADMIN_JWT" | grep -q '"configured":true'
fi

echo "==> 3. health endpoints 200"
for svc in platform-admin-api platform-auth-api platform-storage-api core-api; do
  docker compose exec -T "$svc" python -c "import urllib.request; urllib.request.urlopen('http://localhost/healthz')" \
    || { echo "FAIL $svc"; exit 1; }
done

echo "==> 4. all compose services running"
docker compose ps --services --filter status=running | wc -l | grep -q -v '^0$' \
  || { echo "FAIL: no services running"; exit 1; }

echo "==> all checks passed."
