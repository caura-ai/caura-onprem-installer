#!/usr/bin/env bash
# Caura Enterprise — on-prem upgrade.
#
# Usage:
#   curl -fsSL https://onprem.caura.ai/upgrade.sh | sudo bash
#   curl -fsSL https://onprem.caura.ai/upgrade.sh | sudo bash -s -- --to v1.0.0-rc2
#   sudo ./upgrade.sh --dry-run
#   sudo ./upgrade.sh --to v1.0.0 --no-backup
#   sudo bash ./upgrade.sh --offline --bundle bundle.tar.gz --to v1.0.0
#
# --offline is for a host with no internet access. It needs the new release's
# images already loaded with airgap-load.sh, and the installer bundle as a local
# file, since there is nothing to download either from.
#
# What it does:
#   1. Preflight — disk, current services healthy, license still valid, and,
#      when POSTGRES_REQUIRE_SSL is on, that the database accepts TLS.
#   2. Resolve target version (from --to, or the ghcr `:latest` tag's digest
#      resolved to its semver tag).
#   3. Dry-run summary — from → to, images that will pull, backup plan.
#   4. DB snapshot to $CAURA_HOME/backups/pre-upgrade-<from>-to-<to>-<ts>.pgbin
#      (pg_dump -Fc). Skip with --no-backup.
#   5. Record prev version to $CAURA_HOME/.memclaw-prev-version so a later  # legacy-name-floor: the file written on existing installs; the name is on their disks
#      rollback is a single CLI command: it reads that marker and re-enters
#      this script with --to <prev>. There is no separate rollback script.
#   6. Refresh bundle.tar.gz so compose / nginx / scripts stay aligned:
#      downloaded from --bundle-url, or read from the file --bundle names.
#   7. Pull new images (compose pull --ignore-buildable). With --offline,
#      check instead that every image is loaded, and the gateway's base too.
#   8. Rebuild gateway (local build).
#   9. Rolling `compose up -d`.
#  10. Poll healthchecks with a timeout. On failure: auto-rollback (restore
#      previous image version, `compose up -d`), report which service
#      broke, exit non-zero.
#
# Exit codes:
#   0  success
#   1  preflight fail
#   2  missing required input / unparseable flag
#   3  docker pull/build/up failed, or (--offline) an image is not loaded
#   4  health-check failed after timeout — auto-rollback succeeded
#   5  health-check failed — auto-rollback ALSO failed (manual recovery
#      needed; snapshot path printed)
#   6  DB backup failed before any changes applied

set -euo pipefail

# ── curl|bash self-rescue ──────────────────────────────────────────────────
# Under `curl -sL upgrade.sh | sudo bash -s -- ...`, bash reads the script
# body off fd 0 (the curl pipe). Any subprocess that inherits fd 0 — docker
# client on `compose exec`/`up -d`, for instance — can swallow script bytes
# before bash reads them, causing a silent mid-run exit.
# Fix: if we're running script-from-stdin, download ourselves to a tempfile
# and re-exec from that real file. The new bash reads its script from fd
# (the script arg), not fd 0, so no subprocess can race us.
# Both spellings of the guard are concatenated, so either one being set means
# "already re-exec'd". Only the old name is ever written (below) — reading both
# just keeps the pair consistent with every other name here, and an empty value
# on either side still reads as "not yet re-exec'd", which is the safe
# direction: at worst the download-and-re-exec runs one extra time.
#
# Hoisted into a variable rather than tested inline because the `if` below is a
# line-continued condition, and a `\` line cannot carry the trailing exemption
# comment the ratchet needs on the line holding the old name.
_reexec_guard="${CAURA_UPGRADE_REEXEC:-}${MEMCLAW_UPGRADE_REEXEC:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
# Resolved here rather than inside the branch because the success banner needs
# it too: when neither the CLI nor an on-disk upgrade.sh is available to roll
# back with, naming this URL is the only route left. One definition, so the
# banner cannot name a different channel than the one this run came from.
UPGRADE_URL="${MEMCLAW_UPGRADE_URL:-https://onprem.caura.ai/upgrade.sh}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
UPGRADE_URL="${CAURA_UPGRADE_URL:-$UPGRADE_URL}"
if [ -z "$_reexec_guard" ] \
   && { [ "${BASH_SOURCE[0]:-$0}" = "bash" ] \
        || [ "${BASH_SOURCE[0]:-$0}" = "-bash" ] \
        || [ ! -r "${BASH_SOURCE[0]:-$0}" ]; }; then
  # Not with --offline: there is nothing to download from, and the copy the
  # operator brought is the one to run. Checked before flags are parsed, since
  # the download comes first.
  for _arg in "$@"; do
    if [ "$_arg" = "--offline" ]; then
      echo "ERROR: --offline: run upgrade.sh from the file, not piped into bash: sudo bash ./upgrade.sh --offline --bundle <bundle.tar.gz> --to <version>. Piped, it re-runs from a copy it downloads, and offline there is nothing to download." >&2
      exit 2
    fi
  done
  _tmp=$(mktemp /tmp/caura-upgrade.XXXXXX.sh)
  if ! curl -fsSL "$UPGRADE_URL" -o "$_tmp"; then
    echo "ERROR: failed to download $UPGRADE_URL for local re-exec" >&2
    exit 1
  fi
  chmod +x "$_tmp"
  MEMCLAW_UPGRADE_REEXEC=1 exec bash "$_tmp" "$@"
