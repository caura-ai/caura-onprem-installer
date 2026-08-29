#!/usr/bin/env bash
# Set the pinned image tag in an existing install's .env.
#
# Usage:
#   ./scripts/set-version.sh v1.1.0          # the stack version
#   ./scripts/set-version.sh --ops v1.1.0    # the scheduler/worker version
#   ./scripts/set-version.sh --home /srv/x v1.1.0
#
# Why this exists rather than a `sed` in the runbook. The version key has two
# spellings, and every .env written before the rename carries only the older
# one. A sed anchored on a single name matches nothing on a file that uses the
# other, exits 0, and prints nothing — so the operator believes the version
# moved, `docker compose up -d` re-resolves to the tag already running, every
# health check passes because nothing changed, and the upgrade "succeeded".
#
# Three things this does that a hand-typed regex does not:
#   1. rewrites whichever spellings the file actually has;
#   2. keeps them consistent when it has both, since Compose reads the newer
#      name first and a half-moved pair resolves to the stale one;
#   3. REFUSES when it has neither, rather than reporting success. Silence was
#      the whole hazard, so this path is the point of the script.
#
# It never ADDS a key. Introducing the new spelling into a customer's .env is a
# separate decision, and an upgrade is not the moment to take it.

set -euo pipefail

MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"  # legacy-name-floor: floor, identical to the five sibling day-2 scripts
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working

SUFFIX="VERSION"
OPS_MODE="false"
VERSION=""

# "$1", not "$*": every call site passes the exit code as $2, and "$*" printed
# it as part of the message ("ERROR Unknown flag: --foo 2"). install.sh:123 and
# upgrade.sh:104 define die() the same way and have the same tell.
die() { printf '\033[31mERROR\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ops)    SUFFIX="OPS_VERSION"; OPS_MODE="true"; shift ;;
    --home)   MEMCLAW_HOME="$2";    shift 2 ;;  # legacy-name-ok: the install-root variable, named as its sibling scripts name it
    -h|--help) sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0" | head -n 8; exit 0 ;;
    -*)       die "Unknown flag: $1" 2 ;;
    *)        [ -z "$VERSION" ] || die "Give exactly one version (got '$VERSION' and '$1')" 2
              VERSION="$1"; shift ;;
  esac
done

[ -n "$VERSION" ] || die "Usage: $0 [--ops] [--home DIR] <version>" 2

# Refuse anything that is not a legal image tag, rather than escaping it into
# the sed below. Escaping would make `&` and `/` safe; refusing also catches the
# typo or half-pasted argument that produced them. An unescaped `&` is sed's
# whole-match backreference, so a version like `v1&x` spliced the ENTIRE matched
# line back into the value and still reported success — the silent-corruption
# class this script exists to remove, reintroduced one layer down.
# Charset is Docker's: [a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}.
case "$VERSION" in
  [!A-Za-z0-9_]*)        die "Version must start with a letter, digit or underscore: '$VERSION'" 2 ;;
  *[!A-Za-z0-9._-]*)     die "Version may only contain letters, digits, '.', '_' and '-': '$VERSION'" 2 ;;
esac
[ "${#VERSION}" -le 128 ] || die "Version is longer than a tag may be (128): '$VERSION'" 2

ENV_FILE="$MEMCLAW_HOME/.env"  # legacy-name-ok: the install-root variable, named as its sibling scripts name it
[ -f "$ENV_FILE" ] || die "No .env at $ENV_FILE. Is this an install root?" 1

# Both spellings of the same key. Written out in full rather than built from
# $SUFFIX so the old names stay greppable — that is how the rename is tracked.
case "$SUFFIX" in
  VERSION)     KEYS="CAURA_VERSION MEMCLAW_VERSION" ;;          # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  OPS_VERSION) KEYS="CAURA_OPS_VERSION MEMCLAW_OPS_VERSION" ;;  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
esac

changed=""
for key in $KEYS; do
  grep -q "^$key=" "$ENV_FILE" || continue
  sed -i.bak "s/^$key=.*/$key=${VERSION}/" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
  changed="$changed $key"
done

if [ -z "$changed" ]; then
  # Absent means different things for the two keys, so this branch does too.
  #
  # For the ops tag, no line at all is a SUPPORTED state and the documented way
  # to make the scheduler images track the stack: with the key absent, compose
  # falls through to the stack version on its own. The runbook step that calls
  # this asks for exactly that, so it is already satisfied — refusing here would
  # abort a documented upgrade at step 2 over a box that is already correct.
  #
  # For the stack version, absent is the broken case. install.sh always writes
  # it, upgrade.sh refuses without it, and a missing one is the silent no-op
  # this script exists to make loud.
  if [ "$OPS_MODE" = "true" ]; then
    printf '\033[33m!!\033[0m  No ops-version key in %s — leaving it absent, so the scheduler images keep tracking the stack version.\n' "$ENV_FILE" >&2
    exit 0
  fi
  # Say which names were looked for: the likely cause is an .env that spells the
  # key a third way.
  die "No version key in $ENV_FILE — looked for:${KEYS// / }. Nothing changed." 1
fi

printf '\033[36m==>\033[0m %s -> %s in %s\n' "${changed# }" "$VERSION" "$ENV_FILE"
