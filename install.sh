#!/usr/bin/env bash
# MemClaw Enterprise — on-prem installer.
#
# Usage:
#   curl -sL https://onprem.caura.ai/install.sh | bash          (zero-config)
#   ./install.sh                                                 (zero-config)
#   ./install.sh --config /etc/memclaw/install.conf --non-interactive  (silent)
#   ./install.sh --hostname ... --admin-email ... --license ... --non-interactive
#
# Precedence (highest wins): CLI flags > env vars (CAURA_*) > --config file
# > defaults. Neither mode prompts — the one-liner auto-generates secrets
# and hands off to /setup (web wizard or memclawctl), silent demands
# everything upfront.
#
# Exit codes: 0 success · 1 preflight fail · 2 missing required input in
# non-interactive mode · 3 docker pull/up failure · 4 license invalid
# · 5 setup/admin creation failed.

set -euo pipefail

# shellcheck disable=SC2034  # VERSION is a script-level marker for ops; not consumed by code
VERSION="1.0.0"

# ── Defaults ────────────────────────────────────────────────────────────────
#
# Every knob below is read under both spellings: the historical one resolves
# first, exactly as it always has, and the CAURA_* name then overrides it WHEN
# IT IS NON-EMPTY. Old names keep working forever (rule 3); nothing here is
# renamed.
#
# FIRST NON-EMPTY, NEVER FIRST DEFINED — and the difference is the whole point.
# These names land in install.conf and .env, files a customer hand-edits, so the
# ordinary half-migrated state is a new name PRESENT AND BLANK beside an old one
# that still holds the value. A first-defined resolution reads that blank as an
# answer; `:-` treats it as absent and falls through, which is the only correct
# reading of a template somebody has started filling in. Several consumers below
# treat an empty value as "skip this" rather than "refuse" — see the notes at
# the embedding-provider auto-flip and the bundle-dir probe — so resolving to ""
# would silently disable a check rather than fail loudly.
#
# Both spellings are written out in full at every site rather than built from a
# shared suffix: the old names have to stay greppable, because grepping for them
# is how this migration is tracked.
# Defaults for these six are NOT applied here — see "Apply defaults" below.
MEMCLAW_HOME="${MEMCLAW_HOME:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
CONFIG_FILE=""
NON_INTERACTIVE="false"
OFFLINE="${MEMCLAW_OFFLINE:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
OFFLINE="${CAURA_OFFLINE:-$OFFLINE}"
SKIP_ADMIN="${MEMCLAW_SKIP_ADMIN:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
SKIP_ADMIN="${CAURA_SKIP_ADMIN:-$SKIP_ADMIN}"

HOSTNAME="${MEMCLAW_HOSTNAME:-}"
HOSTNAME="${CAURA_HOSTNAME:-$HOSTNAME}"
ADMIN_EMAIL="${MEMCLAW_ADMIN_EMAIL:-}"
ADMIN_EMAIL="${CAURA_ADMIN_EMAIL:-$ADMIN_EMAIL}"
ADMIN_PASSWORD="${MEMCLAW_ADMIN_PASSWORD:-}"
ADMIN_PASSWORD="${CAURA_ADMIN_PASSWORD:-$ADMIN_PASSWORD}"
ADMIN_PASSWORD_FILE="${MEMCLAW_ADMIN_PASSWORD_FILE:-}"
ADMIN_PASSWORD_FILE="${CAURA_ADMIN_PASSWORD_FILE:-$ADMIN_PASSWORD_FILE}"
LICENSE_PATH="${MEMCLAW_LICENSE:-}"
LICENSE_PATH="${CAURA_LICENSE:-$LICENSE_PATH}"
LICENSE_URL="${MEMCLAW_LICENSE_URL:-}"
LICENSE_URL="${CAURA_LICENSE_URL:-$LICENSE_URL}"
JWT_SECRET_FILE="${MEMCLAW_JWT_SECRET_FILE:-}"
JWT_SECRET_FILE="${CAURA_JWT_SECRET_FILE:-$JWT_SECRET_FILE}"
POSTGRES_PASSWORD_FILE="${MEMCLAW_POSTGRES_PASSWORD_FILE:-}"
POSTGRES_PASSWORD_FILE="${CAURA_POSTGRES_PASSWORD_FILE:-$POSTGRES_PASSWORD_FILE}"
CORE_ADMIN_API_KEY_FILE="${MEMCLAW_CORE_ADMIN_API_KEY_FILE:-}"
CORE_ADMIN_API_KEY_FILE="${CAURA_CORE_ADMIN_API_KEY_FILE:-$CORE_ADMIN_API_KEY_FILE}"
OPENAI_API_KEY_FILE="${MEMCLAW_OPENAI_API_KEY_FILE:-}"
OPENAI_API_KEY_FILE="${CAURA_OPENAI_API_KEY_FILE:-$OPENAI_API_KEY_FILE}"
# External Postgres (blank = use the bundled `postgres` service). Set these
# to point at a managed/external instance (RDS, Cloud SQL, AlloyDB, etc.).
# The external DB must have the pgvector extension available + a user with
# CREATE privileges. See docs/database.md.
POSTGRES_HOST="${MEMCLAW_POSTGRES_HOST:-}"
POSTGRES_HOST="${CAURA_POSTGRES_HOST:-$POSTGRES_HOST}"
POSTGRES_PORT="${MEMCLAW_POSTGRES_PORT:-}"
POSTGRES_PORT="${CAURA_POSTGRES_PORT:-$POSTGRES_PORT}"
POSTGRES_USER="${MEMCLAW_POSTGRES_USER:-}"
POSTGRES_USER="${CAURA_POSTGRES_USER:-$POSTGRES_USER}"
POSTGRES_DB="${MEMCLAW_POSTGRES_DB:-}"
POSTGRES_DB="${CAURA_POSTGRES_DB:-$POSTGRES_DB}"
POSTGRES_REQUIRE_SSL="${MEMCLAW_POSTGRES_REQUIRE_SSL:-}"
POSTGRES_REQUIRE_SSL="${CAURA_POSTGRES_REQUIRE_SSL:-$POSTGRES_REQUIRE_SSL}"
LLM_PROVIDER="${MEMCLAW_LLM_PROVIDER:-}"
LLM_PROVIDER="${CAURA_LLM_PROVIDER:-$LLM_PROVIDER}"
EMAIL_PROVIDER="${MEMCLAW_EMAIL_PROVIDER:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
EMAIL_PROVIDER="${CAURA_EMAIL_PROVIDER:-$EMAIL_PROVIDER}"
EMBEDDING_PROVIDER="${MEMCLAW_EMBEDDING_PROVIDER:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
EMBEDDING_PROVIDER="${CAURA_EMBEDDING_PROVIDER:-$EMBEDDING_PROVIDER}"
MEMCLAW_VERSION="${MEMCLAW_VERSION:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
MEMCLAW_VERSION="${CAURA_VERSION:-$MEMCLAW_VERSION}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working