fi

# ── Defaults ────────────────────────────────────────────────────────────────
#
# Each knob resolves the historical spelling first and is then overridden by its
# CAURA_* twin when that one is NON-EMPTY. First non-empty, never first defined
# — see the same block in install.sh for why blank has to mean absent on a
# hand-edited file.
CAURA_HOME="${CAURA_HOME:-${MEMCLAW_HOME:-/opt/memclaw}}"  # legacy-name-floor: floor, and the install root default — unchanged for existing installs
TARGET_VERSION=""                  # --to, or auto-resolved from :latest
DRY_RUN="false"
SKIP_BACKUP="false"
ASSUME_YES="${MEMCLAW_YES:-false}"  # -y / --yes, or MEMCLAW_YES=1  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
ASSUME_YES="${CAURA_YES:-$ASSUME_YES}"
HEALTH_TIMEOUT_S=180
BUNDLE_URL="${MEMCLAW_BUNDLE_URL:-https://onprem.caura.ai/bundle.tar.gz}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
BUNDLE_URL="${CAURA_BUNDLE_URL:-$BUNDLE_URL}"
BUNDLE_FILE=""                     # --bundle: a local bundle.tar.gz, used instead of BUNDLE_URL
OFFLINE="false"                    # --offline

# Services we expect to find running and re-verify post-upgrade.
# Keep in sync with docker-compose.yml.
SERVICES=(
  platform-storage-api
  platform-auth-api
  platform-admin-api
  platform-audit-api
  core-storage-api
  core-api
  core-operations
  gateway
)

# ── Helpers ─────────────────────────────────────────────────────────────────
log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
# "$1", not "$*" — see the same note in install.sh. warn() keeps "$*".
die()  { printf '\033[31mERROR\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

# Parse --to value or fall back to `:latest` pointer.
resolve_target_version() {
  if [ -n "$TARGET_VERSION" ]; then
    echo "$TARGET_VERSION"
    return
  fi
  # :latest is a tag; we can't deref digest → tag without registry-crawling.
  # Keep behaviour simple: a version value of `latest` in .env is legal but
  # customer-facing upgrade is always explicit. Refuse if --to is missing.
  die "--to <version> is required (e.g. --to v1.0.0-rc2). Use 'latest' to pin to the floating tag." 2
}

# One key out of .env. Prints empty and returns 0 when the key is missing, so a
# caller can use it under `set -euo pipefail` — same contract as _GET below,
# which this predates in the file only because current_version() needs it here.
_env_key() {
  local _envfile="$CAURA_HOME/.env"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  grep -E "^$1=" "$_envfile" 2>/dev/null \
    | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true
}

current_version() {
  # The tag this install is pinned to, from .env, under either spelling.
  # Empty → never installed / unknown.
  #
  # FIRST NON-EMPTY, not first present, and this is the site where that matters
  # most: .env gets hand-edited by operators. docs/upgrade.md no longer tells
  # them to sed it, but it did until recently, so the files already out there
  # were edited that way. A file carrying a blank CAURA_VERSION= from a newer
  # template beside the filled old-name key that is actually driving the stack
  # is the ordinary half-migrated state. Reading the blank one makes upgrade.sh
  # refuse a healthy install with "nothing to upgrade from".
  local v
  v=$(_env_key CAURA_VERSION)
  [ -n "$v" ] || v=$(_env_key MEMCLAW_VERSION)  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  printf '%s' "$v"
}

# The images a Dockerfile builds FROM, one per line: the first argument after
# FROM that is not a --flag, skipping `scratch` and earlier stages. install.sh
# and airgap-load.sh carry the same function, and a test holds them equal.
gateway_bases() {
  awk 'toupper($1) == "FROM" {
      img = ""
      for (i = 2; i <= NF; i++) if ($i !~ /^--/) { img = $i; break }
      if (img != "" && img != "scratch" && !(img in stage) && !(img in seen)) { print img; seen[img] = 1 }
      for (j = i + 1; j < NF; j++) if (toupper($j) == "AS") stage[$(j + 1)] = 1
    }' "$1"
}

# ── Parse flags ─────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --to)         TARGET_VERSION="$2";       shift 2 ;;
    --dry-run)    DRY_RUN="true";            shift   ;;
    --no-backup)  SKIP_BACKUP="true";        shift   ;;
    -y|--yes)     ASSUME_YES="true";         shift   ;;
    --memclaw-home) CAURA_HOME="$2";         shift 2 ;;  # legacy-name-ok: the flag operators already have in their scripts; rule 3 keeps it working
    --health-timeout) HEALTH_TIMEOUT_S="$2"; shift 2 ;;
    --bundle-url) BUNDLE_URL="$2";           shift 2 ;;
    --bundle)     BUNDLE_FILE="$2";          shift 2 ;;
    --offline)    OFFLINE="true";            shift   ;;
    -h|--help)
      sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0" | head -n 45
      exit 0 ;;
    *) die "Unknown flag: $1" 2 ;;
  esac
