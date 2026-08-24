#!/usr/bin/env bash
# MemClaw Enterprise — on-prem upgrade.
#
# Usage:
#   curl -sL https://onprem.caura.ai/upgrade.sh | sudo bash
#   curl -sL https://onprem.caura.ai/upgrade.sh | sudo bash -s -- --to v1.0.0-rc2
#   sudo ./upgrade.sh --dry-run
#   sudo ./upgrade.sh --to v1.0.0 --no-backup
#
# What it does:
#   1. Preflight — disk, current services healthy, license still valid.
#   2. Resolve target version (from --to, or the ghcr `:latest` tag's digest
#      resolved to its semver tag).
#   3. Dry-run summary — from → to, images that will pull, backup plan.
#   4. DB snapshot to $MEMCLAW_HOME/backups/pre-upgrade-<from>-to-<to>-<ts>.pgbin
#      (pg_dump -Fc). Skip with --no-backup.
#   5. Record prev version to $MEMCLAW_HOME/.memclaw-prev-version so a later
#      `rollback.sh` is a single command.
#   6. Refresh bundle.tar.gz so compose / nginx / scripts stay aligned.
#   7. Pull new images (compose pull --ignore-buildable).
#   8. Rebuild gateway (local build).
#   9. Rolling `compose up -d`.
#  10. Poll healthchecks with a timeout. On failure: auto-rollback (restore
#      previous MEMCLAW_VERSION, `compose up -d`), report which service
#      broke, exit non-zero.
#
# Exit codes:
#   0  success
#   1  preflight fail
#   2  missing required input / unparseable flag
#   3  docker pull/build/up failed
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
if [ -z "$_reexec_guard" ] \
   && { [ "${BASH_SOURCE[0]:-$0}" = "bash" ] \
        || [ "${BASH_SOURCE[0]:-$0}" = "-bash" ] \
        || [ ! -r "${BASH_SOURCE[0]:-$0}" ]; }; then
  _src="${MEMCLAW_UPGRADE_URL:-https://onprem.caura.ai/upgrade.sh}"
  _src="${CAURA_UPGRADE_URL:-$_src}"
  _tmp=$(mktemp /tmp/memclaw-upgrade.XXXXXX.sh)
  if ! curl -fsSL "$_src" -o "$_tmp"; then
    echo "ERROR: failed to download $_src for local re-exec" >&2
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
MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
TARGET_VERSION=""                  # --to, or auto-resolved from :latest
DRY_RUN="false"
SKIP_BACKUP="false"
ASSUME_YES="${MEMCLAW_YES:-false}"  # -y / --yes, or MEMCLAW_YES=1
ASSUME_YES="${CAURA_YES:-$ASSUME_YES}"
HEALTH_TIMEOUT_S=180
BUNDLE_URL="${MEMCLAW_BUNDLE_URL:-https://onprem.caura.ai/bundle.tar.gz}"
BUNDLE_URL="${CAURA_BUNDLE_URL:-$BUNDLE_URL}"

# Services we expect to find running and re-verify post-upgrade.
# Keep in sync with docker-compose.yml.
SERVICES=(
  platform-storage-api
  platform-auth-api
  platform-admin-api
  platform-audit-api
  core-storage-api
  core-api
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
  # Keep behaviour simple: MEMCLAW_VERSION=latest in .env is legal but
  # customer-facing upgrade is always explicit. Refuse if --to is missing.
  die "--to <version> is required (e.g. --to v1.0.0-rc2). Use 'latest' to pin to the floating tag." 2
}

# One key out of .env. Prints empty and returns 0 when the key is missing, so a
# caller can use it under `set -euo pipefail` — same contract as _GET below,
# which this predates in the file only because current_version() needs it here.
_env_key() {
  local _envfile="$MEMCLAW_HOME/.env"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  grep -E "^$1=" "$_envfile" 2>/dev/null \
    | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true
}

current_version() {
  # The tag this install is pinned to, from .env, under either spelling.
  # Empty → never installed / unknown.
  #
  # FIRST NON-EMPTY, not first present, and this is the site where that matters
  # most: .env is hand-edited by operators (docs/upgrade.md tells them to sed it
  # directly), so a file carrying a blank CAURA_VERSION= from a newer template
  # beside the filled old-name key that is actually driving the stack is the
  # ordinary half-migrated state. Reading the blank one makes upgrade.sh refuse
  # a healthy install with "nothing to upgrade from".
  local v
  v=$(_env_key CAURA_VERSION)
  [ -n "$v" ] || v=$(_env_key MEMCLAW_VERSION)  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  printf '%s' "$v"
}

# ── Parse flags ─────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --to)         TARGET_VERSION="$2";       shift 2 ;;
    --dry-run)    DRY_RUN="true";            shift   ;;
    --no-backup)  SKIP_BACKUP="true";        shift   ;;
    -y|--yes)     ASSUME_YES="true";         shift   ;;
    --memclaw-home) MEMCLAW_HOME="$2";       shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT_S="$2"; shift 2 ;;
    --bundle-url) BUNDLE_URL="$2";           shift 2 ;;
    -h|--help)
      sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0" | head -n 45
      exit 0 ;;
    *) die "Unknown flag: $1" 2 ;;
  esac
done

# ── Preflight ───────────────────────────────────────────────────────────────
log "Preflight checks"

