# Runbook — eToro on-prem cutover to v2.11.16 (backend-v2.29.0)

**Status:** DESIGN — not yet executed. Requires eToro VPN/SSH + explicit human go.
**Source (from):** v2.11.14 (backend-v2.27.0, alembic **033**)
**Target (to):** v2.11.16 (backend-v2.29.0, alembic **039**), plugin 2.16.1, floor held 2.16.0
**Validated on:** erni (`erni-onprem`) 2026-08-20 — #802 gateway hop 200, migrations 034–039 clean, alembic=039.

---

## 1. Key finding — this is NOT shm-bound

An earlier draft flagged migration **035** as needing postgres `shm=1g`. **That was wrong.**
`CREATE INDEX CONCURRENTLY` cannot use parallel workers, so it uses `maintenance_work_mem`
+ on-disk temp sort — it never touches `/dev/shm`. The `shm=1g` override exists only for the
*parallel HNSW* build (migration 012, applied on eToro long ago). erni built all four
CONCURRENTLY indexes valid at the default 64 MB. **Nothing in 034–039 needs `shm`.**

The real risk is **time inside upgrade.sh's 180 s health gate** on eToro's large tables: the
four `CONCURRENTLY` builds (which also *wait, unbounded, behind open writer transactions*)
plus 038's `VALIDATE` scan. If migrations exceed the gate, upgrade.sh auto-rolls-back
**mid-migration** — the failure mode to avoid.

## 2. Migration batch profile (034 → 039)

| Mig | Operation | eToro cost |
|-----|-----------|------------|
| 034 | search_vector trigger swap, **no backfill** | instant |
| 035 | 3× `CREATE INDEX CONCURRENTLY` (partial/btree) | **heavy** — `ix_memory_entity_links_entity_id` is a *full* btree on the large link table |
| 036 | nullable columns on `memories` + 2 new empty tables + their indexes | instant |
| 037 | nullable column + 1× `CONCURRENTLY` on `memories` | indexes **0 rows** (new all-NULL column) but does 2 full heap scans |
| 038 | `documents` NULL-backfill + `ADD CHECK NOT VALID` → `VALIDATE` (non-blocking) → `SET NOT NULL` | one scan of `documents` |
| 039 | new table `tenant_usage_counters` | instant |