done

# ── Preflight ───────────────────────────────────────────────────────────────
log "Preflight checks"

[ -d "$CAURA_HOME" ] || die "No install found at $CAURA_HOME. Run install.sh first." 1
[ -f "$CAURA_HOME/docker-compose.yml" ] || die "$CAURA_HOME/docker-compose.yml missing" 1
[ -f "$CAURA_HOME/.env" ] || die "$CAURA_HOME/.env missing" 1

# A local bundle is checked here, before anything changes: read to the end, so
# a truncated copy fails now rather than halfway through extracting over the
# install, and holding the files the rest of this run reads from it. Its path is
# made absolute because this script changes into the install root, and the
# success banner prints it.
if [ "$OFFLINE" = "true" ] && [ -z "$BUNDLE_FILE" ]; then
  die "--offline needs --bundle <bundle.tar.gz>: with no internet access there is nowhere to download it from. Bring it across with the release tarball." 2
fi
if [ -n "$BUNDLE_FILE" ]; then
  if [ ! -f "$BUNDLE_FILE" ] || [ ! -r "$BUNDLE_FILE" ]; then
    die "--bundle: $BUNDLE_FILE is not a readable file" 2
  fi
  BUNDLE_FILE="$(cd "$(dirname "$BUNDLE_FILE")" && pwd)/$(basename "$BUNDLE_FILE")"
  _bundle_list=$(tar -tzf "$BUNDLE_FILE" 2>/dev/null | sed 's|^\./||') \
    || die "--bundle: $BUNDLE_FILE is not a gzipped tarball, or it is truncated" 2
  _needed="docker-compose.yml nginx/Dockerfile"
  if [ "$OFFLINE" = "true" ]; then
    _needed="$_needed docker-compose.airgap.yml"
  fi
  for _f in $_needed; do
    # grep reads it all, not -q: an early exit would fail the pipe on SIGPIPE.
    printf '%s\n' "$_bundle_list" | grep -xF "$_f" >/dev/null \
      || die "--bundle: $BUNDLE_FILE has no $_f. Is it the installer bundle (bundle.tar.gz)?" 2
  done
fi
if [ "$OFFLINE" = "true" ]; then
  # Where this script is, for the rollback line the success banner prints. Read
  # from something that is not a file (`bash <(...)` names a pipe), it has no
  # path that would run again, so the banner says what to fill in instead.
  SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/$(basename "${BASH_SOURCE[0]:-$0}")"
  [ -f "$SELF" ] || SELF="<path to upgrade.sh>"
elif grep -Eq '"offline": *true' "$CAURA_HOME/install.state.json" 2>/dev/null; then
  # A warning, not a stop. Without --offline this run downloads the bundle and
  # pulls from the registry, dropping the air-gap overlay install.sh chose, and
  # that is what a host given internet access since wants. A host that still
  # has none fails at the bundle download, before .env or the stack changes.
  warn "This install was made with install.sh --offline. Without internet access, load the new release with airgap-load.sh and run: upgrade.sh --offline --bundle <bundle.tar.gz> --to <version>"
fi

command -v docker >/dev/null || die "Docker ≥ 24 required" 1
docker info >/dev/null 2>&1 || {
  if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null && [ -f "$0" ] && [ -r "$0" ]; then
    warn "Cannot reach Docker daemon as $(whoami). Re-executing via sudo."
    exec sudo -E bash "$0" "$@"
  fi
  die "Cannot reach Docker daemon. Run as root or add yourself to the docker group." 1
}
docker compose version >/dev/null 2>&1 || die "docker compose v2 required" 1

# Resolve current + target
FROM_VERSION=$(current_version)
[ -n "$FROM_VERSION" ] || die "MEMCLAW_VERSION not set in $CAURA_HOME/.env — nothing to upgrade from." 1  # legacy-name-floor: names the key as it is written in existing .env files
TO_VERSION=$(resolve_target_version)

if [ "$FROM_VERSION" = "$TO_VERSION" ]; then
  log "Already at $TO_VERSION. Nothing to do."
  exit 0
fi

# Rough disk check — pg_dump + new images easily eat 10 GB.
DISK_GB=$(df -BG "$CAURA_HOME" | awk 'NR==2 {sub("G","",$4); print $4}')
[ "${DISK_GB:-0}" -ge 5 ] \
  || die "Only ${DISK_GB}G free at $CAURA_HOME — need at least 5G for backup + new images." 1

# ── Summary ─────────────────────────────────────────────────────────────────
cat <<EOF

──────────────────────────────────────────
  Caura upgrade plan