# TLS — four modes:
#   "self-signed" (default!) — we generate a 10y RSA-2048 cert on first
#       install with openssl. Idempotent across re-runs.
#   "byo" — customer passes --tls-cert + --tls-key (corporate CA, etc.).
#   "letsencrypt" — Caddy sidecar handles ACME issuance + renewal. Needs
#       --tls-domain (publicly resolvable FQDN) + --tls-email. Port 80
#       must be reachable from the internet for HTTP-01 challenge.
#   "" (empty) — HTTP-only. Only reachable by passing
#       --acknowledge-insecure; install.sh refuses otherwise.
TLS_MODE="${MEMCLAW_TLS_MODE:-self-signed}"
TLS_MODE="${CAURA_TLS_MODE:-$TLS_MODE}"
TLS_CERT_FILE="${MEMCLAW_TLS_CERT_FILE:-}"
TLS_CERT_FILE="${CAURA_TLS_CERT_FILE:-$TLS_CERT_FILE}"
TLS_KEY_FILE="${MEMCLAW_TLS_KEY_FILE:-}"
TLS_KEY_FILE="${CAURA_TLS_KEY_FILE:-$TLS_KEY_FILE}"
TLS_DOMAIN="${MEMCLAW_TLS_DOMAIN:-}"
TLS_DOMAIN="${CAURA_TLS_DOMAIN:-$TLS_DOMAIN}"
TLS_EMAIL="${MEMCLAW_TLS_EMAIL:-}"
TLS_EMAIL="${CAURA_TLS_EMAIL:-$TLS_EMAIL}"
ACK_INSECURE="${MEMCLAW_ACK_INSECURE:-false}"
ACK_INSECURE="${CAURA_ACK_INSECURE:-$ACK_INSECURE}"
BIND_ADDRESS="${MEMCLAW_BIND_ADDRESS:-0.0.0.0}"
BIND_ADDRESS="${CAURA_BIND_ADDRESS:-$BIND_ADDRESS}"

# ── Helpers ─────────────────────────────────────────────────────────────────
log()   { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
# "$1", not "$*". Every call site passes the exit code as $2, and "$*" joined it
# into the printed text — "ERROR Unknown flag: --bogus 2". warn() above keeps
# "$*" on purpose: it takes no exit code, and one call passes three separate
# message arguments.
die()   { printf '\033[31mERROR\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

read_file() { [ -n "${1:-}" ] && [ -f "$1" ] && cat "$1"; }
random_hex() { head -c "$1" /dev/urandom | xxd -p -c "$1"; }

# ── Parse CLI flags ────────────────────────────────────────────────────────
usage() {
  sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0" | head -n 40
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config)                    CONFIG_FILE="$2";              shift 2 ;;
    --non-interactive|-y)        NON_INTERACTIVE="true";        shift   ;;
    --offline)                   OFFLINE="true";                shift   ;;
    --skip-admin)                SKIP_ADMIN="true";             shift   ;;
    --hostname)                  HOSTNAME="$2";                 shift 2 ;;
    --admin-email)               ADMIN_EMAIL="$2";              shift 2 ;;
    --admin-password)            ADMIN_PASSWORD="$2";           shift 2 ;;
    --admin-password-file)       ADMIN_PASSWORD_FILE="$2";      shift 2 ;;
    --license)                   LICENSE_PATH="$2";             shift 2 ;;
    --license-url)               LICENSE_URL="$2";              shift 2 ;;
    --llm-provider)              LLM_PROVIDER="$2";             shift 2 ;;
    --openai-api-key-file)       OPENAI_API_KEY_FILE="$2";      shift 2 ;;
    --postgres-host)             POSTGRES_HOST="$2";            shift 2 ;;
    --postgres-port)             POSTGRES_PORT="$2";            shift 2 ;;
    --postgres-user)             POSTGRES_USER="$2";            shift 2 ;;
    --postgres-db)               POSTGRES_DB="$2";              shift 2 ;;
    --postgres-require-ssl)      POSTGRES_REQUIRE_SSL="true";   shift   ;;
    --email-provider)            EMAIL_PROVIDER="$2";           shift 2 ;;
    --embedding-provider)        EMBEDDING_PROVIDER="$2";       shift 2 ;;
    --memclaw-home)              MEMCLAW_HOME="$2";             shift 2 ;;
    --version)                   MEMCLAW_VERSION="$2";          shift 2 ;;
    --tls-self-signed)           TLS_MODE="self-signed";        shift   ;;
    --tls-cert)                  TLS_CERT_FILE="$2"; TLS_MODE="byo"; shift 2 ;;
    --tls-key)                   TLS_KEY_FILE="$2";             shift 2 ;;
    --tls-domain)                TLS_DOMAIN="$2";               shift 2 ;;
    --tls-email)                 TLS_EMAIL="$2";                shift 2 ;;
    --tls-letsencrypt)           TLS_MODE="letsencrypt";        shift   ;;
    --bind-address)              BIND_ADDRESS="$2";             shift 2 ;;
    --acknowledge-insecure|--no-tls|--insecure-http)
        TLS_MODE=""; ACK_INSECURE="true";                       shift   ;;
    -h|--help)                   usage ;;
    *) die "Unknown flag: $1" 2 ;;
  esac
done

