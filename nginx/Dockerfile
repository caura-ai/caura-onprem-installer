# On-prem gateway image. Plain-HTTP reverse proxy over Docker DNS upstreams.
# Customers terminate TLS in front (Caddy, Traefik, a cloud load balancer,
# or their own cert) — see docs/TLS.md.
FROM nginx:1.27-alpine

# No package installs: the build must work offline, from the base image alone,
# because an air-gapped host builds this image too. The entrypoint needs only
# envsubst, which the base already ships; fail the build, not the gateway, if
# a future base stops shipping it.
RUN command -v envsubst >/dev/null || { echo "nginx base has no envsubst" >&2; exit 1; }

COPY memclaw.conf.template      /etc/nginx/templates/memclaw.conf.template
COPY memclaw-tls.conf.template  /etc/nginx/templates/memclaw-tls.conf.template
COPY memclaw-locations.template /etc/nginx/templates/memclaw-locations.template
COPY docker-entrypoint.sh /docker-entrypoint-memclaw.sh
RUN chmod +x /docker-entrypoint-memclaw.sh

EXPOSE 80 443
ENTRYPOINT ["/docker-entrypoint-memclaw.sh"]
CMD ["nginx", "-g", "daemon off;"]
