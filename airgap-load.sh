#!/usr/bin/env bash
# Load the on-prem image tarball into the local Docker daemon.
#
# Usage:
#   ./airgap-load.sh                              # auto-detect tarball in cwd
#   ./airgap-load.sh /path/to/memclaw-onprem-v1.0.0.tar.gz
#
# After this completes, `docker compose -f docker-compose.yml \
# -f docker-compose.airgap.yml up -d` will find every image locally
# under both of the image namespaces the bundle carries, plus the upstream
# bases. Every image is tagged under both -- the same image twice, not two
# images -- so this keeps working whichever spelling the compose files use, and
# flipping them needs no coordinated bundle change.

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
# BOTH namespaces, because bundles now carry every image under both and a
# listing that matches only the older one shows half of what was loaded. The
# operator reading it is mid-offline-upgrade with no registry to fall back on:
# "half my images are missing" is a reasonable thing to conclude from that and
# a bad thing to act on. Nothing functional depends on this listing -- compose
# resolves tags, not this -- which is exactly why it has to be right.
#
# The pattern is a variable rather than inline so that this line can carry the
# annotation the naming gate requires; a pattern inside the pipeline below ends
# in a continuation and has nowhere to put a trailing comment.
expected='^(caura-onprem/|memclaw-onprem/|pgvector/pgvector|redis|rabbitmq)'  # legacy-name-ok: keeps the previous image namespace listed beside the new one, for hosts whose compose files have not moved yet, which rule 3 keeps working
# ``|| true`` because of ``pipefail``: grep exits 1 when it matches nothing,
# which would fail this script AFTER a successful load with no output to say
# why. That silent exit is replaced by the named check below.
loaded=$(docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E "$expected" \
  | sort || true)
if [ -z "$loaded" ]; then
  echo "ERROR: docker load reported success but no expected image is present." >&2
  echo "Nothing under either on-prem image namespace, and none of the upstream bases." >&2
  echo "The tarball loaded something other than an on-prem bundle. Do not run compose." >&2
  exit 1
fi
printf '%s\n' "$loaded"

echo ""
echo "==> Ready. Next:"
echo "    1. cp .env.example .env && edit"
echo "    2. drop your license.key into ./license/"
echo "    3. docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d"