# ── Apply config file (lower precedence than CLI/env) ──────────────────────
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || die "Config file not found: $CONFIG_FILE" 2
  log "Loading $CONFIG_FILE"
  # Shell-style parser: ignores lines starting with # or empty, strips
  # surrounding quotes on values. Intentionally minimal — not a TOML parser.
  #
  # THE FILTER. This `case` is a whitelist: a key it does not name is dropped
  # silently, with no warning and no error. So the two branded keys need their
  # caura_* spellings added HERE as well as wherever the value is used — a
  # reader added downstream alone would accept the new key everywhere and read
  # it nowhere, and the config file would look migrated while behaving as if
  # the line were absent.
  #
  # The two new keys collect into temporaries and resolve after the loop rather
  # than assigning as they are seen. Two reasons, and the second is the one that
  # matters: assigning in-loop makes the answer depend on which spelling appears
  # LOWER in the file, and a blank `caura_home =` sitting below a filled old-name
  # key would then win and blank the value. Resolving afterwards is
  # order-independent and first-non-empty in both directions.
  _conf_caura_home=""
  _conf_memclaw_home=""  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _conf_caura_version=""
  _conf_memclaw_version=""  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  while IFS='=' read -r key raw; do
    key="${key// /}"; raw="${raw#"${raw%%[![:space:]]*}"}"
    # Strip a trailing comment, then the surrounding quotes. A quoted value ends
    # at its closing quote and anything past it is commentary; an unquoted one
    # ends at the first whitespace-preceded '#'.
    #
    # Doing this at all is a fix, not a refinement. The previous form removed
    # only a quote at the very END of the line, so any line carrying a trailing
    # comment kept the comment inside the value — and the install.conf.example
    # this repo ships has always had six of them. A silent install from the
    # shipped template resolved its version to
    # `v1.0.0"                        # pin for reproducibility`
    # and wrote that into .env as the image tag. Same for email_provider,
    # llm_provider, embedding_provider, offline and skip_admin.
    case "$raw" in
      '"'*) raw="${raw#\"}"; raw="${raw%%\"*}" ;;
      *)    raw="${raw%%[[:space:]]#*}"; raw="${raw%"${raw##*[![:space:]]}"}" ;;
    esac
    case "$key" in
      ""|\#*) continue ;;
      hostname)                    [ -z "$HOSTNAME" ]                  && HOSTNAME="$raw" ;;
      admin_email)                 [ -z "$ADMIN_EMAIL" ]               && ADMIN_EMAIL="$raw" ;;
      admin_password)              [ -z "$ADMIN_PASSWORD" ]            && ADMIN_PASSWORD="$raw" ;;
      admin_password_file)         [ -z "$ADMIN_PASSWORD_FILE" ]       && ADMIN_PASSWORD_FILE="$raw" ;;
      license)                     [ -z "$LICENSE_PATH" ]              && LICENSE_PATH="$raw" ;;
      license_url)                 [ -z "$LICENSE_URL" ]               && LICENSE_URL="$raw" ;;
      llm_provider)                [ -z "$LLM_PROVIDER" ]              && LLM_PROVIDER="$raw" ;;
      openai_api_key_file)         [ -z "$OPENAI_API_KEY_FILE" ]       && OPENAI_API_KEY_FILE="$raw" ;;
      postgres_host)               [ -z "$POSTGRES_HOST" ]             && POSTGRES_HOST="$raw" ;;
      postgres_port)               [ -z "$POSTGRES_PORT" ]             && POSTGRES_PORT="$raw" ;;
      postgres_user)               [ -z "$POSTGRES_USER" ]             && POSTGRES_USER="$raw" ;;
      postgres_db)                 [ -z "$POSTGRES_DB" ]               && POSTGRES_DB="$raw" ;;
      postgres_require_ssl)        [ -z "$POSTGRES_REQUIRE_SSL" ]      && POSTGRES_REQUIRE_SSL="$raw" ;;
      email_provider)              [ -z "$EMAIL_PROVIDER" ]            && EMAIL_PROVIDER="$raw" ;;
      embedding_provider)          [ -z "$EMBEDDING_PROVIDER" ]        && EMBEDDING_PROVIDER="$raw" ;;
      jwt_secret_file)             [ -z "$JWT_SECRET_FILE" ]           && JWT_SECRET_FILE="$raw" ;;
      postgres_password_file)      [ -z "$POSTGRES_PASSWORD_FILE" ]    && POSTGRES_PASSWORD_FILE="$raw" ;;
      core_admin_api_key_file)     [ -z "$CORE_ADMIN_API_KEY_FILE" ]   && CORE_ADMIN_API_KEY_FILE="$raw" ;;
      memclaw_home)                _conf_memclaw_home="$raw" ;;      # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
      caura_home)                  _conf_caura_home="$raw" ;;
      memclaw_version)             _conf_memclaw_version="$raw" ;;   # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
      caura_version)               _conf_caura_version="$raw" ;;
      offline)                     [ -z "$OFFLINE" ]                   && OFFLINE="$raw" ;;
      skip_admin)                  [ -z "$SKIP_ADMIN" ]                && SKIP_ADMIN="$raw" ;;
    esac
  done < "$CONFIG_FILE"

  # First non-empty of the two spellings, then applied only if it is non-empty.
  # The outer guard is what keeps a blank key from clobbering a value the
  # environment or a CLI flag already supplied: a home key with nothing after
  # the `=` is a half-filled template, not an instruction to install into "".
  _conf_home="${_conf_caura_home:-$_conf_memclaw_home}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  [ -n "$_conf_home" ] && [ -z "$MEMCLAW_HOME" ] && MEMCLAW_HOME="$_conf_home"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  _conf_version="${_conf_caura_version:-$_conf_memclaw_version}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
  [ -n "$_conf_version" ] && [ -z "$MEMCLAW_VERSION" ] && MEMCLAW_VERSION="$_conf_version"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
fi

# ── Apply defaults ─────────────────────────────────────────────────────────
#
# Last, so the documented order is the order the code runs in:
#
#     CLI flags  >  environment variables  >  --config file  >  defaults
#
# These six carry a NON-EMPTY default, and that is why they used to sit above
# and why the config file used to beat a flag. The other thirteen keys default
# to "", so `[ -z "$VAR" ]` in the config block reads as "nothing higher set
# this" and the file correctly fills the gap. For these six the same guard was
# never true — the install root already held its default by then — so the arms
# were left unguarded, and an unguarded arm overwrites a flag.
#
# The obvious repair, copying `[ -z "$VAR" ]` onto the remaining arms, silently
# does the opposite: with the default already applied the guard never fires and
# the key stops working from the config file at all.
#
# So the defaults move here instead. Every key is now empty until something
# actually sets it, one guard is correct for all twenty-two, and the config file
# fills only what nothing above it supplied. HOSTNAME has always worked this way
# (its default lands further down, next to the generated secrets); this is that
# pattern applied to the rest.
_EMBEDDING_PROVIDER_CHOSEN="$EMBEDDING_PROVIDER"   # before the default lands
MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"  # legacy-name-ok: pinned floor string, and the install root default
MEMCLAW_VERSION="${MEMCLAW_VERSION:-v2.8.4}"  # legacy-name-ok: the shipped release pin, unchanged
OFFLINE="${OFFLINE:-false}"
SKIP_ADMIN="${SKIP_ADMIN:-false}"
EMAIL_PROVIDER="${EMAIL_PROVIDER:-log}"
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-local}"

# Re-export in MEMCLAW_* form so sudo -E preserves them into the child.
# The child re-parses "$@" anyway (which is the primary mechanism), but
# this makes the handoff robust against wrappers that sanitise argv
# (some CI runners, IDE terminals).
export MEMCLAW_HOME MEMCLAW_VERSION
export MEMCLAW_OFFLINE="$OFFLINE" MEMCLAW_SKIP_ADMIN="$SKIP_ADMIN"
export MEMCLAW_HOSTNAME="$HOSTNAME" MEMCLAW_ADMIN_EMAIL="$ADMIN_EMAIL"
export MEMCLAW_ADMIN_PASSWORD="$ADMIN_PASSWORD" MEMCLAW_ADMIN_PASSWORD_FILE="$ADMIN_PASSWORD_FILE"
export MEMCLAW_LICENSE="$LICENSE_PATH" MEMCLAW_LICENSE_URL="$LICENSE_URL"
# And the same resolved values under the CAURA_* spelling, so the two agree in
# the child. Without this the handoff silently inverts the documented
# precedence: a CAURA_* value inherited through `sudo -E` still holds whatever
# the caller's environment had, while its old-name twin carries the value a
# CLI flag or the config file just resolved — and the child, reading the new
# name first, would take the stale one. Only reachable when argv IS sanitised
# (otherwise the child re-parses the flag and corrects itself), which is exactly
# the case this export block exists for.
export CAURA_HOME="$MEMCLAW_HOME" CAURA_VERSION="$MEMCLAW_VERSION"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
export CAURA_OFFLINE="$OFFLINE" CAURA_SKIP_ADMIN="$SKIP_ADMIN"
export CAURA_HOSTNAME="$HOSTNAME" CAURA_ADMIN_EMAIL="$ADMIN_EMAIL"
export CAURA_ADMIN_PASSWORD="$ADMIN_PASSWORD" CAURA_ADMIN_PASSWORD_FILE="$ADMIN_PASSWORD_FILE"
export CAURA_LICENSE="$LICENSE_PATH" CAURA_LICENSE_URL="$LICENSE_URL"

