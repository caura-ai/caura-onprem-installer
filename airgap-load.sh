#!/usr/bin/env bash
# Load the on-prem image tarball into the local Docker daemon.
#
# Usage:
#   ./airgap-load.sh                              # auto-detect tarball in cwd
#   ./airgap-load.sh /path/to/memclaw-onprem-v1.0.0.tar.gz
#   ./airgap-load.sh --skip-verify <tarball>      # load WITHOUT checking it
#
# The tarball's <name>.sha256 and <name>.sha256.sigstore.json, published beside
# it, must sit beside it here too. Nothing is loaded until cosign has proved
# that the on-prem release signed that checksum and the tarball matches it.
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

# THE RELEASE THAT BUILT THE TARBALL, proved before anything is loaded. Each
# on-prem release signs its tarball's checksum keyless: the signing
# certificate names caura-enterprise's release workflow at the version's
# onprem-v* tag, and the signature is entered in Sigstore's public
# transparency log, whose inclusion proof the bundle carries. The checksum and
# its bundle are published beside the tarball, on the same bucket, so neither
# is trusted for what it says about itself. The identity is fixed here and
# completed only with the version being loaded -- an exact match, never a
# pattern, or an older release's signed tarball would pass as a newer one --
# and the trusted root is the one this installer ships. Nothing is fetched: an
# air-gapped host has nothing to fetch from.
RELEASE_IDENTITY="https://github.com/caura-ai/caura-enterprise/.github/workflows/release-onprem.yml@refs/tags/onprem-"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
TRUSTED_ROOT="$(cd "$(dirname "$0")" && pwd)/scripts/sigstore/trusted_root.json"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

verify_release() {
  local tarball="$1" name version checksum bundle published expected f
  local named='^[a-z]+-onprem-(v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9._-]+)?)\.tar\.gz$'
  name=$(basename "$tarball")
  # The published name is the only place the version comes from, so a renamed
  # tarball cannot be matched to the release that built it.
  [[ "$name" =~ $named ]] \
    || die "$name does not name a release version. Keep the name it was published under: <prefix>-onprem-<version>.tar.gz."
  version="${BASH_REMATCH[1]}"
  checksum="${tarball}.sha256"
  bundle="${checksum}.sigstore.json"
  published="https://onprem.caura.ai/releases/${version}/"
  for f in "$checksum" "$bundle"; do
    [ -f "$f" ] \
      || die "$(basename "$f") is not beside the tarball. Download it from ${published} with the tarball and keep the three files together. A tarball from before signed releases has neither file: if you have verified it some other way, run this again with --skip-verify. Nothing was loaded."
  done
  command -v cosign >/dev/null 2>&1 \
    || die "cosign is not installed, so the tarball's signature cannot be checked. Install cosign v3 or later (docs/install-airgap.md) and run this again. Nothing was loaded."
  [ -f "$TRUSTED_ROOT" ] \
    || die "$TRUSTED_ROOT is missing. It ships in the installer bundle: extract the whole bundle. Nothing was loaded."
  echo "==> Verifying that the ${version} release signed $name"
  cosign verify-blob \
    --new-bundle-format \
    --trusted-root "$TRUSTED_ROOT" \
    --bundle "$bundle" \
    --certificate-identity "${RELEASE_IDENTITY}${version}" \
    --certificate-oidc-issuer "$OIDC_ISSUER" \
    "$checksum" >&2 \
    || die "cosign could not verify $(basename "$checksum") as signed by the ${version} release (its own error is above). Nothing was loaded. Do not use this tarball until you know which of three causes it is: the signature is not that release's; this cosign cannot check it (v3 or later is required); or the Sigstore trusted root this installer ships, $TRUSTED_ROOT, is older than a key Sigstore rotated before the release. For the last, fetch the current installer bundle, https://onprem.caura.ai/bundle.tar.gz, on a connected machine, carry it across and run its airgap-load.sh."
  # The first field of the first line: a bare hash, as the release writes
  # it, or sha256sum's "<hash>  <name>". cosign verified the whole file.
  expected=$(awk 'NR == 1 { print $1 }' "$checksum")
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] \
    || die "$(basename "$checksum") does not hold one SHA-256 checksum. Nothing was loaded."
  [ "$(sha256_of "$tarball")" = "$expected" ] \
    || die "$name does not match its signed checksum: it is damaged or is not the published tarball. Nothing was loaded."
  echo "==> $name is the ${version} release's tarball"
}

SKIP_VERIFY=false
TARBALL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-verify) SKIP_VERIFY=true; shift ;;
    -*) die "unknown option $1. Usage: $0 [--skip-verify] [path/to/<tarball>.tar.gz]" ;;
    *)
      [ -z "$TARBALL" ] || die "one tarball at a time"
      TARBALL="$1"
      shift ;;
  esac
done
if [ -z "$TARBALL" ]; then
  TARBALL=$(ls -1 memclaw-onprem-*.tar.gz 2>/dev/null | head -n1 || true)
fi
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
  echo "ERROR: tarball not found." >&2
  echo "Usage: $0 [path/to/memclaw-onprem-<version>.tar.gz]" >&2
  exit 1
fi

if [ "$SKIP_VERIFY" = true ]; then
  {
    echo ""
    echo "WARNING: --skip-verify: loading $TARBALL WITHOUT checking that the"
    echo "WARNING: on-prem release built it. Whoever could replace this file could"
    echo "WARNING: have replaced every image in it. Use this only for a tarball"
    echo "WARNING: you have verified some other way."
    echo ""
  } >&2
else
  verify_release "$TARBALL"
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
