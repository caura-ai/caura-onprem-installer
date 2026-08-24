# On-prem upgrade runbook (operator / managed fleet)

> This is the **operator** protocol for Caura-managed on-prem deployments that
> front the stack with the **TLS (Caddy) overlay** and run the scheduler
> services (e.g. eToro). For the simple customer flow see [`upgrade.md`](upgrade.md).
> The two differ in three ways that matter: managed cutovers (1) always pass
> the explicit `-f` overlay files, (2) stage on a mirror box first, and (3)
> must account for `core-operations`/`platform-operations`.

## Topology & access

| Box | Role | Reach |
|-----|------|-------|
| `erni-onprem` | **staging mirror** (validate here first — never skip) | `gcloud compute ssh erni-onprem --zone us-central1-f` (direct external IP; IAP not authorized). gcloud token lapses → `gcloud auth login`. TLS domain `erni-onprem-test.memclaw.dev`, admin `t@t.com`. |
| `oc-memclaw-prod` | **eToro prod** (`memclawv1.clawz.org` → 134.98.155.239) | `ssh -i ~/.ssh/id_ed25519_etoro_openclaw ubuntu@134.98.155.239`; `sudo`; app at `/opt/memclaw`. |

Compose file set on these boxes (**always pass all three**, in this order):
```
-f docker-compose.yml -f docker-compose.override.yml -f docker-compose.tls-letsencrypt.yml
```
`override.yml` carries the `shm_size: 1g` + core-storage 8-worker + `max_connections=200` boosts; the TLS overlay adds Caddy. Passing explicit `-f` files **disables** auto-loading of `override.yml`, so it must be listed explicitly.

> **Run remote scripts via `scp` + `ssh sudo bash <file>`, never a piped here-doc.**
> A here-doc piped to `bash -s` breaks when the script contains `docker compose exec`
> (the exec drains the remaining stdin; only the first command runs, exit 0 masks it).

---

## Phase 1 — Cut the release

**OSS (`caura-memclaw`):** merge the release-please PR (manual — never auto-merge) → tags `backend-v*` + `plugin-v*`. If the plugin changed:
- bump `MIN_RECOMMENDED_PLUGIN_VERSION` (core-api `version_compat.py`) to the new plugin version (companion PR), and
- the served plugin version (`package.json`, `openclaw.plugin.json`, `src/version.ts` via `gen-version.sh`) must equal that floor. **release-please does NOT run `gen-version.sh`** → `check:version` fails on the release PR until you regenerate `version.ts` and push. **All commits need a DCO `Signed-off-by:`** (`git commit -s`).

**Enterprise (`caura-memclaw-enterprise`):** tag off `dev` HEAD — create the ref via API (a shallow `git clone` of the enterprise repo has hung):
```bash
gh api -X POST repos/caura-ai/caura-memclaw-enterprise/git/refs \
  -f ref=refs/tags/onprem-vX.Y.Z -f sha=$(gh api repos/caura-ai/caura-memclaw-enterprise/commits/dev --jq .sha)
```
`release-onprem.yml` builds **11** images (8 services + `core-operations` + `core-worker` + `platform-operations`). The `core-api-embedder` job **fails (404) — expected** for OpenAI customers; that marks the overall run "failure" and **skips `finalize`** (air-gap tarball/sign/SBOM). The 11 service images still publish — verify those jobs are green.

> **New-image ghcr gotcha:** a brand-new image package is **private** by default; on-prem `docker pull` 401s until you make it public (OSS-sourced images) or grant access. (`core-operations`/`core-worker` are public; `platform-operations` is enterprise and currently private/deferred.)

## Phase 2 — Pre-flight

- **Migrations:** compare public (core-storage) + enterprise (platform-storage) Alembic heads to what's deployed; they run on container lifespan during the cutover.
- **Destructive lifecycle/migrations:** if the release introduces a destructive op (e.g. a new retention purge) or a heavy migration, **analyze first-run impact read-only first** (count affected rows) and get sign-off before deploying.
- Gateway/nginx: rebuilt locally regardless (`docker compose build gateway`).
- Confirm embedding provider (`EMBEDDING_PROVIDER=openai` → embedder 404 is fine).

## Phase 3 — Validate on `erni-onprem` (never skip)

Run the full Phase-4 cutover on erni, then smoke:
- Alembic heads advanced; `/healthz`, `/api/version`, plugin floor == served version.
- **write→recall round-trip:** login (`/api/auth/user/login`) → mint a key (`POST /api/keys?tenant_id=…` body `{tenant_id,label,kind:"user_api_key"}`) → MCP `memclaw_write`+`memclaw_recall` **with an explicit `agent_id`** (the default `mcp-agent` is rejected on the gateway path) → delete the key.

## Phase 4 — eToro cutover