# ── Preflight ──────────────────────────────────────────────────────────────
log "Preflight checks"
command -v docker >/dev/null || die "Docker ≥ 24 required (not found on PATH)" 1

# CLI on PATH isn't enough — we need to actually reach dockerd. When the user
# isn't root and not in the docker group, re-exec via sudo rather than failing.
if ! docker info >/dev/null 2>&1; then
  # Detect stdin mode (e.g. `curl | bash`): $0 is the shell binary rather
  # than a readable script file. We can't `exec sudo -E bash "$0"` in that
  # case — that tries to execute the bash binary as a script. Tell the user
  # to prefix with sudo instead of attempting a broken re-exec.
  if [ ! -f "$0" ] || ! [ -r "$0" ]; then
    die "Cannot reach Docker daemon, and install.sh is running via stdin (curl|bash) — can't self-sudo. Re-run as: curl -sL https://onprem.caura.ai/install.sh | sudo bash" 1
  fi
  if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null; then
    warn "Cannot reach Docker daemon as $(whoami). Re-executing via sudo."
    # -E preserves the full env; argv is still passed so child re-parses
    # CLI flags too — belt + suspenders for the handoff.
    exec sudo -E bash "$0" "$@"
  fi
  die "Cannot reach Docker daemon. Either run as root, add yourself to the docker group (and re-login), or install sudo." 1
fi

docker compose version >/dev/null 2>&1 || die "docker compose v2 required" 1

# Version gate runs after the dockerd check so it always has a real answer.
DOCKER_MAJOR=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1 || echo 0)
[ "${DOCKER_MAJOR:-0}" -ge 24 ] || warn "Docker version < 24 detected (got '${DOCKER_MAJOR}') — proceed at your own risk"

# RAM + disk (best effort; skip on non-Linux)
if [ -r /proc/meminfo ]; then
  KB=$(awk '/MemTotal/{print $2}' /proc/meminfo)
  GB=$(( KB / 1024 / 1024 ))
  [ "$GB" -ge 7 ] || warn "Only ${GB}G RAM detected (8G recommended)"
fi
DISK_GB=$(df -BG "$PWD" | awk 'NR==2 {sub("G","",$4); print $4}')
[ "${DISK_GB:-0}" -ge 40 ] || warn "Only ${DISK_GB}G free (50G recommended)"

# ── Silent-mode input validation ────────────────────────────────────────────
if [ "$NON_INTERACTIVE" = "true" ]; then
  missing=""
  [ -z "$HOSTNAME" ]                           && missing="$missing hostname"
  [ -z "$LICENSE_PATH$LICENSE_URL" ]            && missing="$missing license"
  if [ "$SKIP_ADMIN" != "true" ]; then
    [ -z "$ADMIN_EMAIL" ] && missing="$missing admin_email"
    [ -z "$ADMIN_PASSWORD$ADMIN_PASSWORD_FILE" ] && missing="$missing admin_password"
  fi
  [ -z "$missing" ] || die "Non-interactive mode missing required fields:${missing}" 2
fi

# ── Resolve / generate secrets ──────────────────────────────────────────────
HOSTNAME="${HOSTNAME:-memclaw.local}"

JWT_SECRET=$(read_file "$JWT_SECRET_FILE" || true)
[ -n "${JWT_SECRET:-}" ] || JWT_SECRET=$(random_hex 32)

POSTGRES_PASSWORD=$(read_file "$POSTGRES_PASSWORD_FILE" || true)
[ -n "${POSTGRES_PASSWORD:-}" ] || POSTGRES_PASSWORD=$(random_hex 16)

CORE_ADMIN_API_KEY=$(read_file "$CORE_ADMIN_API_KEY_FILE" || true)
[ -n "${CORE_ADMIN_API_KEY:-}" ] || CORE_ADMIN_API_KEY="mc_admin_$(random_hex 24)"

# Shared secret presented by platform-operations as ``X-Internal-Token`` on
# its cron fanout POSTs (session-cleanup, org-hard-delete-sweep); platform-
# admin-api gates those endpoints on the matching value. Must be identical
# on both services — both read the single .env var below.
PLATFORM_OPERATIONS_INTERNAL_TOKEN="${PLATFORM_OPERATIONS_INTERNAL_TOKEN:-}"
[ -n "${PLATFORM_OPERATIONS_INTERNAL_TOKEN:-}" ] || PLATFORM_OPERATIONS_INTERNAL_TOKEN="mc_opstok_$(random_hex 24)"

# Fernet key for core-api settings encryption. Required when
# ENVIRONMENT=production; no sensible default. Generate one if the
# caller didn't supply it.
if [ -z "${SETTINGS_ENCRYPTION_KEY:-}" ]; then
  if command -v python3 >/dev/null; then
    SETTINGS_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || true)
  fi
  # Fallback: 32 random bytes base64url (same shape as Fernet)
  [ -n "${SETTINGS_ENCRYPTION_KEY:-}" ] || SETTINGS_ENCRYPTION_KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')
fi

OPENAI_API_KEY=$(read_file "$OPENAI_API_KEY_FILE" || true)

# Auto-flip the default EMBEDDING_PROVIDER=local to "openai" when the
# customer provides an OpenAI key without an explicit --embedding-provider.
# Otherwise the slim core-api image boots with provider="local" but no
# sentence-transformers installed, so every write throws ImportError and
# semantic search returns zero hits. The fatter embedder image is only
# engaged when we're actually using local — see LOCAL_EMBEDDINGS below.
#
# The third test asks "did the operator pick a provider themselves?", and it now
# asks it of every source rather than of the environment alone.
# _EMBEDDING_PROVIDER_CHOSEN is the value as it stood immediately before the
# default was applied, so it is non-empty exactly when an env var, a
# --embedding-provider flag or an install.conf key supplied one.
#
# Testing the environment was too narrow in two directions. It read an explicit
# CAURA_EMBEDDING_PROVIDER=local as unset, which the dual-read fixed; it also
# ignored --embedding-provider and the config file entirely, so an operator who
# asked for local embeddings on the command line and happened to supply an
# OpenAI key had that choice overwritten and their key spent.
if [ -n "${OPENAI_API_KEY:-}" ] \
   && [ "${EMBEDDING_PROVIDER}" = "local" ] \
   && [ -z "$_EMBEDDING_PROVIDER_CHOSEN" ]; then
  EMBEDDING_PROVIDER="openai"
  log "OPENAI_API_KEY provided; setting EMBEDDING_PROVIDER=openai (override with --embedding-provider)."
