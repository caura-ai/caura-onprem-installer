# Install guide (connected)

This guide walks you through installing MemClaw Enterprise on a
customer-managed VM with outbound internet access. For air-gapped
installs see `install-airgap.md`.

## Prerequisites

- Ubuntu 22.04 or equivalent Linux distribution.
- **Docker ≥ 24** and **docker compose v2** (both come bundled with
  recent Docker Desktop / Docker CE).
- **8 GB RAM** (16 GB recommended for >50 concurrent users).
- **50 GB disk** free for Postgres + Redis + RabbitMQ.
- A DNS record pointing to the VM (e.g. `memclaw.acme.local → 10.0.0.42`).
- A TLS certificate for that hostname (`cert.pem` + `key.pem`).
- A **license key** issued by Caura (`.key` file).

## Option A — zero-config one-liner

```bash
curl -sL https://onprem.caura.ai/install.sh | bash
```

What it does:

1. Runs preflight checks (Docker, RAM, disk).
2. Generates a secure `JWT_SECRET`, `POSTGRES_PASSWORD`, and admin API key.
3. Writes `.env` to `/opt/memclaw/`.
4. Pulls pinned images from `ghcr.io/caura-ai`.
5. Starts the stack with `docker compose up -d`.
6. Prints a URL like `https://<detected-host>/setup`.

Visit the URL in a browser, upload your license, create the first admin,
and you're done.

## Option B — silent install (Ansible / Terraform / runbook)

Provide every required value upfront via a config file:

```bash
./install.sh --config /etc/memclaw/install.conf --non-interactive
```

See `install.conf.example` for the full template. The installer writes a
machine-readable `install-result.json` for your automation to consume.

## Verifying the install

```bash
curl -sk https://$PUBLIC_HOSTNAME/api/version
curl -sk https://$PUBLIC_HOSTNAME/api/license/status \
  -H "Authorization: Bearer $ADMIN_JWT"
```

The license status should return `{"configured": true, "severity": "ok", ...}`.
Log in to the dashboard; a yellow or red banner appears at the top when
the license is approaching expiry.

## Upgrading

See `upgrade.md`.

## Troubleshooting

See `troubleshooting.md`.
