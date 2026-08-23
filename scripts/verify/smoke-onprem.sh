#!/usr/bin/env bash
# Post-install smoke test for MemClaw on-prem deployments.
#
# Exits 0 on success, non-zero with an indicative code on failure.
# Safe to run repeatedly — creates temporary memories + agents tagged
# with "onprem-smoke-<timestamp>" and doesn't touch customer data.
#
# Usage:
#   ./smoke-onprem.sh                                    # uses $BASE_URL and install-result.json
#   BASE_URL=http://memclaw.acme.local ADMIN_JWT=... ADMIN_API_KEY=... ./smoke-onprem.sh
#
# Exit codes:
#   0 all green · 1 config missing · 2 gateway unreachable
#   3 auth failure · 4 core-api unreachable · 5 MCP failed
#   6 embedder failed · 7 license invalid

set -euo pipefail

# The install root, under either spelling — old name first, CAURA_HOME
# overriding only when non-empty. See scripts/backup.sh for the full note.
MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"
MEMCLAW_HOME="${CAURA_HOME:-$MEMCLAW_HOME}"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
BASE_URL="${BASE_URL:-}"
ADMIN_JWT="${ADMIN_JWT:-}"
ADMIN_API_KEY="${ADMIN_API_KEY:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

pass() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit "${2:-1}"; }
info() { printf "\033[36m==>\033[0m %s\n" "$*"; }

# ── Auto-discover URL + creds from install-result.json / .env ───────────────
if [ -z "$BASE_URL" ] && [ -f "$MEMCLAW_HOME/install-result.json" ]; then
  BASE_URL=$(jq -r .url "$MEMCLAW_HOME/install-result.json" 2>/dev/null || true)
  ADMIN_EMAIL=${ADMIN_EMAIL:-$(jq -r .admin_email "$MEMCLAW_HOME/install-result.json" 2>/dev/null || true)}
  ADMIN_API_KEY=${ADMIN_API_KEY:-$(jq -r .api_key "$MEMCLAW_HOME/install-result.json" 2>/dev/null || true)}
fi
BASE_URL=${BASE_URL:-http://localhost}
[ -n "$BASE_URL" ] || fail "BASE_URL not set and install-result.json not found" 1

info "smoke target: $BASE_URL"

# ── 1. gateway healthz ──────────────────────────────────────────────────────
info "1. gateway healthz"
code=$(curl -so /dev/null -w "%{http_code}" --max-time 5 "$BASE_URL/healthz")
[ "$code" = "200" ] || fail "gateway /healthz returned $code" 2
pass "gateway healthy"

# ── 2. core-api version ─────────────────────────────────────────────────────
info "2. core-api version endpoint"
ver=$(curl -sf --max-time 5 "$BASE_URL/api/version" | jq -r .version 2>/dev/null || echo "")
[ -n "$ver" ] || fail "/api/version not reachable" 4
pass "core-api: version=$ver"

# ── 3. setup status ─────────────────────────────────────────────────────────
info "3. setup status (admin should exist)"
st=$(curl -sf --max-time 5 "$BASE_URL/api/setup/status")
admin_exists=$(echo "$st" | jq -r .admin_exists)
license_loaded=$(echo "$st" | jq -r .license_loaded)
[ "$admin_exists" = "true" ] || fail "no admin has been created yet" 3
[ "$license_loaded" = "true" ] || fail "license file not loaded" 7
pass "admin_exists=$admin_exists, license_loaded=$license_loaded"

# ── 4. login + JWT ──────────────────────────────────────────────────────────
if [ -z "$ADMIN_JWT" ] && [ -n "${ADMIN_EMAIL:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ]; then
  info "4. login with admin credentials"
  ADMIN_JWT=$(curl -sf -X POST "$BASE_URL/api/auth/user/login" \
    -H "Content-Type: application/json" \
    -d "$(jq -cn --arg e "$ADMIN_EMAIL" --arg p "$ADMIN_PASSWORD" '{email:$e,password:$p}')" \
    | jq -r .access_token)
  [ -n "$ADMIN_JWT" ] || fail "login returned no access_token" 3
  pass "JWT ${#ADMIN_JWT} chars"
elif [ -n "$ADMIN_JWT" ]; then
  info "4. using pre-supplied ADMIN_JWT"
  pass "JWT from env"
else
  info "4. skipping login (no password; pass ADMIN_EMAIL + ADMIN_PASSWORD or ADMIN_JWT)"
fi

# ── 5. license status (via admin JWT if we have one) ────────────────────────
if [ -n "$ADMIN_JWT" ]; then
  info "5. license status"
  ls=$(curl -sf --max-time 5 "$BASE_URL/api/license/status" \
    -H "Authorization: Bearer $ADMIN_JWT")
  sev=$(echo "$ls" | jq -r .severity)
  days=$(echo "$ls" | jq -r .days_remaining)
  case "$sev" in
    ok|warning)      pass "severity=$sev, days_remaining=$days" ;;
    critical)        pass "severity=$sev, days_remaining=$days (⚠️ renew soon)" ;;
    expired)         fail "license expired ($days days ago) — writes are blocked" 7 ;;
    *)               fail "unknown severity '$sev'" 7 ;;
  esac