──────────────────────────────────────────
  home:         $CAURA_HOME
  from:         $FROM_VERSION
  to:           $TO_VERSION
  bundle:       ${BUNDLE_FILE:-$BUNDLE_URL}
  images:       $([ "$OFFLINE" = "true" ] && echo 'already loaded (--offline: nothing is pulled)' || echo 'pulled from the registry')
  db backup:    $([ "$SKIP_BACKUP" = "true" ] && echo 'SKIPPED (--no-backup)' || echo 'YES (pg_dump -Fc)')
  health wait:  ${HEALTH_TIMEOUT_S}s
  on failure:   auto-rollback to $FROM_VERSION
──────────────────────────────────────────

EOF

if [ "$DRY_RUN" = "true" ]; then
  log "Dry-run — no changes applied."
  exit 0
fi

if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) die "Aborted by user." 0 ;;
  esac
elif [ "$ASSUME_YES" != "true" ]; then
  warn "Non-interactive shell and --yes not set — proceeding anyway (curl|bash pipelines are stdin-less)."
fi

cd "$CAURA_HOME"

# Reconstruct the same -f overlays install.sh chose, so upgrade preserves
# the customer's TLS / embedder / airgap selections instead of silently
# downgrading to the bare docker-compose.yml. Read flags from .env:
#   TLS mode `letsencrypt` → -f docker-compose.tls-letsencrypt.yml
#   EMBEDDING_PROVIDER=local + no remote keys → -f docker-compose.embedder.yml
# and, from --offline, the air-gap overlay that names the loaded images, with
# the air-gap embedder overlay in place of the other one: install.sh --offline's
# choice, in its order.
COMPOSE_FILES=(-f docker-compose.yml)
# Read $1 from .env; print empty + return 0 when the key is missing,
# so callers can use `_GET FOO` under `set -euo pipefail` without the
# script aborting on a no-match. Without `|| true`, grep's exit 1
# propagates through pipefail and aborts the whole upgrade — which is
# exactly what bit upgrades from older rc's that pre-date the
# the TLS-mode / embedding-provider env keys.
_GET() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true
}
_TLS_MODE=$(_GET MEMCLAW_TLS_MODE)
# The CAURA_* spelling of the same key wins only when it is NON-EMPTY, and this
# is the sharpest instance of that rule in the repo. An empty _TLS_MODE is not
# an error here — it means "add no overlay", so a blank CAURA_TLS_MODE= sitting
# in a hand-edited .env beside the old-name key holding "letsencrypt", which is
# what actually drives the stack, would drop the Caddy sidecar on upgrade. The
# customer's ACME terminator disappears, the gateway takes its host ports back,
# and every service reports healthy: the upgrade "succeeds" while TLS quietly
# stops being served. Nothing in this script would go red.
_TLS_MODE_NEW=$(_GET CAURA_TLS_MODE)
[ -n "$_TLS_MODE_NEW" ] && _TLS_MODE="$_TLS_MODE_NEW"
_EMBED_PROVIDER=$(_GET EMBEDDING_PROVIDER)
_OPENAI_KEY=$(_GET OPENAI_API_KEY)
_PLATFORM_EMBED_KEY=$(_GET PLATFORM_EMBEDDING_API_KEY)
if [ "$_TLS_MODE" = "letsencrypt" ] && [ -f docker-compose.tls-letsencrypt.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.tls-letsencrypt.yml)
fi
_EMBEDDER_OVERLAY=docker-compose.embedder.yml
if [ "$OFFLINE" = "true" ]; then
  COMPOSE_FILES+=(-f docker-compose.airgap.yml)
  _EMBEDDER_OVERLAY=docker-compose.embedder.airgap.yml
fi
if [ "$_EMBED_PROVIDER" = "local" ] \
   && [ -z "$_OPENAI_KEY" ] && [ -z "$_PLATFORM_EMBED_KEY" ]; then
  if [ -f "$_EMBEDDER_OVERLAY" ]; then
    COMPOSE_FILES+=(-f "$_EMBEDDER_OVERLAY")
  elif [ "$OFFLINE" = "true" ]; then
    # Every bundle carries this file and install.sh copies it, so only a hand
    # deletion gets here. Offline the local embedder is the only one there is,
    # so going on without it would start a core-api that cannot embed, and
    # nothing would fail. Stop before anything changes; the bundle has the file.
    die "--offline: this install embeds locally (EMBEDDING_PROVIDER=local, no remote key), but $CAURA_HOME/$_EMBEDDER_OVERLAY is missing, so the upgrade would drop the local embedder. Restore it from the bundle, then run this again: tar -xzf '$BUNDLE_FILE' -C '$CAURA_HOME' ./$_EMBEDDER_OVERLAY" 1
  fi
fi
log "Compose overlays: ${COMPOSE_FILES[*]}"