Nothing takes a write-blocking AccessExclusive lock on a large populated table (038 uses the
`NOT VALID`→`VALIDATE` pattern deliberately; 036's plain indexes are on new empty tables).

## 3. Procedure

### Phase 0 — assess (read-only, txn-wrapped)
Capture, before scheduling the window:
- row counts: `memories`, `memory_entity_links`, `documents`
- `documents` NULL-timestamp count: `SELECT count(*) FROM documents WHERE created_at IS NULL OR updated_at IS NULL;`
- open long transactions: `SELECT pid, state, now()-xact_start AS age, query FROM pg_stat_activity WHERE state <> 'idle' AND xact_start IS NOT NULL ORDER BY age DESC LIMIT 10;`

These predict in-gate timing and whether an open writer would stall `CONCURRENTLY`.

### Phase 1 — pre-build 035's indexes on the LIVE v2.11.14 DB (low-traffic window)
Their columns already exist; `CONCURRENTLY` does not lock writes; `IF NOT EXISTS` then makes
migration 035 a no-op. Run each, then confirm `indisvalid = true`.

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memory_entity_links_entity_id ON memory_entity_links (entity_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_supersedes_id       ON memories (supersedes_id)          WHERE supersedes_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_relations_evidence_memory     ON relations (evidence_memory_id)    WHERE evidence_memory_id IS NOT NULL;
```

Validity check:
```sql
SELECT c.relname, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN ('ix_memory_entity_links_entity_id','ix_memories_supersedes_id','ix_relations_evidence_memory');
```
If any `CONCURRENTLY` build is killed it leaves an **invalid** index — drop it
(`DROP INDEX CONCURRENTLY IF EXISTS <name>;`) and re-run; migration 035 also self-heals this.

> 037's `ix_memories_stale_embedding` **cannot** be pre-built — its predicate references
> `embedded_content_hash`, a column that migration 037 adds. It indexes 0 rows at creation
> (the new column is all-NULL), so it is cheap regardless; do not attempt to pre-create it.

**erni dry-run baseline (2026-08-20).** Drop+rebuild of the heaviest index validated the exact
DDL and gives a per-row anchor (no contention, small box):

| table | erni rows | note |
|-------|----------:|------|
| `memories` | 16,123 | |
| `memory_entity_links` | 156,948 | drives the full-btree build |
| `relations` | 65,361 | |
| `documents` | 1 | 038 scan not exercisable here; 0 NULL timestamps |

`CREATE INDEX CONCURRENTLY ix_memory_entity_links_entity_id` on 156,948 rows = **0.33 s**
(index 3.1 MB, valid) → ≈ **2 µs/row** with no write contention. Pre-building has **no health
gate**, so absolute time is not a constraint — the point is to keep this build *out* of the
180 s upgrade gate. The remaining in-gate unknown is 038's `VALIDATE` scan of `documents`
(empty on erni); size it in Phase 0 — the `--health-timeout 600` covers it.

### Phase 2 — upgrade with a widened health gate (low-traffic window)
```bash
curl -sL https://onprem.caura.ai/upgrade.sh | sudo bash -s -- --to v2.11.16 --health-timeout 600
```
- Backfills `GATEWAY_SHARED_SECRET` into `.env` (satisfies #802; core-api won't boot without it).
- Runs 034–039: 035 now near-instant (indexes exist); 037's heap scans + 038's `documents`
  scan absorbed by the 600 s gate.
- Health-gates with auto-rollback (restores previous `MEMCLAW_VERSION`).

### Phase 3 — restore override-only state (recurring trap)
upgrade.sh runs `-f docker-compose.yml` (+ auto-detected TLS/embedder overlays), **not** the
override — so `shm_size:1g`, re-homed `core-operations`, and `RESERVED_AGENT_ID_POLICY` revert.
Bump the ops pin, then recreate with the **full overlay set** (eToro is `letsencrypt`):

```bash
# in /opt/memclaw
sudo sed -i 's/^MEMCLAW_OPS_VERSION=.*/MEMCLAW_OPS_VERSION=v2.11.16/' .env
sudo docker compose -f docker-compose.yml -f docker-compose.tls-letsencrypt.yml -f docker-compose.override.yml up -d
```
> Omitting `docker-compose.tls-letsencrypt.yml` makes the base gateway try to publish `:80`,
> which clashes with caddy (owns host `:80`/`:443`) → "port already allocated". Always include it.

### Phase 4 — verify
- `alembic` head = **039** (`SELECT version_num FROM alembic_version;`).
- All 4 indexes `indisvalid = true` (035's three + 037's `ix_memories_stale_embedding`).
- Secret convergence: `.env` == core-api == gateway `GATEWAY_SHARED_SECRET`; rendered nginx
  injects it (`grep X-Gateway-Secret` in the gateway's `/etc/nginx/snippets`).
- **Live hop:** authenticated request through the edge returns **200**, `0`×`401` in core-api logs:
  ```bash
  # via the caddy domain, e.g. --resolve <domain>:443:127.0.0.1
  curl -sk --resolve <domain>:443:127.0.0.1 -H "X-Admin-API-Key: $CORE_ADMIN_API_KEY" \
    -o /dev/null -w '%{http_code}\n' https://<domain>/v1/memories/stats
  ```
- `RESERVED_AGENT_ID_POLICY=reject` on core-api; postgres `ShmSize=1073741824`.
- Fleet watch to plugin floor 2.16.0.

### Rollback
`sudo /opt/memclaw/scripts/rollback.sh` (or upgrade.sh's auto-rollback restores the prior
version). Pre-built indexes are safe to leave in place after a rollback.

## 4. Open dependencies / caveats
- **eToro VPN/SSH** required; cutover needs **explicit human go**.
- **Phase 1 pre-build is a step beyond the bare documented upgrade** — online and non-locking,
  but confirm it is acceptable before running.
- Air-gap tarball for v2.11.16 is **not published** (embedder build blocked on `OSS_REPO_TOKEN`
  access to the renamed `caura-onprem` repo). eToro uses the online `upgrade.sh` path, so this
  does not block the cutover; it only affects air-gap customers.
- DB reads must be transaction-wrapped and read-only per standing eToro policy.