[ -d "$MEMCLAW_HOME" ] || die "No install found at $MEMCLAW_HOME. Run install.sh first." 1
[ -f "$MEMCLAW_HOME/docker-compose.yml" ] || die "$MEMCLAW_HOME/docker-compose.yml missing" 1
[ -f "$MEMCLAW_HOME/.env" ] || die "$MEMCLAW_HOME/.env missing" 1

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
[ -n "$FROM_VERSION" ] || die "MEMCLAW_VERSION not set in $MEMCLAW_HOME/.env — nothing to upgrade from." 1
TO_VERSION=$(resolve_target_version)

if [ "$FROM_VERSION" = "$TO_VERSION" ]; then
  log "Already at $TO_VERSION. Nothing to do."
  exit 0
fi

# Rough disk check — pg_dump + new images easily eat 10 GB.
DISK_GB=$(df -BG "$MEMCLAW_HOME" | awk 'NR==2 {sub("G","",$4); print $4}')
[ "${DISK_GB:-0}" -ge 5 ] \
  || die "Only ${DISK_GB}G free at $MEMCLAW_HOME — need at least 5G for backup + new images." 1

# ── Summary ─────────────────────────────────────────────────────────────────
cat <<EOF

──────────────────────────────────────────
  MemClaw upgrade plan
──────────────────────────────────────────
  home:         $MEMCLAW_HOME
  from:         $FROM_VERSION
  to:           $TO_VERSION
  bundle:       $BUNDLE_URL
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

cd "$MEMCLAW_HOME"

# Reconstruct the same -f overlays install.sh chose, so upgrade preserves
# the customer's TLS / embedder / airgap selections instead of silently
# downgrading to the bare docker-compose.yml. Read flags from .env:
#   MEMCLAW_TLS_MODE=letsencrypt → -f docker-compose.tls-letsencrypt.yml
#   EMBEDDING_PROVIDER=local + no remote keys → -f docker-compose.embedder.yml
COMPOSE_FILES=(-f docker-compose.yml)
# Read $1 from .env; print empty + return 0 when the key is missing,
# so callers can use `_GET FOO` under `set -euo pipefail` without the
# script aborting on a no-match. Without `|| true`, grep's exit 1
# propagates through pipefail and aborts the whole upgrade — which is
# exactly what bit upgrades from older rc's that pre-date the
# MEMCLAW_TLS_MODE / EMBEDDING_PROVIDER env keys.
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
if [ "$_EMBED_PROVIDER" = "local" ] \
   && [ -z "$_OPENAI_KEY" ] && [ -z "$_PLATFORM_EMBED_KEY" ] \
   && [ -f docker-compose.embedder.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.embedder.yml)
fi
log "Compose overlays: ${COMPOSE_FILES[*]}"

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
       >"$BACKUP_PATH" 2>/tmp/memclaw-pgdump.err; then
    warn "pg_dump failed — see /tmp/memclaw-pgdump.err"
    rm -f "$BACKUP_PATH"
    die "Refusing to continue without a backup. Pass --no-backup to override." 6
  fi
  SIZE=$(du -h "$BACKUP_PATH" | cut -f1)
  log "DB snapshot captured ($SIZE)"
fi

# Remember where to roll back to.
echo "$FROM_VERSION" > .memclaw-prev-version

# ── Refresh bundle + update .env ────────────────────────────────────────────
log "Refreshing bundle (compose / nginx / scripts) from $BUNDLE_URL"
if ! curl -fsSL "$BUNDLE_URL" | tar -xz -C . ; then
  die "Failed to fetch bundle from $BUNDLE_URL" 3
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
  warn "Rolling back .env → $FROM_VERSION (cause: $why)"
  # Both keys, for the reason the bump above spells out — and it bites harder
  # on this path: a rollback that moves only one of them leaves the stack
  # pinned to the version we were rolling AWAY from, reported as recovered.
  _rewrite_env_key MEMCLAW_VERSION "$FROM_VERSION" || true  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _rewrite_env_key CAURA_VERSION "$FROM_VERSION" || true
}

_rollback() {
  # Full rollback: flip .env, up -d on old tag, best-effort restore backup.
  local why="$1"
  warn "Rolling back to $FROM_VERSION (cause: $why)"
  _rewrite_env_key MEMCLAW_VERSION "$FROM_VERSION" || true  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _rewrite_env_key CAURA_VERSION "$FROM_VERSION" || true
  if ! docker compose "${COMPOSE_FILES[@]}" up -d; then
    warn "compose up -d during rollback ALSO failed — manual recovery needed."
    warn "Backup (if taken): $MEMCLAW_HOME/$BACKUP_PATH"
    exit 5
  fi
  if [ -n "$BACKUP_PATH" ]; then
    warn "DB schema unchanged (we didn't run migrations yet). Backup kept for safety at $MEMCLAW_HOME/$BACKUP_PATH"
  fi
  exit 4
}

# ── Pull + build + up ───────────────────────────────────────────────────────
log "Pulling images at :${TO_VERSION}"
if ! docker compose "${COMPOSE_FILES[@]}" pull --ignore-buildable 2>/dev/null; then
  warn "pull --ignore-buildable unsupported — retrying plain pull"
  docker compose "${COMPOSE_FILES[@]}" pull || {
    warn "compose pull failed — rolling back"
    _rollback_pre_up "pull_failed"
    exit 3
  }
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
cat <<EOF

──────────────────────────────────────────
  Upgrade complete
──────────────────────────────────────────
  $FROM_VERSION → $TO_VERSION
  All services healthy.
  DB backup: ${BACKUP_PATH:-skipped}
  Rollback: sudo $MEMCLAW_HOME/scripts/rollback.sh
──────────────────────────────────────────

EOF