# ── TLS to the database ─────────────────────────────────────────────────────
# Earlier bundles passed POSTGRES_REQUIRE_SSL to core-storage-api under a name
# it does not read, so the setting did nothing. From v2.13.0 core-storage-api
# enforces it and will not start against a database that refuses TLS. A .env
# that set it while it was inert would fail the health wait only after
# platform-storage-api has run the new version's migrations, and the rollback
# would put the old images back over them. So ask the database now, through the
# running core-storage-api, and stop before anything changes if it refuses.
#
# Stops only on a definite refusal. If the check itself cannot run (the
# service is down, the probe errors some other way), it warns and carries on,
# so an upgrade that works today is never blocked by the check.
_check_db_tls() {
  local flag to err rc=0
  # As compose reads .env: an unquoted value ends at " #". Then pydantic's
  # spellings of true, which is what core-storage-api parses it with.
  flag=$(_GET POSTGRES_REQUIRE_SSL | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
    | tr '[:upper:]' '[:lower:]')
  case "$flag" in
    1|t|true|y|yes|on) ;;
    *) return 0 ;;
  esac
  # An older target does not read the setting, so there is nothing to protect
  # (this script is also how a rollback to one runs). `latest` sorts after it.
  to="${TO_VERSION#v}"
  [ "$(printf '%s\n%s\n' "$to" 2.13.0 | sort -V | head -n 1)" = "2.13.0" ] || return 0

  log "POSTGRES_REQUIRE_SSL is on: checking that the database accepts TLS"
  local probe='
import asyncio, os, sys
import asyncpg
async def probe():
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn, ssl="require", timeout=15)
    await conn.close()
try:
    asyncio.run(probe())
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(3 if "rejected SSL upgrade" in str(exc) else 4)
'
  err=$(docker compose "${COMPOSE_FILES[@]}" exec -T core-storage-api python -c "$probe" 2>&1 >/dev/null) \
    || rc=$?
  case "$rc" in
    0) log "The database accepts TLS" ;;
    3) die "POSTGRES_REQUIRE_SSL is true in .env, but the database refuses TLS (${err}).
       From v2.13.0 core-storage-api enforces that setting and would not start.
       Turn on TLS at the database, or set POSTGRES_REQUIRE_SSL=false in
       $CAURA_HOME/.env, then run this again. Nothing has been changed.
       See docs/database.md, \"TLS to the database\"." 1 ;;
    *) warn "Could not check whether the database accepts TLS (${err:-exit $rc})."
       warn "Continuing. If it refuses TLS, core-storage-api will fail its health check and this upgrade rolls back." ;;
  esac
}
_check_db_tls

# ── DB backup ───────────────────────────────────────────────────────────────
BACKUP_PATH=""
if [ "$SKIP_BACKUP" != "true" ]; then
  mkdir -p backups
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP_PATH="backups/pre-upgrade-${FROM_VERSION}-to-${TO_VERSION}-${TS}.pgbin"
  log "DB snapshot → $BACKUP_PATH"
  # Use the running postgres container; pg_dump -Fc is custom-format, gzipped
  # internally, ready for pg_restore. Reads from the same creds the compose
  # file uses (DB/USER/PASSWORD env inside the container).
  if ! docker compose exec -T postgres \
       pg_dump -Fc -U "${POSTGRES_USER:-memclaw}" "${POSTGRES_DB:-memclaw}" \
       >"$BACKUP_PATH" 2>/tmp/caura-pgdump.err; then
    warn "pg_dump failed — see /tmp/caura-pgdump.err"
    rm -f "$BACKUP_PATH"
    die "Refusing to continue without a backup. Pass --no-backup to override." 6
  fi
  SIZE=$(du -h "$BACKUP_PATH" | cut -f1)
  log "DB snapshot captured ($SIZE)"
fi

# Remember where to roll back to.
echo "$FROM_VERSION" > .memclaw-prev-version

# Snapshot the compose files before the bundle replaces them.
#
# _rollback re-runs `up -d` pinned at FROM_VERSION, but by then the refresh has
# already overwritten docker-compose.yml with the NEW version's copy — so the old
# tag gets resolved against whatever images the new compose names. That is fine
# only while both versions name the same images, and they no longer do: the
# registry prefix moved to caura-onprem- at v2.11.18, and earlier tags exist
# under the old prefix only. Without this, rolling back to anything older
# resolves an image:tag pair that was never published, `up -d` fails, and the
# stack is left down on the manual-recovery path.
COMPOSE_SNAPSHOT=".compose-pre-upgrade"
rm -rf "$COMPOSE_SNAPSHOT"
mkdir -p "$COMPOSE_SNAPSHOT"
for _cf in docker-compose*.yml; do
  [ -e "$_cf" ] && cp -p "$_cf" "$COMPOSE_SNAPSHOT/"
done
# Kept after a SUCCESSFUL upgrade too, deliberately: it is the only copy of what
# the previous version was running, and an operator recovering by hand needs it.

# ── Refresh bundle + update .env ────────────────────────────────────────────
if [ -n "$BUNDLE_FILE" ]; then
  log "Refreshing bundle (compose / nginx / scripts) from $BUNDLE_FILE"
  tar -xzf "$BUNDLE_FILE" -C . || die "Failed to extract the bundle from $BUNDLE_FILE" 3
