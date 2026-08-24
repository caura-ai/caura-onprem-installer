# Upgrade guide

Upgrades are **tag-driven** and fully reversible. Bump `CAURA_VERSION`
in `.env`, pull the new images, restart. Data stays in Docker named
volumes — never touched by the upgrade.

> **Operators / Caura-managed fleet:** if the deployment fronts the stack with
> the TLS (Caddy) overlay or runs the scheduler services (`core-operations`,
> `platform-operations`), follow the
> [operator runbook](upgrade-runbook-operator.md) instead — it covers the
> explicit `-f` overlay files, staging-first validation, and the scheduler
> images. The flow below is the minimal customer path.

## Supported upgrade path

**Sequential minor versions only.** v1.0.x → v1.1.x, then v1.1.x → v1.2.x.
Skipping minors (v1.0.x → v1.2.x) is not tested and may leave Alembic
migrations in an inconsistent state.

Patch releases (v1.0.0 → v1.0.1) can always be jumped without
intermediate steps.

## Prerequisites

- Working install (if the current stack is down, fix that first; an
  upgrade won't repair a broken deployment).
- Backup from the last 24h (`./scripts/backup.sh` — see `day2-ops.md`).
- The target release tarball (`memclaw-onprem-<new-version>.tar.gz`) if
  you're air-gapped.

## Connected upgrade

```bash
cd /opt/memclaw

# 1. Take a safety backup
./scripts/backup.sh

# 2. Pick the target version
./scripts/set-version.sh v1.1.0

# 3. Pull + roll
docker compose pull
docker compose up -d

# 4. Watch migrations run
docker compose logs -f platform-storage-api
# → look for "alembic upgrade head" and "Application startup complete"
```

> `set-version.sh` ships in the release bundle. On an install that predates it
> the file will not be there — open `.env` and change the version key by hand
> instead. It is `CAURA_VERSION`, or the older `MEMCLAW_VERSION` if your file  <!-- legacy-name-ok: teaches the old spelling so an operator recognises which key their file has -->
> was written before the rename; both are read, and
> [`env-aliases.md`](env-aliases.md) lists every pair.

Migrations auto-run on `platform-storage-api` startup (idempotent — the
runner is safe to re-invoke on restart). Other services won't accept
traffic until storage-api reports healthy.

## Air-gap upgrade

Identical flow, with two extra steps at the front:

```bash
# 1. Load the new images alongside the old ones
./airgap-load.sh /path/to/memclaw-onprem-v1.1.0.tar.gz

# 2–4. Same as connected, but `docker compose pull` is a no-op — the
#      airgap overlay resolves to locally-loaded tags.
cd /opt/memclaw
./scripts/backup.sh
./scripts/set-version.sh v1.1.0
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
```

The old images stay on disk. Once you've verified the new version,
clean them up:

```bash
docker image prune  # removes unreferenced images only
```

## Verify after upgrade

```bash
# Version endpoint reflects the new tag
curl -sf http://memclaw.acme.com/api/version | jq .version
# → "v1.1.0"

# License still valid
curl -s http://memclaw.acme.com/api/license/status \
  -H "Authorization: Bearer $JWT" | jq .severity
# → "ok"

# Write a test memory + search for it (round-trip through the full stack)
curl -s -X POST http://memclaw.acme.com/api/memories \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"...","agent_id":"...","content":"upgrade-smoke","memory_type":"fact"}'
# → 201

# All containers healthy
docker compose ps
```

## Rollback

```bash
cd /opt/memclaw
./scripts/set-version.sh v1.0.0   # old tag
docker compose up -d
```

**Caveat**: Alembic doesn't auto-downgrade. If the new version shipped a
destructive migration, rollback requires restoring from the backup you
took in step 1:

```bash
./scripts/restore.sh /opt/memclaw/backups/memclaw-backup-<last-good>.tar.gz
```

Caura commits to making minor-version migrations backwards-compatible
within the same major. Cross-major migrations (v1.x → v2.x) will call
this out in the release notes.
