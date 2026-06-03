# On-prem gateway image. Plain-HTTP reverse proxy over Docker DNS upstreams.
# Customers terminate TLS in front (Caddy, Traefik, a cloud load balancer,
# or their own cert) — see docs/TLS.md.
FROM nginx:1.27-alpine

RUN apk add --no-cache gettext

COPY memclaw.conf.template      /etc/nginx/templates/memclaw.conf.template
COPY memclaw-tls.conf.template  /etc/nginx/templates/memclaw-tls.conf.template
COPY memclaw-locations.template /etc/nginx/templates/memclaw-locations.template
COPY docker-entrypoint.sh /docker-entrypoint-memclaw.sh
RUN chmod +x /docker-entrypoint-memclaw.sh

EXPOSE 80 443
ENTRYPOINT ["/docker-entrypoint-memclaw.sh"]
CMD ["nginx", "-g", "daemon off;"]
