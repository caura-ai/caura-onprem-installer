#!/usr/bin/env bash
# Fail closed when a fleet repo's vendored legacy-name engine differs from caura's canonical copy.
set -euo pipefail

LOCAL_PATH=${1:-scripts/legacy_name_ratchet.py}
CANONICAL_URL=https://raw.githubusercontent.com/caura-ai/caura/main/scripts/legacy_name_ratchet.py
CANONICAL_PAGE=https://github.com/caura-ai/caura/blob/main/scripts/legacy_name_ratchet.py
SCRATCH=$(mktemp -d)
trap 'rm -rf -- "$SCRATCH"' EXIT
CANONICAL_COPY="$SCRATCH/legacy_name_ratchet.py"

error() {
  echo "::error::$*" >&2
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    error "legacy-name parity check needs sha256sum or shasum"
    return 1
  fi
}

if [ ! -f "$LOCAL_PATH" ] || [ -L "$LOCAL_PATH" ]; then
  error "legacy-name engine is missing or is not a regular file: $LOCAL_PATH"
  exit 1
fi

if ! curl --fail --silent --show-error --location --retry 3 \
  --proto '=https' --tlsv1.2 -o "$CANONICAL_COPY" "$CANONICAL_URL"; then
  error "legacy-name parity check could not fetch canonical engine from $CANONICAL_URL; refusing to validate against a stale or inferred copy"
  exit 1
fi

if [ ! -s "$CANONICAL_COPY" ]; then
  error "canonical legacy-name engine fetched from $CANONICAL_URL was empty; refusing to continue"
  exit 1
fi

local_sha=$(sha256_file "$LOCAL_PATH")
canonical_sha=$(sha256_file "$CANONICAL_COPY")

if cmp -s -- "$LOCAL_PATH" "$CANONICAL_COPY"; then
  echo "legacy-name engine matches canonical: $LOCAL_PATH (sha256: $local_sha)"
  exit 0
fi

echo "::error file=$LOCAL_PATH::vendored legacy-name engine differs from caura's canonical copy" >&2
echo "local sha256:     $local_sha" >&2
echo "canonical sha256: $canonical_sha" >&2
echo "canonical source: $CANONICAL_PAGE" >&2
cat >&2 <<'EOF'
Re-port procedure:
  1. Copy caura/scripts/legacy_name_ratchet.py into this repository unchanged.
  2. Run the repository's ratchet tests, ratchet gate/report, sentinel, and exact CI gates.
  3. Prove the report has zero added, annotated, removed, moved, and net movement.
  4. Do not change the ratchet config, allowlist, markers, or counted content to make the check pass.
EOF
exit 1
