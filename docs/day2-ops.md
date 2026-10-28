# Day-2 operations

## Backups

`scripts/backup.sh` snapshots Postgres + Redis +
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

The default backup/restore scripts do not include the separate private
`setup-token`. Preserve it separately using authorized root/service-owner
access, or recreate and prepare it before restore. With the file-delivery bundle,
`restore.sh` stops before database changes if the proof is missing, symlinked,
or nonregular and points to the recovery procedure. Rerun restore after repair.
The compatible auth image bind-mounts this file read-only and refuses a missing
source. Use [the proof-file recovery procedure](troubleshooting.md#missing-setup-token-bind-source)
for its mode-0600/appuser ownership requirements. Recreating this first-run proof
does not rotate database passwords, `.env` credentials or the license. Restores
of older releases must use their matching Compose bundle.

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
- `$CAURA_HOME/logs/<service>/<service>.log` — on-disk files,
  rotated daily at UTC midnight, 5-day retention.

Full layout, redaction rules, and the support-bundle workflow are in
[`logging.md`](logging.md).

```bash
# Live tail across all services
docker compose logs -f

# Grep last 5 days across on-disk files
grep -r "core_api_request_failed" /opt/memclaw/logs/

# Collect a redacted support bundle for Caura
cauractl support bundle --notes "recall 503 under load"
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

Drop the new `license.key` into `./license/`. That is the whole action —
both services re-verify on an hourly loop, so the replacement is picked up
within 60 minutes with no further step. `cauractl license load <path>`
from the host is a convenience wrapper around that same copy; despite the
name it does not reload anything.

To make it take effect now, restart the two services that hold the
license:

```bash
docker compose restart platform-admin-api platform-auth-api
```

The two paths fail in opposite directions, so pick one deliberately. The
hourly loop is fail-safe — an unreadable or missing file keeps the cached
license and polls every ~30s rather than flipping the org read-only. A
restart is fail-fast: the license is read during startup and a service
refuses to start on an invalid one.

## Retired broker artifacts

The broker-free stack needs Postgres and Redis backups. Restore accepts older
archives and reports that `rabbitmq-defs.json` is ignored; the archive remains
unchanged. Installer upgrades do not delete existing RabbitMQ named volumes.
Keep old backups and volumes for a planned rollback; no automatic cleanup is
performed. Pair this installer with service images containing the direct admin
audit persistence fix before relying on new audit writes.


After verifying direct audit writes, identify the retired container for this
install's compose project with `docker ps -a --filter label=com.docker.compose.service=rabbitmq`.
Check the project label and mount/volume name with `docker inspect <container-id>`.
Stop it with `docker stop <container-id>` and remove only that container with
`docker rm <container-id>`; do not use `-v`. Keep its named volume and the
`rabbitmq:3-management-alpine` image throughout the rollback window. Offline
rollback to an older compose requires that image locally; do not prune it.
After explicitly closing that window, identify the project's `rabbitmqdata`
volume with `docker volume ls` and remove only that confirmed unused volume
with `docker volume rm <project-rabbitmqdata-volume>`. Retain backup archives.