fi

ADMIN_PASSWORD_RESOLVED="$ADMIN_PASSWORD"
if [ -z "$ADMIN_PASSWORD_RESOLVED" ] && [ -n "$ADMIN_PASSWORD_FILE" ]; then
  ADMIN_PASSWORD_RESOLVED=$(read_file "$ADMIN_PASSWORD_FILE" || true)
fi

# ── Stage MEMCLAW_HOME ─────────────────────────────────────────────────────
mkdir -p "$MEMCLAW_HOME"/{license,nginx,scripts,backups}

# License dir is writable by the container user so the first-run wizard's
# POST /api/setup/license can drop license.key into the mount. Install.sh
# runs as root (via the sudo re-exec), so the dir is root-owned by
# default; the platform-auth-api container runs as appuser (UID 1000).
# Without world-write the setup POST 500s with EACCES.
#
# Sticky bit (1777, like /tmp): the dir has to be world-writable for appuser
# to create license.key, but the sticky bit stops any *other* local user from
# unlinking a file they don't own. That closes an unlink+symlink race against
# the root-owned temp file install.sh stages here on a --license install — in
# a plain 0777 dir, directory write permission alone lets any user rm root's
# temp and plant a symlink before the write lands. appuser still fully manages
# the files it owns.
#
# License authenticity is protected by the RS256 signature in the file —
# world-write on the dir only lets the container *write* the file; anything
# placed there still has to verify against the public key baked into the
# image. Same rationale as the logs dir below.
chmod 1777 "$MEMCLAW_HOME/license"

# Log sink directories — bind-mounted into each container at
# /var/log/memclaw/<service>/. The containers run as `appuser` (non-root,
# UID assigned by useradd — typically 1000 on Debian slim), so the host
# dir must be writable by them. 0777 keeps the install portable across
# whatever UID a future base-image rebuild settles on. The logs are
# customer-facing anyway (grepped from support bundles), not secrets.
for svc in platform-storage-api platform-auth-api platform-admin-api \
           platform-audit-api core-storage-api core-api gateway; do
  mkdir -p "$MEMCLAW_HOME/logs/$svc"
  chmod 0777 "$MEMCLAW_HOME/logs/$svc"
done

# Copy compose + scripts + docs from the bundle dir (wherever install.sh
# lives). Three modes:
#   1. MEMCLAW_BUNDLE_DIR explicitly set — use that.
#   2. install.sh is a real file adjacent to compose files (git clone path).
#   3. Stdin mode (curl|sudo bash) or standalone file with no adjacent
#      bundle — fetch the bundle tarball from onprem.caura.ai and extract.
SRC_DIR=""
# Resolved to one variable first, so the -n probe and the use below cannot
# disagree about which spelling won. An empty value here means "fall through to
# mode 2/3" rather than "refuse", so blank must read as absent: a blank
# CAURA_BUNDLE_DIR beside a real old-name value has to keep using the real one,
# not quietly send an air-gapped install off to fetch a tarball it has no
# network for.
BUNDLE_DIR="${MEMCLAW_BUNDLE_DIR:-}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
BUNDLE_DIR="${CAURA_BUNDLE_DIR:-$BUNDLE_DIR}"
if [ -n "$BUNDLE_DIR" ] && [ -f "$BUNDLE_DIR/docker-compose.yml" ]; then
  SRC_DIR="$BUNDLE_DIR"
elif [ -f "$0" ] && [ -r "$0" ]; then
  candidate=$(dirname "$(realpath "$0")")
  [ -f "$candidate/docker-compose.yml" ] && SRC_DIR="$candidate"
fi