fi

# ── 6. MCP handshake + tools/list ───────────────────────────────────────────
if [ -n "${ADMIN_API_KEY:-}" ]; then
  info "6. MCP handshake + tools/list"
  mcp=$(curl -sf --max-time 10 -X POST "$BASE_URL/mcp/" \
    -H "X-API-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}')
  name=$(echo "$mcp" | jq -r .result.serverInfo.name 2>/dev/null || echo "")
  [ -n "$name" ] || fail "MCP initialize returned no serverInfo.name" 5
  pass "MCP server: $name"

  tools=$(curl -sf --max-time 10 -X POST "$BASE_URL/mcp/" \
    -H "X-API-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
  tool_count=$(echo "$tools" | jq '.result.tools | length' 2>/dev/null || echo 0)
  [ "$tool_count" -ge 5 ] || fail "MCP tools/list returned $tool_count tools (expected ≥5)" 5
  pass "MCP tools: $tool_count registered"

  # ── 7. write + recall round-trip ──
  info "7. memclaw_write"
  STAMP=$(date +%s)
  write_resp=$(curl -sf --max-time 15 -X POST "$BASE_URL/mcp/" \
    -H "X-API-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$(jq -cn --arg s "$STAMP" '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"memclaw_write",arguments:{content:("onprem-smoke-" + $s + " — automated verify"),memory_type:"fact"}}}')")
  mem=$(echo "$write_resp" | jq -r '.result.content[0].text' | jq -r .id 2>/dev/null || echo "")
  [ -n "$mem" ] && [ "$mem" != "null" ] || fail "memclaw_write did not return a memory id (resp: $(echo "$write_resp" | head -c 200))" 5
  pass "wrote memory $mem"

  info "8. memclaw_recall (tests the embedder)"
  sleep 3
  recall_resp=$(curl -sf --max-time 30 -X POST "$BASE_URL/mcp/" \
    -H "X-API-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$(jq -cn --arg s "$STAMP" '{jsonrpc:"2.0",id:4,method:"tools/call",params:{name:"memclaw_recall",arguments:{query:("onprem smoke " + $s),top_k:3}}}')")
  recall_text=$(echo "$recall_resp" | jq -r '.result.content[0].text')
  echo "$recall_text" | grep -q "Embedding service unavailable" \
    && fail "recall failed: embedder not reachable. For no-LLM-key installs, engage docker-compose.embedder.yml (see docs/troubleshooting.md)" 6
  found=$(echo "$recall_text" | jq '.results | length' 2>/dev/null || echo 0)
  [ "$found" -ge 1 ] || fail "recall returned no results (text: $(echo "$recall_text" | head -c 200))" 6
  pass "recall returned $found result(s), embedder ok"
else
  info "6-8. skipping MCP round-trip (no ADMIN_API_KEY)"
fi

echo ""
info "\033[32mall checks passed\033[0m"
