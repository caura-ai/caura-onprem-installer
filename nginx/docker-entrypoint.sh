#!/bin/sh
# Render the on-prem gateway config from env vars at container start.
#
# Two output flavours:
#   1. HTTP-only (default) — listen 80, no TLS. Customers either run
#      Caura on a private network or front it with their own TLS
#      terminator. Renders from memclaw.conf.template.
#   2. HTTP+TLS — when /etc/nginx/tls/cert.pem + /etc/nginx/tls/key.pem
#      are mounted (install.sh --tls-cert / --tls-self-signed paths).
#      Renders from memclaw-tls.conf.template; port 80 redirects to 443
#      except for /healthz.
#
# In both cases the location rules come from memclaw-locations.template,
# rendered once into /etc/nginx/snippets/memclaw-locations.conf so the
# HTTP-only and TLS variants share a single source.
set -eu

export SERVER_NAME="${SERVER_NAME:-_}"
export AUTH_UPSTREAM="${AUTH_UPSTREAM:-platform-auth-api:8020}"
export ADMIN_UPSTREAM="${ADMIN_UPSTREAM:-platform-admin-api:8001}"
export CORE_UPSTREAM="${CORE_UPSTREAM:-core-api:8000}"
export APP_UPSTREAM="${APP_UPSTREAM:-app-frontend:3000}"
export CLIENT_MAX_BODY_SIZE="${CLIENT_MAX_BODY_SIZE:-16m}"

mkdir -p /etc/nginx/snippets
rm -f /etc/nginx/conf.d/default.conf

# Always render the shared location snippet first.
# Perimeter secret injected as X-Gateway-Secret on requests carrying gateway-
# resolved identity headers. core-api compares it and rejects the header-trust
# path without it. No default: an empty value renders an empty header, which
# core-api reads as "not from the gateway" and 401s. Compose supplies the same
# value core-api gets.
if [ -z "${GATEWAY_SHARED_SECRET:-}" ]; then
    echo "FATAL: GATEWAY_SHARED_SECRET is empty. core-api requires it and will" >&2
    echo "reject gateway traffic without a matching header. Run upgrade.sh (it" >&2
    echo "backfills the key) or add it to .env, then recreate the stack." >&2
    exit 1
fi
export GATEWAY_SHARED_SECRET

envsubst '${SERVER_NAME} ${AUTH_UPSTREAM} ${ADMIN_UPSTREAM} ${CORE_UPSTREAM} ${APP_UPSTREAM} ${CLIENT_MAX_BODY_SIZE} ${GATEWAY_SHARED_SECRET}' \
    < /etc/nginx/templates/memclaw-locations.template \
    > /etc/nginx/snippets/memclaw-locations.conf

if [ -f /etc/nginx/tls/cert.pem ] && [ -f /etc/nginx/tls/key.pem ]; then
    TLS_MODE="tls"
    TEMPLATE="/etc/nginx/templates/memclaw-tls.conf.template"
else
    TLS_MODE="http-only"
    TEMPLATE="/etc/nginx/templates/memclaw.conf.template"
fi

envsubst '${SERVER_NAME} ${AUTH_UPSTREAM} ${ADMIN_UPSTREAM} ${CORE_UPSTREAM} ${APP_UPSTREAM} ${CLIENT_MAX_BODY_SIZE} ${GATEWAY_SHARED_SECRET}' \
    < "$TEMPLATE" \
    > /etc/nginx/conf.d/default.conf

echo "on-prem gateway configured ($TLS_MODE):"
echo "  server_name = $SERVER_NAME"
echo "  auth        = $AUTH_UPSTREAM"
echo "  admin       = $ADMIN_UPSTREAM"
echo "  core        = $CORE_UPSTREAM"
echo "  app         = $APP_UPSTREAM"
[ "$TLS_MODE" = "tls" ] && echo "  tls         = /etc/nginx/tls/{cert,key}.pem"

exec "$@"