if [ -z "$SRC_DIR" ]; then
  BUNDLE_URL="${MEMCLAW_BUNDLE_URL:-https://onprem.caura.ai/bundle.tar.gz}"
  BUNDLE_URL="${CAURA_BUNDLE_URL:-$BUNDLE_URL}"
  log "No adjacent bundle found; fetching $BUNDLE_URL"
  SRC_DIR=$(mktemp -d)
  curl -fsSL "$BUNDLE_URL" | tar -xz -C "$SRC_DIR" \
    || die "Failed to fetch/extract $BUNDLE_URL" 3
  # Allow the tarball to either include a top-level dir or not — normalize
  # by walking the first level of subdirectories and adopting the first one
  # that carries docker-compose.yml. The previous form (``[ -d "$SRC_DIR"/* ]``)
  # was an SC2144 trap: ``-d`` only inspects the first glob expansion, and
  # if the glob matched multiple files the test silently became undefined.
  if [ ! -f "$SRC_DIR/docker-compose.yml" ]; then
    for inner in "$SRC_DIR"/*/; do
      if [ -d "$inner" ] && [ -f "$inner/docker-compose.yml" ]; then
        SRC_DIR="${inner%/}"
        break
      fi
    done
  fi
  [ -f "$SRC_DIR/docker-compose.yml" ] || die "bundle missing docker-compose.yml after extract" 3
fi

for f in docker-compose.yml docker-compose.airgap.yml \
         docker-compose.embedder.yml docker-compose.embedder.airgap.yml \
         docker-compose.tls-letsencrypt.yml \
         .env.example install.conf.example; do
  [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$MEMCLAW_HOME/"
done
for d in nginx scripts docs license; do
  [ -d "$SRC_DIR/$d" ] && cp -Rf "$SRC_DIR/$d" "$MEMCLAW_HOME/"
done

# ── Materialize TLS certs ──────────────────────────────────────────────────
# Three paths land cert.pem + key.pem at $MEMCLAW_HOME/tls/, which is
# bind-mounted into the gateway container at /etc/nginx/tls/. The nginx
# entrypoint detects them and renders the TLS template instead of HTTP.
mkdir -p "$MEMCLAW_HOME/tls"
chmod 0755 "$MEMCLAW_HOME/tls"

case "$TLS_MODE" in
  byo)
    [ -n "$TLS_CERT_FILE" ] && [ -n "$TLS_KEY_FILE" ] \
      || die "--tls-cert requires --tls-key (and vice versa)" 2
    [ -f "$TLS_CERT_FILE" ] || die "--tls-cert: file not found: $TLS_CERT_FILE" 2
    [ -f "$TLS_KEY_FILE" ]  || die "--tls-key: file not found: $TLS_KEY_FILE" 2
    cp -f "$TLS_CERT_FILE" "$MEMCLAW_HOME/tls/cert.pem"
    cp -f "$TLS_KEY_FILE"  "$MEMCLAW_HOME/tls/key.pem"
    chmod 0644 "$MEMCLAW_HOME/tls/cert.pem"
    chmod 0600 "$MEMCLAW_HOME/tls/key.pem"
    log "TLS: bring-your-own cert installed."
    ;;
  letsencrypt)
    [ -n "$TLS_DOMAIN" ] || die "--tls-letsencrypt requires --tls-domain (publicly-resolvable FQDN)" 2
    [ -n "$TLS_EMAIL" ]  || die "--tls-letsencrypt requires --tls-email (used by the CA for renewal/expiry notices)" 2
    case "$TLS_DOMAIN" in
      localhost|127.*|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|*.local|*.internal)
        die "Let's Encrypt requires a publicly-resolvable domain — '$TLS_DOMAIN' is private. Use --tls-self-signed for private networks." 2
        ;;
    esac
    log "TLS: Let's Encrypt via Caddy sidecar (domain=$TLS_DOMAIN, email=$TLS_EMAIL)"
    mkdir -p "$MEMCLAW_HOME/caddy"
    # Caddyfile — Caddy autodetects the domain at the top of the block,
    # provisions a cert via ACME HTTP-01, renews automatically. The
    # `tls` line gives ACME the contact email. reverse_proxy points at
    # the gateway service inside the docker network (which now listens
    # only on 80 internally — host port mapping is dropped by the
    # tls-letsencrypt overlay).
    cat > "$MEMCLAW_HOME/caddy/Caddyfile" <<EOF
{
    email $TLS_EMAIL
    # Comment out for production — uncomment to test against the LE
    # staging endpoint (no rate limit; certs are not browser-trusted).
    # acme_ca https://acme-staging-v02.api.letsencrypt.org/directory
}

$TLS_DOMAIN {
    encode zstd gzip
    reverse_proxy gateway:80 {
        header_up Host {host}
        header_up X-Real-IP {remote}
        header_up X-Forwarded-For {remote}
        header_up X-Forwarded-Proto {scheme}
    }
}
EOF
    chmod 0644 "$MEMCLAW_HOME/caddy/Caddyfile"
    # Make sure no stale cert in /opt/memclaw/tls/ — would confuse the
    # gateway entrypoint into serving its own TLS instead of plain HTTP.
    rm -f "$MEMCLAW_HOME/tls/cert.pem" "$MEMCLAW_HOME/tls/key.pem"
    ;;
  self-signed)
    if [ ! -f "$MEMCLAW_HOME/tls/cert.pem" ] || [ ! -f "$MEMCLAW_HOME/tls/key.pem" ]; then
      command -v openssl >/dev/null || die "--tls-self-signed requires openssl on the host" 1
      cn="${TLS_DOMAIN:-${HOSTNAME:-memclaw.local}}"
      log "TLS: generating self-signed cert for CN=$cn (10 years, RSA-2048)"
      # SAN list covers the hostname plus localhost + the bind IP so
      # local probes / k8s liveness checks don't trip cert mismatches.
      _san="DNS:$cn,DNS:localhost,IP:127.0.0.1"
      openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -subj "/CN=$cn/O=MemClaw On-Prem" \
        -addext "subjectAltName=$_san" \
        -keyout "$MEMCLAW_HOME/tls/key.pem" \
        -out    "$MEMCLAW_HOME/tls/cert.pem" \
        2>/dev/null \
        || die "openssl failed to generate self-signed cert" 1
      chmod 0644 "$MEMCLAW_HOME/tls/cert.pem"
      chmod 0600 "$MEMCLAW_HOME/tls/key.pem"
    else
      log "TLS: reusing existing self-signed cert at $MEMCLAW_HOME/tls/"
    fi
    ;;
  "")
    : # HTTP-only; checked below
    ;;
  *)
    die "Unknown --tls-mode: $TLS_MODE (valid: self-signed, byo)" 2
    ;;
esac

# If the customer explicitly opted into HTTP (--no-tls / --insecure-http
# / --acknowledge-insecure), warn about the implications but proceed —
# they've made an informed choice. The default path is TLS, so reaching
# this branch always means a deliberate opt-out.
if [ -z "$TLS_MODE" ]; then
  if [ "$ACK_INSECURE" != "true" ]; then
    # Should be impossible (--no-tls is the only way to clear TLS_MODE),
    # but defensive: treat as a misconfiguration rather than silently
    # downgrading the customer's stack.
    die "TLS disabled but --acknowledge-insecure not set. Re-run install.sh without flags to use the self-signed default, or pass --no-tls to opt out explicitly." 1
  fi
  warn "TLS disabled (--no-tls). API keys will traverse plaintext on $BIND_ADDRESS:${HTTP_PORT:-80}."
  warn "Suitable only for: behind your own TLS terminator, fully isolated"
  warn "private networks, or local development. Production deployments"
  warn "should use --tls-self-signed (default) or --tls-cert/--tls-key."
fi

# ── Materialize license ─────────────────────────────────────────────────────
# license/ is world-writable so the first-run wizard's POST /setup/license can
# drop a key as appuser — which also lets an untrusted local user plant, or
# race in, a symlink at license/license.key. Since this installer runs as root,
# a naive cp/curl/chmod could be redirected through such a link to clobber or
# expose an arbitrary file. So when we materialize a key we stage it into a
# root-owned temp file, set its mode there, then rename it into place:
# rename(2) replaces the directory entry without following a destination
# symlink, and the temp can't be unlink+swapped from under us because the dir
# carries the sticky bit (1777, set at creation) — so only root can remove the
# root-owned temp. No check-then-use window for either step. platform-auth-api
# reads license.key as appuser (UID 1000), hence 0644; the RS256 signature, not
# file perms, is the authenticity boundary (as with the 0644 cert.pem).
#
# We deliberately do NOT chmod an already-present license.key: every writer
# leaves it readable already (this path stages 0644 as root; the wizard's
# POST /setup/license writes it appuser-owned), so there's nothing to repair —
# and chmod-ing an existing name in this world-writable dir would reintroduce a
# symlink TOCTOU for no benefit. A legacy key with bad perms is fixed by
# re-running with --license (or --license-url), which re-materializes safely.
if [ -n "$LICENSE_PATH" ] || [ -n "$LICENSE_URL" ]; then
  license_tmp=$(mktemp "$MEMCLAW_HOME/license/.license.XXXXXX") \
    || die "Could not create a temp file in license/" 4
  # The trap removes the staged temp on every exit path — a die (which calls
  # exit), normal completion, or a signal — so the guards below just die.
  # Cleared after the rename so a later unrelated exit won't rm the installed key.
  trap 'rm -f "$license_tmp"' EXIT INT TERM HUP
  if [ -n "$LICENSE_PATH" ]; then
    [ -f "$LICENSE_PATH" ] || die "License file not found: $LICENSE_PATH" 4
    cp -f "$LICENSE_PATH" "$license_tmp" \
      || die "Could not read license file: $LICENSE_PATH" 4
  else
    log "Fetching license from $LICENSE_URL"
    curl -fsSL "$LICENSE_URL" -o "$license_tmp" \
      || die "Could not fetch license from $LICENSE_URL" 4
  fi
  chmod 0644 "$license_tmp" \
    || die "Could not set permissions on staged license" 4
  mv -f "$license_tmp" "$MEMCLAW_HOME/license/license.key" \
    || die "Could not install license/license.key" 4
  trap - EXIT INT TERM HUP
fi

# Advisory only — chmod on an existing name here is TOCTOU (chmod follows symlinks).
# Warn so operators can diagnose EACCES in platform-auth-api without guessing.
if [ -f "$MEMCLAW_HOME/license/license.key" ]; then
  key_mode=$(stat -c '%a' "$MEMCLAW_HOME/license/license.key" 2>/dev/null || true)
  case "$key_mode" in
    *4|*5|*6|*7) ;;
    *) warn "license/license.key exists but is not world-readable (mode $key_mode)." \
            "platform-auth-api (appuser/UID 1000) may fail with EACCES." \
            "Re-run install.sh with --license <path> to re-materialize it safely." ;;
  esac
fi

# ── Write .env ──────────────────────────────────────────────────────────────
# Resolved here rather than in the heredoc below, which is the one place the
# two-line idiom used everywhere else cannot go: every line inside that heredoc
# is written verbatim into the customer's .env, so a second read — or the
# exemption comment marking one — would land in their file as text. The keys the
# heredoc writes are unchanged; only the value this one resolves is.
MEMCLAW_OPS_VERSION="${CAURA_OPS_VERSION:-${MEMCLAW_OPS_VERSION:-}}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
cat > "$MEMCLAW_HOME/.env" <<EOF
# Rendered by install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ). Do not edit while
# the stack is running — rerun ./install.sh to regenerate.
MEMCLAW_VERSION=${MEMCLAW_VERSION}
# Image tag for the scheduler/worker services (platform-operations, and in a
# later phase core-operations/core-worker). Normally equal to MEMCLAW_VERSION;
# kept as a separate knob so the operations sidecars can be rolled out on a
# release that the core stack hasn't moved to yet (phased on-prem parity).
MEMCLAW_OPS_VERSION=${MEMCLAW_OPS_VERSION:-${MEMCLAW_VERSION}}
PUBLIC_HOSTNAME=${HOSTNAME}
JWT_SECRET=${JWT_SECRET}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
CORE_ADMIN_API_KEY=${CORE_ADMIN_API_KEY}
PLATFORM_OPERATIONS_INTERNAL_TOKEN=${PLATFORM_OPERATIONS_INTERNAL_TOKEN}
# Database target. Blank host = bundled postgres service (default).
# Set POSTGRES_HOST to use an external instance (must have pgvector +
# a CREATE-capable user; see docs/database.md).
POSTGRES_HOST=${POSTGRES_HOST}
POSTGRES_PORT=${POSTGRES_PORT}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_REQUIRE_SSL=${POSTGRES_REQUIRE_SSL}
SETTINGS_ENCRYPTION_KEY=${SETTINGS_ENCRYPTION_KEY}
EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER}
OPENAI_API_KEY=${OPENAI_API_KEY}
EMAIL_PROVIDER=${EMAIL_PROVIDER}
# Gateway bind / ports — consumed by docker-compose.yml.
# BIND_ADDRESS=0.0.0.0 = listen on all interfaces (default).
# BIND_ADDRESS=127.0.0.1 = loopback only (use when fronted by another proxy).
BIND_ADDRESS=${BIND_ADDRESS}
HTTP_PORT=80
HTTPS_PORT=443
# TLS mode signal — read by upgrade.sh and memclawctl to know whether
# to re-apply overlays on a `docker compose up -d`. Possible values:
# self-signed, byo, letsencrypt, "" (HTTP-only).
MEMCLAW_TLS_MODE=${TLS_MODE}
MEMCLAW_TLS_DOMAIN=${TLS_DOMAIN}
MEMCLAW_TLS_EMAIL=${TLS_EMAIL}
EOF

# Capture install state for later reruns / memclawctl upgrade
cat > "$MEMCLAW_HOME/install.state.json" <<EOF
{
  "version": "${MEMCLAW_VERSION}",
  "hostname": "${HOSTNAME}",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "offline": ${OFFLINE},
  "jwt_secret_generated": $([ -z "$JWT_SECRET_FILE" ] && echo true || echo false),
  "postgres_password_generated": $([ -z "$POSTGRES_PASSWORD_FILE" ] && echo true || echo false)
}
EOF

# ── Pull + bring up the stack ──────────────────────────────────────────────
# Decide whether to engage local embeddings. Auto-opt-in when the customer
# has neither an LLM key nor a PLATFORM_EMBEDDING_* override — otherwise
# the slim core-api image 503s on every recall.
LOCAL_EMBEDDINGS="${MEMCLAW_LOCAL_EMBEDDINGS:-}"
LOCAL_EMBEDDINGS="${CAURA_LOCAL_EMBEDDINGS:-$LOCAL_EMBEDDINGS}"
if [ -z "$LOCAL_EMBEDDINGS" ]; then
  if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${PLATFORM_EMBEDDING_API_KEY:-}" ] \
      && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ "${EMBEDDING_PROVIDER}" = "local" ]; then
    LOCAL_EMBEDDINGS="true"
    log "No LLM API key detected — enabling local embeddings (sentence-transformers + BGE)."
  else
    LOCAL_EMBEDDINGS="false"
  fi
fi

cd "$MEMCLAW_HOME"
COMPOSE_FILES=(-f docker-compose.yml)

# Let's Encrypt overlay: must come BEFORE airgap/embedder overlays so its
# `gateway: ports: []` empty-list override wins over the base file's port
# mapping (compose merges by index — last-wins for the ports list).
if [ "$TLS_MODE" = "letsencrypt" ]; then
  [ -f docker-compose.tls-letsencrypt.yml ] || die "letsencrypt: docker-compose.tls-letsencrypt.yml missing in bundle" 3
  COMPOSE_FILES+=(-f docker-compose.tls-letsencrypt.yml)
fi

if [ "$OFFLINE" = "true" ]; then
  [ -f docker-compose.airgap.yml ] || die "docker-compose.airgap.yml missing — load the tarball first" 3
  COMPOSE_FILES+=(-f docker-compose.airgap.yml)
  # Verify the airgap tarball was loaded — the override swaps service images
  # to memclaw-onprem/* but the upstream bases (postgres, redis, rabbitmq)
  # stay on their Docker Hub names and must be loaded locally too.
  missing=""
  for img in pgvector/pgvector:pg16 redis:7-alpine rabbitmq:3-management-alpine; do
    docker image inspect "$img" >/dev/null 2>&1 || missing="$missing $img"
  done
  [ -z "$missing" ] || die "--offline: upstream base images missing:${missing}. Run airgap-load.sh first." 3
  if [ "$LOCAL_EMBEDDINGS" = "true" ]; then
    [ -f docker-compose.embedder.airgap.yml ] || die "--offline + local embeddings: docker-compose.embedder.airgap.yml missing" 3
    COMPOSE_FILES+=(-f docker-compose.embedder.airgap.yml)
    docker image inspect "memclaw-onprem/core-api-embedder:${MEMCLAW_VERSION}" >/dev/null 2>&1 \
      || die "--offline + local embeddings: memclaw-onprem/core-api-embedder:${MEMCLAW_VERSION} not loaded. Re-run airgap-load.sh with a tarball that includes the embedder variant." 3
  fi
else
  if [ "$LOCAL_EMBEDDINGS" = "true" ]; then
    [ -f docker-compose.embedder.yml ] || die "local embeddings: docker-compose.embedder.yml missing" 3
    COMPOSE_FILES+=(-f docker-compose.embedder.yml)
  fi
  log "Pulling images (ghcr.io/caura-ai/*:${MEMCLAW_VERSION})"
  # --ignore-buildable skips services with a `build:` section (gateway is
  # built locally from ./nginx/). Available in compose v2.22+ — ships with
  # Docker 24+ which we already require. Fall back without the flag on
  # older installs, tolerating the gateway pull 404.
  if docker compose "${COMPOSE_FILES[@]}" pull --ignore-buildable 2>/dev/null; then
    :
  else
    warn "compose pull --ignore-buildable unsupported or errored; retrying with plain pull (gateway fetch failure expected, will build locally)"
    docker compose "${COMPOSE_FILES[@]}" pull || true
  fi
fi

# Force-rebuild the gateway from the (possibly-updated) bundled
# nginx/Dockerfile. `docker compose up -d` alone would reuse a cached
# memclaw-onprem/gateway:latest from a previous install, so a customer
# running an install.sh that carries a newer nginx.conf.template would
# silently keep the old gateway. `build --no-cache gateway` is the
# cheap guarantee — ~15-30s on this small image, and bulletproofs
# every on-prem update.
log "Rebuilding gateway image from bundled nginx config"
docker compose "${COMPOSE_FILES[@]}" build --no-cache gateway \
  || die "gateway build failed" 3

log "Starting the stack"
docker compose "${COMPOSE_FILES[@]}" up -d || die "docker compose up failed" 3

# ── Wait for /setup/status to come up ──────────────────────────────────────
URL="https://${HOSTNAME}"
# Fall back to http when no TLS cert is mounted yet
if ! curl -sk --connect-timeout 2 "$URL/healthz" >/dev/null 2>&1; then
  URL="http://localhost"
fi

log "Waiting for services (this can take ≤5 min on a cold start)"
# ``-f`` is required — without it a 502 from the still-booting
# platform-auth-api breaks the loop early and the next request races in.
# ``--connect-timeout``/``--max-time`` cap each poll so a server that
# accepts the TCP connection but then stalls on the response (the exact
# 2026-05-26 failure mode) can't block a single poll forever. The loop
# is bounded by a 300s WALL-CLOCK deadline, not a fixed attempt count,
# so slow individual polls can't push total wait past the budget —
# anything beyond 5 min is a real failure, not a slow boot. The previous
# 180s ceiling was hit on a GitHub Actions runner (cold image pull +
# alembic migration + first-boot JWT secret generation stacked up).
SETUP_READY="false"
setup_deadline=$(( $(date +%s) + 300 ))
while [ "$(date +%s)" -lt "$setup_deadline" ]; do
  if curl -sfk --connect-timeout 3 --max-time 5 "$URL/api/setup/status" >/dev/null 2>&1; then
    SETUP_READY="true"
    break
  fi
  # Clamp the trailing sleep to the time actually left: the loop
  # condition is only checked at the top, so an iteration starting just
  # under the deadline could otherwise run curl (≤5s) + sleep 3 and
  # overshoot the 300s budget by up to 8s.
  remaining=$(( setup_deadline - $(date +%s) ))
  [ "$remaining" -gt 0 ] || break
  sleep "$(( remaining < 3 ? remaining : 3 ))"
done
if [ "$SETUP_READY" != "true" ]; then
  # The wait loop only ever sees pass/fail via ``-f``; on timeout we
  # don't know WHY (routing 404 vs gateway 502 vs TLS redirect vs a
  # 5xx from the handler). The 2026-05-26 nightly proved this is not a
  # slow-boot problem — every container was "Up 5 min" yet /setup/status
  # never returned 200. Dump enough to diagnose from the captured
  # install stdout next time instead of guessing: the exact HTTP status
  # of one more probe, plus the two services in the request path.
  warn "/setup/status never returned 200 within 5 min — capturing diagnostics"
  # ``--connect-timeout``/``--max-time`` matter MORE here than in the
  # poll loop: this runs in the known-bad state, so an unbounded probe
  # against a server that accepts TCP but stalls would hang the whole
  # installer in the error path. Cap it hard. ``2>/dev/null`` (not
  # ``2>&1``) keeps curl's own error text out of ${probe} — on a
  # timeout that text is newline-appended and the second line would
  # print without warn's ``!! `` prefix. http_code=000 already signals
  # the failure; the curl error still reaches the terminal's stderr.
  probe=$(curl -sk --connect-timeout 5 --max-time 10 -o /dev/null \
    -w 'http_code=%{http_code} effective_url=%{url_effective} redirect_url=%{redirect_url}' \
    "$URL/api/setup/status" 2>/dev/null || true)
  warn "  last probe via ${URL}: ${probe}"
  if docker compose version >/dev/null 2>&1; then
    warn "  --- docker compose logs (gateway, platform-auth-api, last 60 lines) ---"
    # ``>&2`` keeps the log dump on the same stream as the warn lines
    # above — so a plain ``./install.sh > log`` (no 2>&1) still captures
    # the whole diagnostic block together, not just the indented logs.
    docker compose "${COMPOSE_FILES[@]}" logs --tail=60 --no-color \
      gateway platform-auth-api 2>&1 | sed 's/^/  /' >&2 || true
  fi
  die "/setup/status never returned 200 within 5 min — see diagnostics above" 5
fi

# ── Silent-mode: complete /setup/admin ──────────────────────────────────────
if [ "$SKIP_ADMIN" != "true" ] && [ "$NON_INTERACTIVE" = "true" ]; then
  [ -n "$ADMIN_PASSWORD_RESOLVED" ] || die "admin password not resolved" 2
  ORG_NAME="${HOSTNAME%%.*}"
  log "Creating initial admin ${ADMIN_EMAIL}"
  resp=$(curl -fsSk -X POST "$URL/api/setup/admin" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"email":"%s","password":"%s","org_name":"%s"}' \
            "$ADMIN_EMAIL" "$ADMIN_PASSWORD_RESOLVED" "$ORG_NAME")") \
    || die "/setup/admin failed — check logs: docker compose logs platform-auth-api" 5
  API_KEY=$(echo "$resp" | sed -n 's/.*"api_key":"\([^"]*\)".*/\1/p')
  # Machine-readable result for Ansible
  cat > "$MEMCLAW_HOME/install-result.json" <<EOF
{"url": "${URL}", "admin_email": "${ADMIN_EMAIL}", "api_key": "${API_KEY}"}
EOF
  printf '\n\033[32m=== MemClaw Install Complete ===\033[0m\n'
  printf 'url:     %s\n' "$URL"
  printf 'admin:   %s\n' "$ADMIN_EMAIL"
  printf 'api_key: %s  (printed once — store it)\n' "$API_KEY"
else
  printf '\n\033[32m=== MemClaw is running ===\033[0m\n'
  printf 'Visit %s/setup to upload your license and create the first admin.\n' "$URL"
fi