```bash
cd /opt/memclaw
F="-f docker-compose.yml -f docker-compose.override.yml -f docker-compose.tls-letsencrypt.yml"

# 1. Backup FIRST
bash scripts/backup.sh

# 2. Bump versions — keep the scheduler images in lockstep with the stack
# Rewrites whichever spelling this .env carries. A managed box installed
# before the rename has only MEMCLAW_*, so a CAURA_-only sed would match; legacy-name-ok: names both spellings so the edit works on an .env written before the rename
# nothing and silently leave the box on its old tag. Both names are read.
sed -i -E 's/^(CAURA|MEMCLAW)_VERSION=.*/\1_VERSION=vX.Y.Z/' .env  # legacy-name-ok: names both spellings so the edit works on an .env written before the rename
# Converge the transitional ops-version skew: ops images track the stack.
sed -i -E 's/^(CAURA|MEMCLAW)_OPS_VERSION=.*/\1_OPS_VERSION=vX.Y.Z/' .env   # or delete the line — BOTH spellings if present — to track the stack version; legacy-name-ok: names both spellings so the edit works on an .env written before the rename

# 3. Pull service images — INCLUDING core-operations (and platform-operations if deployed)
docker compose $F pull core-api core-storage-api platform-admin-api platform-auth-api \
  platform-audit-api platform-storage-api app-frontend core-operations

# 4. Build gateway locally
docker compose $F build gateway

# 5. Force-recreate platform-storage-api -> triggers enterprise Alembic (runs only on lifespan)
docker compose $F up -d --force-recreate --no-deps platform-storage-api

# 6. Bring everything up -> public Alembic + recreates core-storage, core-api, core-operations, ...
docker compose $F up -d
```

**Hard rules (each one bit us):**
- **Never `--remove-orphans`** — Caddy lives in the TLS overlay and would be killed.
- **Always all three `-f` files** (base + override + tls) or Caddy/TLS breaks and the override boosts are dropped.
- **Force-recreate `platform-storage-api` explicitly** — Alembic only runs on lifespan; a plain `up -d` skips a healthy container.

**Smoke:** Alembic heads; `/healthz`; `/api/version`; plugin floor == served; `core-operations` `Up` with `"Scheduler started"` in its logs; write/recall round-trip.

## Phase 5 — Fleet plugin auto-upgrade

With the floor bumped, manifest-aware nodes (plugin ≥ 2.6.0) auto-upgrade on their next heartbeat (~60s). Nodes < 2.6.0 need a manual install one-liner; managed (MCP) agents need nothing; offline nodes upgrade when they reconnect. Watch `public.fleet_nodes.plugin_version` climb.

---

## Scheduler / lifecycle services

| Service | What it does | On-prem reality |
|---------|--------------|-----------------|
| **core-operations** | OSS lifecycle crons: archive-expired/stale, **purge-soft-deleted** (retention, default 30d), crystallize, entity-link, insights. | **Deployed.** Fires fanout POSTs to core-api; **core-api self-consumes them in-process** on the InProcess event bus (`register_archive_consumers` under `isinstance(event_bus, InProcessEventBus)` + pipeline/insights always). Single-replica. Pinned to `CAURA_OPS_VERSION`. Logs to **stdout** (image runs non-root). Scheduler is **aligned** (daily cadence; does **not** fire on container startup). |
| **platform-operations** | session-cleanup (5m) + org-hard-delete-sweep (24h), synchronous via platform-admin-api. | **Deferred** on eToro. Needs `PLATFORM_OPERATIONS_INTERNAL_TOKEN` on **both** itself and platform-admin-api; admin-api listens on **:8001** (not the :8101 cloud default). Its image is enterprise → private; make pullable before deploying. |
| **core-worker** | SaaS pubsub offload for embed/enrich/lifecycle. | **Not used on-prem** — the inprocess bus means core-api handles lifecycle itself. |

**Event bus:** on-prem `EVENT_BUS_BACKEND` is unset → **`inprocess`** (the only backends are `inprocess`/`pubsub`; there is **no** rabbit bus — `RABBITMQ_URL` serves platform-audit, not `common.events`). This is *why* core-worker isn't needed here.

**Trigger a lifecycle action deterministically** (e.g. to action a reviewed purge at a known state instead of waiting for the aligned tick) — note **core-api has no `curl`**, use python:
```bash
docker compose $F exec -T -e KEY="$(grep ^CORE_ADMIN_API_KEY= .env | cut -d= -f2-)" core-api python -c \
  "import os,urllib.request as u; r=u.urlopen(u.Request('http://localhost:8000/api/v1/admin/lifecycle/fanout/purge-soft-deleted',method='POST',headers={'X-API-Key':os.environ['KEY']}),timeout=30); print(r.status, r.read()[:200])"
```
core-api logs `lifecycle purge-soft-deleted processed` with per-org `deleted` counts.

## Consolidated gotchas
- `scp` + `ssh sudo bash <file>`; never pipe a here-doc containing `docker compose exec`.
- Always all three `-f` files; never `--remove-orphans`; force-recreate platform-storage-api for migrations.
- Keep the ops-version pin == the stack version (the skew var was a one-time transitional hack).
- `.env` keys have two spellings (`CAURA_*` and the older `MEMCLAW_*`); both are read, first non-empty wins. Rewrite whichever the box has — see [`env-aliases.md`](env-aliases.md).  <!-- legacy-name-ok: names both spellings so the edit works on an .env written before the rename -->
- Cut enterprise tags via `gh api … /git/refs` (clone hangs).
- release-please needs `gen-version.sh` re-run for `version.ts`; DCO sign-off required; merges are manual.
- New ghcr image packages are private → make public / grant access before on-prem pull.
- `core-api` has no `curl` → use `python -c urllib` inside it.
- embedder build 404 is expected (skips `finalize`); the 11 service images still publish.
