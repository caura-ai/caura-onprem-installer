# Day-2 operations

## Backups

`scripts/backup.sh` snapshots Postgres + Redis + RabbitMQ definitions +
the `.env` + `license.key` into a single tarball.

```bash
cd /opt/memclaw
./scripts/backup.sh
# → /opt/memclaw/backups/memclaw-backup-<timestamp>.tar.gz
```

Schedule via cron:

```
0 2 * * * cd /opt/memclaw && /opt/memclaw/scripts/backup.sh >>/var/log/memclaw-backup.log 2>&1
```

## Restore

```bash
# Data only (.env + license preserved):
./scripts/restore.sh /path/to/memclaw-backup-<ts>.tar.gz

# Full restore (replaces .env + license.key):
./scripts/restore.sh --replace-config /path/to/memclaw-backup-<ts>.tar.gz
docker compose restart
```

## Logs

Every service writes JSON logs to **two places**:

- stdout → `docker compose logs -f <service>` — and whatever log
  driver you've configured in `docker-compose.yml` (gelf, fluentd…).
- `$MEMCLAW_HOME/logs/<service>/<service>.log` — on-disk files,
  rotated daily at UTC midnight, 5-day retention.

Full layout, redaction rules, and the support-bundle workflow are in
[`logging.md`](logging.md).

```bash
# Live tail across all services
docker compose logs -f

# Grep last 5 days across on-disk files
grep -r "core_api_request_failed" /opt/memclaw/logs/

# Collect a redacted support bundle for Caura
memclawctl support bundle --notes "recall 503 under load"
```

## Health + license endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` (unauth) | Per-service liveness — used by compose healthchecks |
| `GET /readyz` (unauth) | Readiness — true once DB migrations have run |
| `GET /api/license/status` (auth) | License claims + severity; frontend banner polls this |

## Monitoring

Every service exports Prometheus metrics on `/metrics` (same port as the
API). Point your Prometheus at:

```yaml
- targets:
  - platform-admin-api:8001
  - platform-auth-api:8020
  - platform-storage-api:8003
  - platform-audit-api:8021
  - core-api:8000
  - core-storage-api:8002
```

## Reloading the license without downtime

Drop the new `license.key` into `./license/` and run:

```bash
docker compose exec platform-admin-api memclawctl license load /etc/memclaw/license.key
docker compose exec platform-auth-api  memclawctl license load /etc/memclaw/license.key
```

Both services re-verify hourly anyway, so on-disk replacement is picked
up within 60 minutes without the manual nudge.
