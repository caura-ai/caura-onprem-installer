# memclawctl

Release A also installs `cauractl`; both console entries load this same Click
command group, while existing runbooks keep their established default.

Day-2 operations CLI for an on-prem Caura deployment. Talks to the
running stack over HTTP — no direct DB access — so the same tool works
in connected and air-gapped installs.

## Install

On the host, which is the only place it runs today:

```
pipx install ./tools/memclawctl      # editable for dev
pip install caura-memclawctl         # once published
```

### There is no in-image path yet

This section used to say the CLI was *"shipped inside the app-frontend /
platform-admin-api images"* and could be run with
`docker compose exec platform-admin-api memclawctl <command>`. **Neither image  <!-- legacy-name-floor: quotes the removed instruction verbatim; the shipped CLI's own command -->
contains it**, so that command returns
`exec: "memclawctl": executable file not found in $PATH`. Three independent  <!-- legacy-name-floor: the error string a customer actually sees -->
reasons, none of which a change in this repo can alter:

- Both images are built in `caura-ai/caura-enterprise`. That repository has no
  `tools/` tree at all, and `platform-admin-api/Dockerfile` copies only the
  workspace manifests, its own `src/`, `common/` and its entrypoint — so this
  package is not in the build context.
- `app-frontend` is a `node` build stage serving a static export from
  `nginx:alpine`. It has no Python interpreter, so installing a Python CLI there
  means adding a runtime nothing else in the image uses.
- Neither on-prem repo builds an image that installs it either: `nginx/` is
  `nginx:1.27-alpine` plus `gettext`, and `core-api-embedder/` layers
  sentence-transformers onto the core-api image.

Whether an image *should* carry the CLI is an open question for whoever owns
those recipes; it is not answerable from here. Until it is answered, the host
install above is the supported path.

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

Every command runs on the host. The column that used to sit beside this one
described a `docker compose exec` path that does not exist — see above.

| Command | Needs host access beyond the HTTP API |
|---|---|
| status, license, api, memory export/import | no — HTTP only |
| setup | no — HTTP only, but it prompts interactively |
| backup, restore, upgrade, rollback | **yes** — Docker socket and the install directory |
| plugin install-url | no — prints a command, calls nothing |

That last row is the one that would still matter if an in-image path were ever
built: those four cannot work from inside a service container regardless of
whether the CLI is installed there.

## Authentication

The admin JWT comes from one of two places:

- `--admin-key` on the command line, or
- `CAURA_ADMIN_KEY` — or `MEMCLAW_ADMIN_KEY`, its permanent alias.  <!-- legacy-name-ok: teaches the dual-read spelling cli.py actually reads -->

An empty key is not an error. `_client` attaches an `Authorization` header only
when one is set, so the endpoints that do not need it still work.

`--password-stdin` flows read stdin directly and never echo.

**There is no `login` command and no credentials file.** An earlier version of
this section described a credentials file in the operator's home directory
written by a `login` subcommand, and an "inside the admin container" path using
a service-internal key. None of the three exists: `cli.py` resolves the key
from the flag and those two environment variables and nothing else, and `src/`
contains no `login` command and no reference to a credentials file.