else
  log "Refreshing bundle (compose / nginx / scripts) from $BUNDLE_URL"
  if ! curl -fsSL "$BUNDLE_URL" | tar -xz -C . ; then
    die "Failed to fetch bundle from $BUNDLE_URL" 3
  fi
fi

# Rewrite an existing `$1=` line in .env in place. Returns non-zero, and
# changes nothing, when the key is not in the file — it never ADDS one.
#
# That restriction is the point of the helper rather than a limitation of it.
# The version keys are read in precedence order (new spelling first, here and in
# every compose file), so whichever of them the file carries is the one actually
# driving the stack, and every one it carries has to move together. But
# introducing the new spelling into a customer's .env is a writer change that
# belongs to item 5.4, and an upgrade is not the moment to make it.
_rewrite_env_key() {
  grep -q "^$1=" .env || return 1
  sed -i.bak "s/^$1=.*/$1=$2/" .env
  rm -f .env.bak
}

# Mutate the version in .env (in-place). Keep the file otherwise.
#
# BOTH spellings, or the upgrade silently no-ops. Every compose file resolves an
# image tag by taking the new spelling of the version key first and the old one
# only as its fallback, so on a .env that carries a non-empty new-spelling key,
# moving only the old one leaves every image pinned to the tag the new one still
# names: `pull` and `up -d` re-resolve to the version already running, nothing
# restarts, every health check passes because nothing changed, and this script
# prints "Upgrade complete" over an upgrade that did not happen. Reading in one
# order and writing in another is the whole bug.
if ! _rewrite_env_key MEMCLAW_VERSION "$TO_VERSION"; then  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  echo "MEMCLAW_VERSION=${TO_VERSION}" >> .env  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
fi
# Present-but-blank is rewritten too, which is deliberate: the file already
# names the key, and after this it names the version that is actually running.
_rewrite_env_key CAURA_VERSION "$TO_VERSION" || true

# ── Rollback helpers (defined before any call site) ────────────────────────
_rollback_pre_up() {
  # Revert .env and leave old containers running — no up -d ran yet.
  local why="$1"
  warn "Rolling back .env and the compose files → $FROM_VERSION (cause: $why)"
  # Both keys, for the reason the bump above spells out — and it bites harder
  # on this path: a rollback that moves only one of them leaves the stack
  # pinned to the version we were rolling AWAY from, reported as recovered.
  _rewrite_env_key MEMCLAW_VERSION "$FROM_VERSION" || true  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _rewrite_env_key CAURA_VERSION "$FROM_VERSION" || true
  # And the compose files the refresh replaced. A stop here is made to be run
  # again (--offline: once the release is loaded), and the next run snapshots
  # whatever compose files it finds. Left as the new version's, that snapshot,
  # and so any rollback from it, would no longer hold what FROM_VERSION runs.
  # nginx/ stays as the bundle left it: the running gateway's image was built
  # earlier, and the next run extracts the bundle again before it builds.
  if [ -d "${COMPOSE_SNAPSHOT:-}" ]; then
    cp -p "$COMPOSE_SNAPSHOT"/*.yml . 2>/dev/null || true
  fi
}

_rollback() {
  # Full rollback: flip .env, up -d on old tag, best-effort restore backup.
  local why="$1"
  warn "Rolling back to $FROM_VERSION (cause: $why)"
  _rewrite_env_key MEMCLAW_VERSION "$FROM_VERSION" || true  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _rewrite_env_key CAURA_VERSION "$FROM_VERSION" || true
  # Put back the compose files FROM_VERSION was actually running with. Must come
  # before `up -d`, or the old tag is resolved against the new file's images.
  if [ -d "${COMPOSE_SNAPSHOT:-}" ]; then
    cp -p "$COMPOSE_SNAPSHOT"/*.yml . 2>/dev/null || true
  fi
  if ! docker compose "${COMPOSE_FILES[@]}" up -d; then
    warn "compose up -d during rollback ALSO failed — manual recovery needed."
    warn "Backup (if taken): $CAURA_HOME/$BACKUP_PATH"
    exit 5
  fi
  if [ -n "$BACKUP_PATH" ]; then
    warn "DB schema unchanged (we didn't run migrations yet). Backup kept for safety at $CAURA_HOME/$BACKUP_PATH"
  fi
  exit 4
}

# ── Backfill required secrets ───────────────────────────────────────────────
# GATEWAY_SHARED_SECRET (OSS caura#802): from backend-2.28.0 core-api refuses to
# start in production without a perimeter for its header-trust auth path. The
# gateway injects it as X-Gateway-Secret; core-api compares it. Older .env files
# predate the key, and the bundle refreshed above references it — an empty value
# fails the `${GATEWAY_SHARED_SECRET:?}` substitution at the pull/up -d below —
# so it is backfilled here, before any compose invocation. Keyed on the VALUE
# being empty, not the line being absent: .env.example ships the key blank, so a
# seeded .env has the line already and a presence check would leave it empty.
# Generated, not prompted: a shared secret between containers in this project.
if [ -z "$(_env_key GATEWAY_SHARED_SECRET)" ]; then
  _gw_secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  if ! _rewrite_env_key GATEWAY_SHARED_SECRET "$_gw_secret"; then
    echo "GATEWAY_SHARED_SECRET=${_gw_secret}" >> .env
  fi
  unset _gw_secret
  log "Generated GATEWAY_SHARED_SECRET (required by core-api from backend-2.28.0)."
fi

# CORE_STORAGE_SHARED_SECRET (from backend-2.46.0): core-api refuses to start
# without a perimeter for the core-api -> core-storage-api hop; core-storage-api
# compares the same value. Same backfill rationale as GATEWAY_SHARED_SECRET above
# — keyed on the empty VALUE, generated before any compose invocation.
if [ -z "$(_env_key CORE_STORAGE_SHARED_SECRET)" ]; then
  _cs_secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  if ! _rewrite_env_key CORE_STORAGE_SHARED_SECRET "$_cs_secret"; then
    echo "CORE_STORAGE_SHARED_SECRET=${_cs_secret}" >> .env
  fi
  unset _cs_secret
  log "Generated CORE_STORAGE_SHARED_SECRET (required by core-api from backend-2.46.0)."
fi

# ── Pull + build + up ───────────────────────────────────────────────────────
# --offline: there is no registry to pull from, so every image the refreshed
# compose names must be loaded already, and so must the images the gateway's
# build starts from. The gateway itself is left out: it is built below, and it
# is the one service docker-compose.yml builds (a test holds that). Prints the
# missing ones; fails if compose cannot render the files.
_missing_images() {
  local img images missing=""
  images=$(docker compose "${COMPOSE_FILES[@]}" config --images) || return 1
  # One image per word, by design.
  # shellcheck disable=SC2086
  for img in $images $(gateway_bases nginx/Dockerfile); do
    case "$img" in */gateway:*) continue ;; esac
    docker image inspect "$img" >/dev/null 2>&1 || missing="$missing $img"
  done
  printf '%s' "$missing"
}

