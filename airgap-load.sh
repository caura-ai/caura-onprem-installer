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
# flipping them needs no coordinated bundle change. The gateway is the
# exception: it is built on this host from nginx/, offline, from a base image
# the tarball carries from the release after v2.13.0.

set -euo pipefail

# The images a Dockerfile builds FROM, one per line: the first argument after
# FROM that is not a --flag, skipping `scratch` and earlier stages. install.sh
# and upgrade.sh carry the same function, and the release reads nginx/Dockerfile
# by the same rule to put the gateway's base in the air-gap tarball.
gateway_bases() {
  awk 'toupper($1) == "FROM" {
      img = ""
      for (i = 2; i <= NF; i++) if ($i !~ /^--/) { img = $i; break }
      if (img != "" && img != "scratch" && !(img in stage) && !(img in seen)) { print img; seen[img] = 1 }
      for (j = i + 1; j < NF; j++) if (toupper($j) == "AS") stage[$(j + 1)] = 1
    }' "$1"
}

TARBALL="${1:-}"
if [ -z "$TARBALL" ]; then
  TARBALL=$(ls -1 memclaw-onprem-*.tar.gz 2>/dev/null | head -n1 || true)
fi
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
  echo "ERROR: tarball not found." >&2
  echo "Usage: $0 [path/to/memclaw-onprem-<version>.tar.gz]" >&2
  exit 1
fi

# Gateway tags and the image each points at, one "tag id" pair per line.
gateway_tags() {
  docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
    | grep -E '^[a-z]+-onprem/gateway:' || true
}

gateway_before=$(gateway_tags)
echo "==> Loading images from $TARBALL (this can take a few minutes)"
gunzip -c "$TARBALL" | docker load

# The gateway is built on this host from nginx/, never loaded. Tarballs up to
# v2.13.0 carried a different gateway image under the very tag that build
# produces, and a loaded image is one `docker compose up` runs instead of
# building: it cannot route an on-prem stack and restarts forever. So drop
# whatever gateway tag THIS load set: one that is new, or now points at a
# different image. A gateway built here under another version's tag is
# untouched by the load and left alone.
gateway_loaded=$(comm -13 <(printf '%s\n' "$gateway_before" | sort) \
                          <(gateway_tags | sort) | awk 'NF { print $1 }')
for ref in $gateway_loaded; do
  if docker image rm "$ref" >/dev/null 2>&1; then
    echo "==> Removed $ref: the tarball's gateway, which this host builds itself"
  else
    echo "WARNING: could not remove $ref. Build the gateway before starting" >&2
    echo "         the stack (step 3 below), or it will run instead." >&2
  fi
done

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
expected='^(caura-onprem/|memclaw-onprem/|pgvector/pgvector|redis|rabbitmq|nginx)'  # legacy-name-ok: keeps the previous image namespace listed beside the new one, for hosts whose compose files have not moved yet, which rule 3 keeps working
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

# The gateway build needs its base image, and offline it cannot be pulled.
# Tarballs from the release after v2.13.0 carry it; older ones do not.
dockerfile="$(cd "$(dirname "$0")" && pwd)/nginx/Dockerfile"
if [ -f "$dockerfile" ]; then
  for base in $(gateway_bases "$dockerfile"); do
    docker image inspect "$base" >/dev/null 2>&1 && continue
    echo "" >&2
    echo "WARNING: the gateway is built on this host and needs $base, which" >&2
    echo "this tarball does not carry. On a machine with internet access:" >&2
    echo "    docker pull --platform linux/amd64 $base" >&2
    echo "    docker save $base | gzip > gateway-base.tar.gz" >&2
    echo "Copy gateway-base.tar.gz here and run:" >&2
    echo "    gunzip -c gateway-base.tar.gz | docker load" >&2
  done
fi

echo ""
echo "==> Ready. Next:"
echo "    1. cp .env.example .env && edit"
echo "    2. drop your license.key into ./license/"
echo "    3. docker compose -f docker-compose.yml -f docker-compose.airgap.yml build --no-cache gateway"
echo "    4. docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d"
echo "    Those are for a new install. To upgrade one that is running, run instead:"
echo "    sudo bash ./upgrade.sh --offline --bundle <bundle.tar.gz> --to <version>"
echo "    (docs/upgrade.md, Air-gap upgrade)"
