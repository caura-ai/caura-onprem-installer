# memclawctl

Day-2 operations CLI for an on-prem Caura deployment. Talks to the
running stack over HTTP — no direct DB access — so the same tool works
in connected and air-gapped installs.

## Install

Shipped inside the app-frontend / platform-admin-api images; run from
a compose exec:

```
docker compose exec platform-admin-api memclawctl <command>
```

Or install standalone on the host:

```
pipx install ./tools/memclawctl      # editable for dev
pip install caura-memclawctl         # once published
```

## Commands

| Command | Purpose |
|---|---|
| `memclawctl status` | Print `/setup/status` + `/license/status` side-by-side |
| `memclawctl setup` | Interactive first-run wizard (CLI alternative to `/setup`) |
| `memclawctl create-org --name Acme --slug acme` | Create another org |
| `memclawctl create-admin --org acme --email x --password-stdin` | Add an admin to an existing org |
| `memclawctl issue-api-key --org acme --label ci` | Mint a tenant API key |
| `memclawctl license load /path/to/license.key` | Hot-reload a new license |
| `memclawctl license status` | Pretty-print current license + days remaining |
| `memclawctl backup --out /backups/` | Wraps scripts/backup.sh |
| `memclawctl restore --from <tar.gz>` | Wraps scripts/restore.sh |
| `memclawctl upgrade --to <version> [--dry-run] [--no-backup] [-y]` | Delegates to `$CAURA_HOME/upgrade.sh` — pre-upgrade pg_dump, pull, rolling up, health-stability check, auto-rollback |  <!-- legacy-name-floor: the shipped CLI's own command -->
| `memclawctl rollback [-y]` | Roll back to the version recorded in `.memclaw-prev-version` (written by upgrade.sh) |
| `memclawctl plugin install-url --fleet-id <id> [--api-url ...] [--api-key ...]` | Print the exact `curl -X POST \| bash` command a customer runs on an OpenClaw VM |
| `memclawctl memory export <tenant> --api-key mc_...` | Stream all memories for a tenant to JSONL |
| `memclawctl memory import <tenant> --api-key mc_... --file dump.jsonl` | Load a JSONL dump back into a tenant |
| `memclawctl api <METHOD> <PATH> [--body file\|-] [--api-key mc_...]` | Generic authenticated passthrough to the running stack |

### Where each command runs

| Command | Host | `docker compose exec platform-admin-api` |
|---|---|---|
| status, license, api, memory export/import | ✓ | ✓ |
| setup | ✓ | ✓ (but host is easier — interactive prompts work) |
| backup, restore, upgrade, rollback | ✓ | ✗ (need Docker socket + filesystem access on the host) |
| plugin install-url | ✓ | ✓ |

## Authentication

- Inside the admin container: uses the service's internal admin key.
- From the host: reads `~/.memclaw/credentials` (set by `memclawctl login`),
  which stores a short-lived admin JWT.
- For `--password-stdin` flows: reads stdin directly, never echoes.