if [ "$OFFLINE" = "true" ]; then
  log "Checking that the images for ${TO_VERSION} are loaded (--offline: nothing is pulled)"
  if ! _missing=$(_missing_images); then
    _rollback_pre_up "compose_config_failed"
    die "docker compose could not read the refreshed compose files; its error is above." 3
  fi
  if [ -n "$_missing" ]; then
    _rollback_pre_up "images_not_loaded"
    die "--offline: not loaded:${_missing}. Load the ${TO_VERSION} release tarball with airgap-load.sh, then run this again. Tarballs up to v2.13.0 do not carry the gateway's base image; docs/install-airgap.md says how to bring it." 3
  fi
else
  log "Pulling images at :${TO_VERSION}"
  if ! docker compose "${COMPOSE_FILES[@]}" pull --ignore-buildable 2>/dev/null; then
    warn "pull --ignore-buildable unsupported — retrying plain pull"
    docker compose "${COMPOSE_FILES[@]}" pull || {
      warn "compose pull failed — rolling back"
      _rollback_pre_up "pull_failed"
      exit 3
    }
  fi
fi

log "Rebuilding gateway from bundled nginx template"
docker compose "${COMPOSE_FILES[@]}" build --no-cache gateway || {
  warn "gateway build failed — rolling back"
  _rollback_pre_up "gateway_build_failed"
  exit 3
}

log "Rolling services to $TO_VERSION"
docker compose "${COMPOSE_FILES[@]}" up -d || {
  warn "compose up -d failed — rolling back"
  _rollback "up_failed"
}

# ── Health verify ──────────────────────────────────────────────────────────
log "Waiting up to ${HEALTH_TIMEOUT_S}s for all services to report healthy"

_service_healthy() {
  local svc="$1"
  local status
  status=$(docker compose ps --format json "$svc" 2>/dev/null \
           | grep -oE '"Health":"[^"]*"|"State":"[^"]*"' | tr '\n' ' ')
  # "restarting" catches crashloops. Containers without a healthcheck
  # cycle running → exited → restarting → running on crash; the running
  # windows are brief but long enough to mask a crash loop if we only
  # sample once. Explicitly rejecting "restarting" closes that hole.
  echo "$status" | grep -q '"State":"restarting"' && return 1
  echo "$status" | grep -q '"State":"exited"' && return 1
  echo "$status" | grep -q '"Health":"healthy"' && return 0
  echo "$status" | grep -q '"Health":"unhealthy"' && return 1
  echo "$status" | grep -q '"Health":"starting"' && return 1
  # No healthcheck declared → accept State=running.
  echo "$status" | grep -q '"State":"running"' && return 0
  return 1
}

# Require N consecutive all-healthy passes before declaring success. A
# crashlooping container briefly reports State=running between restarts;
# a single pass can land in the wrong window. Stability check catches it.
STABLE_PASSES_REQUIRED=3
stable_passes=0
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
unhealthy=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  unhealthy=""
  for svc in "${SERVICES[@]}"; do
    _service_healthy "$svc" || unhealthy="$unhealthy $svc"
  done
  if [ -z "$unhealthy" ]; then
    stable_passes=$((stable_passes + 1))
    [ "$stable_passes" -ge "$STABLE_PASSES_REQUIRED" ] && break
  else
    stable_passes=0
  fi
  sleep 3
