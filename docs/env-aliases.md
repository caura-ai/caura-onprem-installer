# Environment variable names — the legacy alias table

Every setting this installer reads has two spellings. The `CAURA_*` name is the
one the docs teach; the older `MEMCLAW_*` name is a permanent alias and keeps  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
working. Nothing here is deprecated on a timer — an install that has only ever
set the old names needs no change, ever.

This is the one table. Every other doc, template and script teaches the
`CAURA_*` name and links here rather than repeating the pairs.

## How a name resolves

**The first name holding a NON-EMPTY value wins.** Not the first one that
happens to be set — the first one with something in it.

That distinction matters because these names land in `install.conf` and `.env`,
files you edit by hand. Part-way through adopting the new spelling, the ordinary
state of such a file is a new name present and blank next to an old one that
still has the value in it:

<!-- A raw <pre> block rather than a fenced one, deliberately. This example has
     to spell the old name, so the line needs the ratchet's exemption marker —
     and inside a ``` fence that marker renders to the reader as literal text,
     which put an internal CI token in the middle of customer-facing prose.
     GitHub strips an HTML comment out of a raw <pre> before it reaches the
     page, so here the marker is invisible where it has to be. -->
<pre><code>CAURA_VERSION=            # started migrating, not filled in yet
MEMCLAW_VERSION=v2.8.4    # what the stack is actually running<!-- legacy-name-ok: this table's worked example -->
</code></pre>

The stack runs `v2.8.4`. A blank never wins, and it never silently blanks a
setting that was working. If both are filled, the `CAURA_*` one wins. If neither
is set, the built-in default applies exactly as before.

The same rule holds at every layer: shell variables in `install.sh` and
`upgrade.sh`, keys in `.env` read by Docker Compose, keys in `install.conf`, and
the `cauractl` CLI's own options.

## Precedence between sources

Unchanged by the aliases, and the same for both spellings:

```
CLI flags  >  environment variables  >  --config file  >  built-in defaults
```

## `install.conf` keys

| Teach this | Still accepted | What it sets |
|---|---|---|
| `caura_home` | `memclaw_home` | Install root |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `caura_version` | `memclaw_version` | Image tag every service runs |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->

Every other key in `install.conf` (`hostname`, `admin_email`, `license`, the
`postgres_*` family, …) carries no brand and is unchanged.

## Environment variables

| Teach this | Still accepted | What it sets |
|---|---|---|
| `CAURA_HOME` | `MEMCLAW_HOME` | Install root (default `/opt/memclaw`) |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_VERSION` | `MEMCLAW_VERSION` | Image tag every service runs |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_OPS_VERSION` | `MEMCLAW_OPS_VERSION` | Scheduler/worker image tag; blank tracks `CAURA_VERSION` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_HOSTNAME` | `MEMCLAW_HOSTNAME` | Public hostname; must match the TLS cert CN |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_OFFLINE` | `MEMCLAW_OFFLINE` | `true` selects the air-gap compose overlay |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_SKIP_ADMIN` | `MEMCLAW_SKIP_ADMIN` | Leave first-admin creation to the web wizard |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_YES` | `MEMCLAW_YES` | Assume yes at `upgrade.sh` prompts |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **Admin bootstrap** | | |
| `CAURA_ADMIN_EMAIL` | `MEMCLAW_ADMIN_EMAIL` | First admin's email |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_ADMIN_PASSWORD` | `MEMCLAW_ADMIN_PASSWORD` | First admin's password (prefer the `_FILE` form) |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_ADMIN_PASSWORD_FILE` | `MEMCLAW_ADMIN_PASSWORD_FILE` | File holding that password |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **Licence** | | |
| `CAURA_LICENSE` | `MEMCLAW_LICENSE` | Path to `license.key` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_LICENSE_URL` | `MEMCLAW_LICENSE_URL` | URL to fetch the licence from |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **Secrets on disk** | | |
| `CAURA_JWT_SECRET_FILE` | `MEMCLAW_JWT_SECRET_FILE` | File holding the JWT secret; generated if unset |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_POSTGRES_PASSWORD_FILE` | `MEMCLAW_POSTGRES_PASSWORD_FILE` | File holding the Postgres password; generated if unset |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_CORE_ADMIN_API_KEY_FILE` | `MEMCLAW_CORE_ADMIN_API_KEY_FILE` | File holding the core admin API key; generated if unset |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_OPENAI_API_KEY_FILE` | `MEMCLAW_OPENAI_API_KEY_FILE` | File holding the OpenAI key |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **External Postgres** (blank = the bundled service) | | |
| `CAURA_POSTGRES_HOST` | `MEMCLAW_POSTGRES_HOST` | External DB host |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_POSTGRES_PORT` | `MEMCLAW_POSTGRES_PORT` | External DB port |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_POSTGRES_USER` | `MEMCLAW_POSTGRES_USER` | External DB user |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_POSTGRES_DB` | `MEMCLAW_POSTGRES_DB` | External DB name |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_POSTGRES_REQUIRE_SSL` | `MEMCLAW_POSTGRES_REQUIRE_SSL` | Require TLS to the external DB |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **Providers** | | |
| `CAURA_LLM_PROVIDER` | `MEMCLAW_LLM_PROVIDER` | `openai` \| `anthropic` \| `gemini` \| `local` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_EMBEDDING_PROVIDER` | `MEMCLAW_EMBEDDING_PROVIDER` | `local` \| `openai` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_LOCAL_EMBEDDINGS` | `MEMCLAW_LOCAL_EMBEDDINGS` | Force the embedder overlay on or off |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_EMAIL_PROVIDER` | `MEMCLAW_EMAIL_PROVIDER` | `log` \| `sendgrid` \| `resend` \| `smtp` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **TLS** | | |
| `CAURA_TLS_MODE` | `MEMCLAW_TLS_MODE` | `self-signed` \| `byo` \| `letsencrypt` \| empty (HTTP-only) |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_TLS_CERT_FILE` | `MEMCLAW_TLS_CERT_FILE` | Bring-your-own certificate |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_TLS_KEY_FILE` | `MEMCLAW_TLS_KEY_FILE` | Bring-your-own private key |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_TLS_DOMAIN` | `MEMCLAW_TLS_DOMAIN` | Publicly-resolvable FQDN for Let's Encrypt |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_TLS_EMAIL` | `MEMCLAW_TLS_EMAIL` | Contact address the CA uses for renewal notices |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_ACK_INSECURE` | `MEMCLAW_ACK_INSECURE` | Acknowledge running without TLS |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_BIND_ADDRESS` | `MEMCLAW_BIND_ADDRESS` | Gateway bind address (default `0.0.0.0`) |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **Installer plumbing** | | |
| `CAURA_BUNDLE_DIR` | `MEMCLAW_BUNDLE_DIR` | Use a bundle already on disk instead of fetching |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_BUNDLE_URL` | `MEMCLAW_BUNDLE_URL` | Where to fetch `bundle.tar.gz` from |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_UPGRADE_URL` | `MEMCLAW_UPGRADE_URL` | Where `upgrade.sh` re-downloads itself from |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_UPGRADE_REEXEC` | `MEMCLAW_UPGRADE_REEXEC` | Internal re-exec guard; not a knob to set |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| **`cauractl`** | | |
| `CAURA_URL` | `MEMCLAW_URL` | Base URL of the stack |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_ADMIN_KEY` | `MEMCLAW_ADMIN_KEY` | Admin JWT |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_API_KEY` | `MEMCLAW_API_KEY` | Per-tenant API key for `memory export` / `import` |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
| `CAURA_SUPPORT_URL` | `MEMCLAW_SUPPORT_URL` | Support-bundle upload endpoint |  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->

## Three names that keep the old spelling only

`MEMCLAW_API_URL`, `MEMCLAW_SITE_URL` and `MEMCLAW_BILLING_ENABLED` are set on  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
the `app-frontend` service in `docker-compose.yml` and read **inside the
application image**, which is built elsewhere. This installer only writes them.
Giving them a `CAURA_*` spelling here would name something no shipped image
reads, so they are left alone until the application image moves. Do not "finish"
them by hand — see `docs/troubleshooting.md`.

## The registry image names do move

The nine `ghcr.io/caura-ai/memclaw-*` images in `docker-compose.yml` and  <!-- legacy-name-floor: names the GHCR packages that are retained after the move -->
`docker-compose.embedder.yml` are the one brand surface here that is **not**
frozen. New versions publish under `ghcr.io/caura-ai/caura-*`. GHCR has no
rename, so the old packages are retained indefinitely rather than redirected.

Nothing is asked of an operator either way. A connected upgrade re-fetches the
bundle and extracts it over the install (`upgrade.sh:291-292`), which replaces
`docker-compose.yml` — so it picks up the new names by itself. An install that
never upgrades keeps pulling the name already in its compose file, which is
exactly why the old packages stay.

**They are deliberately absent from the list below.** That list is names that
*cannot* move; these can, and the distinction is the whole difference between
a registry path and a customer's disk. The `memclaw-onprem/*` air-gap tags look  <!-- legacy-name-floor: distinguishes the frozen air-gap tags from the registry names -->
like registry names and are not — they are inside tarballs customers already
hold, so they belong to the list, not to this section.

## Names that are not aliases

These carry the old brand because something outside this repo depends on the
exact characters, and they are not settings you can re-spell:

- `/opt/memclaw` and `/var/log/memclaw/` — paths on customer disks.  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
- The `POSTGRES_DB` / `POSTGRES_USER` defaults — the database an existing install
  already owns its data under.
- The `memclaw-onprem/*` air-gap image names — already inside tarballs customers hold.  <!-- legacy-name-ok: the legacy alias table, which is the surviving footprint -->
- `memclawctl` — the console script an existing install already has on `$PATH`.  <!-- legacy-name-ok: a permanent console-script alias, which rule 3 keeps working -->
  The command is `cauractl` now; both entry points resolve to the same
  implementation, and dropping the old one is what would break a runbook.

`scripts/do_not_touch_sentinel.py` fails CI if any of them is removed.
