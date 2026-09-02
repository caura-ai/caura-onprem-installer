# Caura Enterprise — On-Prem Deployment

> To protect existing deployments, installed-machine
> identifiers (`/etc/memclaw`, `MEMCLAW_*` vars, the `memclaw` postgres  <!-- legacy-name-floor: lists the frozen installed-machine identifiers -->
> role, the `memclaw-onprem/*` air-gap image tags) are unchanged,  <!-- legacy-name-floor: lists the frozen installed-machine identifiers -->
> permanently: existing installs need nothing.
>
> **Registry image names are the exception.** New versions publish under
> `ghcr.io/caura-ai/caura-*`. GHCR has no rename, so the `memclaw-*`  <!-- legacy-name-floor: the retained GHCR packages a pinned install still resolves -->
> packages are retained indefinitely rather than redirected — an install
> that never upgrades keeps pulling the name in the compose file it
> already holds. Detail in [`docs/env-aliases.md`](docs/env-aliases.md).

Self-hosted Caura Enterprise as a Docker Compose stack. Runs on a
single VM (connected or air-gapped) and is fully managed via a signed
license file issued by Caura.

## Quickstart (connected VM)

```bash
curl -sL https://onprem.caura.ai/install.sh | bash
```

The installer stands up the stack with sane defaults, then prints the URL
of the first-run wizard where an admin uploads the license and creates the
first user account.

## Quickstart (air-gapped VM)

1. Transfer the release tarball to the VM (any medium — USB, SCP over an
   internal bastion, etc.):
   ```
   memclaw-onprem-<version>.tar.gz
   ```
2. Load the bundled images locally:
   ```
   ./airgap-load.sh
   ```
3. Run the installer in offline mode:
   ```
   ./install.sh --offline --license /path/to/license.key
   ```

## Silent / unattended install

Supports every field via CLI flag, env var (`CAURA_*`), or a single
config file:

```bash
./install.sh --config /etc/memclaw/install.conf --non-interactive
```

See `install.conf.example` for the full template.

Every setting also answers to its older `MEMCLAW_*` name — permanently, with no  <!-- legacy-name-ok: teaches the old spelling so a reader recognises it -->
deprecation date, so an existing install needs no change. The one table of both
spellings is [`docs/env-aliases.md`](docs/env-aliases.md).

## Repository layout

```
.
├── docker-compose.yml          # connected stack (pulls from ghcr.io/caura-ai/*)
├── docker-compose.airgap.yml   # overlay: uses locally-loaded images
├── .env.example                # consolidated env template
├── install.conf.example        # silent-install config template
├── install.sh                  # one-liner installer (TBD Phase 5)
├── airgap-load.sh              # docker load < tarball (TBD Phase 4)
├── license/
│   └── README.md               # customer drops license.key here
├── nginx/
│   └── memclaw.conf.template   # SERVER_NAME substituted at container start
├── scripts/
│   ├── backup.sh               # pg_dump + redis BGSAVE
│   ├── restore.sh              # pair to backup.sh
│   ├── set-version.sh          # bump the pinned image tag in an install's .env
│   └── verify/                 # end-to-end smoke scripts
└── docs/
    ├── install.md
    ├── install-airgap.md
    ├── day2-ops.md
    ├── logging.md               # log layout + support-bundle workflow
    ├── upgrade.md
    ├── env-aliases.md            # the one table of old/new env-name pairs
    └── troubleshooting.md
```

## Support

- Issue tracker: contact your Caura representative.
- Status dashboard: `https://ops.caura.ai` (Caura-operated).
- Support bundles: `memclawctl support bundle` produces a redacted
  tarball you can email or upload. See [`docs/logging.md`](docs/logging.md).