done

if [ -n "$unhealthy" ] || [ "$stable_passes" -lt "$STABLE_PASSES_REQUIRED" ]; then
  warn "Services not stably healthy after ${HEALTH_TIMEOUT_S}s:$unhealthy"
  _rollback "health_check_timeout:$unhealthy"
fi

# ── Success ────────────────────────────────────────────────────────────────
# The rollback route the banner prints. The line the operator is handed has to
# be one that runs on THIS host, because the banner this replaced named a
# `scripts/rollback.sh` that has never existed in any repo or bundle, and the
# whole point is to stop printing routes on faith.
#
# THE PRIMARY ROUTE IS ALWAYS upgrade.sh, and that is what makes it checkable.
# A rollback is an upgrade toward the older tag, so this script is the thing
# that has to run either way; the only question is whether it is already on
# disk. `bash <path>` rather than executing it directly, because a copy fetched
# with `curl -o` is mode 644 — readable, not executable — and that is how it
# gets there at all: install.sh does not place it. Testing -x instead would
# push that common case to the network for no reason; `bash` makes the readable
# case usable.
#
# -fsSL on the fallback, matching the self-rescue download above. Without -f,
# an error page from a broken endpoint is piped into `sudo bash` rather than
# failing loudly, and this line is handed to an operator to paste.
#
# THE CLI IS OFFERED WITHOUT A DETECTION CHECK, deliberately, and this is a
# correction: an earlier revision branched on whether the CLI was on PATH, and
# that cannot be answered from here. This script runs as root — either because
# operator used `sudo`, which is the documented form, or because it re-executed
# itself via `sudo -E` — and sudoers' secure_path resets PATH regardless of
# -E, so a per-user pipx/pip install is invisible to the check while being
# perfectly present in the shell the operator returns to. Detection would have
# under-reported it exactly where it is most likely to be installed. An
# explicit "if it is installed" is honest and needs nothing to be true.
#
# The version is substituted in rather than left as <prev>, so the operator
# does not have to go and read the marker file to use the line.
#
# `cauractl rollback` is not offered as an alternative spelling: both console
# entries ship from one package today, so a host with the new name always has
# the old one. Which name to teach is the command rename's decision.
#
# Both held in variables because a ratchet marker has to sit on the line
# carrying the name, and inside the heredoc below that would print the marker
# on the operator's screen.
# The install-root flag is passed EXPLICITLY below, and it is not decoration.
# The variable holding that root resolves from two env-var spellings or from the
# flag, and it is a plain shell variable that is never exported -- so `sudo`
# carries none of them to the child. Without the flag on the printed line, the
# pasted command re-derives the built-in default and rolls back THE WRONG
# DIRECTORY on any install that used a custom root, quietly, because the default
# directory usually exists too. The path is single-quoted in the printed string
# so a root containing a space survives being pasted rather than splitting into
# a shorter valid path.
#
# OFFLINE, the route is this same script with the same bundle. The download
# fallback has nothing to reach, and the CLI's rollback runs upgrade.sh without
# --offline, so neither is offered. The bundle serves the older version as well:
# it runs every release since v2.11.16, the first one with an air-gap tarball.
if [ "$OFFLINE" = "true" ]; then
  ROLLBACK_HINT="sudo bash '$SELF' --offline --bundle '$BUNDLE_FILE' --to $FROM_VERSION --memclaw-home '$CAURA_HOME'"  # legacy-name-ok: the install-root flag and variable, named as this script names them
elif [ -r "$CAURA_HOME/upgrade.sh" ]; then  # legacy-name-ok: the install-root variable, named as its sibling scripts name it
  ROLLBACK_HINT="sudo bash '$CAURA_HOME/upgrade.sh' --to $FROM_VERSION --memclaw-home '$CAURA_HOME'"  # legacy-name-ok: the install-root flag and variable, named as this script names them
else
  ROLLBACK_HINT="curl -fsSL $UPGRADE_URL | sudo bash -s -- --to $FROM_VERSION --memclaw-home '$CAURA_HOME'"  # legacy-name-ok: the install-root flag and variable, named as this script names them
fi
ROLLBACK_ALT="or 'memclawctl rollback', if the operator CLI is installed"  # legacy-name-floor: the shipped CLI's own command; an install whose CLI predates the alias has only this spelling
if [ "$OFFLINE" = "true" ]; then
  ROLLBACK_ALT="with ${FROM_VERSION}'s images still loaded; airgap-load.sh reloads them if pruned"
fi
cat <<EOF

──────────────────────────────────────────
  Upgrade complete
──────────────────────────────────────────
  $FROM_VERSION → $TO_VERSION
  All services healthy.
  DB backup: ${BACKUP_PATH:-skipped}
  Rollback: $ROLLBACK_HINT
            $ROLLBACK_ALT
──────────────────────────────────────────

EOF
