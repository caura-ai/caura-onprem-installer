# On-prem upgrade runbook (operator / managed fleet)

> This is the **operator** protocol for Caura-managed on-prem deployments that
> front the stack with the **TLS (Caddy) overlay** and run the scheduler
> services. For the simple customer flow see [`upgrade.md`](upgrade.md).
> The two differ in three ways that matter: managed cutovers (1) always pass
> the explicit `-f` overlay files, (2) stage on a mirror box first, and (3)
> must account for `core-operations`/`platform-operations`.

## Topology & access

| Box | Role | Reach |
|-----|------|-------|
| *staging mirror* | Validate here first — **never skip**. | `gcloud compute ssh <box> --zone <zone>` (direct external IP; IAP not authorized). gcloud token lapses → `gcloud auth login`. Has its own TLS domain and admin account. |
| *managed prod* | The customer-facing deployment being cut over. | `ssh -i <key> <user>@<host>`; `sudo`; app at `/opt/memclaw`. <!-- legacy-name-ok: the on-disk install path, which is floor --> |

> **Box names, hostnames, addresses and key paths are deliberately not in this
> file.** This repository is public. The fleet inventory — which mirror pairs
> with which deployment, and how to reach each — lives in the internal ops
> store; get it from there before starting. A runbook is portable across
> deployments, an inventory is not, and the two have no reason to share a file.

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

**OSS (`caura`):** merge the release-please PR (manual — never auto-merge) → tags `backend-v*` + `plugin-v*`. If the plugin changed:
- bump `MIN_RECOMMENDED_PLUGIN_VERSION` (core-api `version_compat.py`) to the new plugin version (companion PR), and
- the served plugin version (`package.json`, `openclaw.plugin.json`, `src/version.ts` via `gen-version.sh`) must equal that floor. **release-please does NOT run `gen-version.sh`** → `check:version` fails on the release PR until you regenerate `version.ts` and push. **All commits need a DCO `Signed-off-by:`** (`git commit -s`).

**Enterprise (`caura-enterprise`):** tag off `dev` HEAD — create the ref via API (a shallow `git clone` of the enterprise repo has hung):
```bash
gh api -X POST repos/caura-ai/caura-enterprise/git/refs \
  -f ref=refs/tags/onprem-vX.Y.Z -f sha=$(gh api repos/caura-ai/caura-enterprise/commits/dev --jq .sha)
```
`release-onprem.yml` builds **11** images (8 services + `core-operations` + `core-worker` + `platform-operations`). The `core-api-embedder` job **fails (404) — expected** for OpenAI customers; that marks the overall run "failure" and **skips `finalize`** (air-gap tarball/sign/SBOM). The 11 service images still publish — verify those jobs are green.

> **New-image ghcr gotcha:** a brand-new image package is **private** by default; on-prem `docker pull` 401s until you make it public (OSS-sourced images) or grant access. (`core-operations`/`core-worker` are public; `platform-operations` is enterprise and currently private/deferred.)

## Phase 2 — Pre-flight

- **Migrations:** compare public (core-storage) + enterprise (platform-storage) Alembic heads to what's deployed; they run on container lifespan during the cutover.
- **Destructive lifecycle/migrations:** if the release introduces a destructive op (e.g. a new retention purge) or a heavy migration, **analyze first-run impact read-only first** (count affected rows) and get sign-off before deploying.
- Gateway/nginx: rebuilt locally regardless (`docker compose build gateway`).
- Confirm embedding provider (`EMBEDDING_PROVIDER=openai` → embedder 404 is fine).

## Phase 3 — Validate on the staging mirror (never skip)

Run the full Phase-4 cutover on the mirror, then smoke:
- Alembic heads advanced; `/healthz`, `/api/version`, plugin floor == served version.
- **write→recall round-trip:** login (`/api/auth/user/login`) → mint a key (`POST /api/keys?tenant_id=…` body `{tenant_id,label,kind:"user_api_key"}`) → MCP `memclaw_write`+`memclaw_recall` **with an explicit `agent_id`** (the default `mcp-agent` is rejected on the gateway path) → delete the key.

## Phase 4 — Production cutover

```bash
cd /opt/memclaw
F="-f docker-compose.yml -f docker-compose.override.yml -f docker-compose.tls-letsencrypt.yml"

# 1. Backup FIRST
bash scripts/backup.sh

# 2. Bump versions — keep the scheduler images in lockstep with the stack
# Rewrites whichever spelling of the version key this .env carries, and
# refuses rather than reporting success if it carries neither.
./scripts/set-version.sh vX.Y.Z
# Converge the transitional ops-version skew: ops images track the stack.
# A box with no ops-version line at all is already in that state, so this
# reports it and exits 0 rather than treating it as an error.
./scripts/set-version.sh --ops vX.Y.Z

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

> `set-version.sh` ships in the release bundle. On an install that predates it
> the file will not be there — open `.env` and change the version key by hand
> instead. It is `CAURA_VERSION`, or the older `MEMCLAW_VERSION` if your file  <!-- legacy-name-ok: teaches the old spelling so an operator recognises which key their file has -->
> was written before the rename; both are read, and
> [`env-aliases.md`](env-aliases.md) lists every pair.

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
| **platform-operations** | session-cleanup (5m) + org-hard-delete-sweep (24h), synchronous via platform-admin-api. | **Deferred** on current managed deployments. Needs `PLATFORM_OPERATIONS_INTERNAL_TOKEN` on **both** itself and platform-admin-api; admin-api listens on **:8001** (not the :8101 cloud default). Its image is enterprise → private; make pullable before deploying. |
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
