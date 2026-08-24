# Logging & support bundles

On-prem MemClaw writes structured JSON logs to two places:

- **stdout** of each container — captured by `docker compose logs` and
  anything your log shipper (Loki, Promtail, Filebeat, cloud agent) is
  configured to read from the Docker engine.
- **`$CAURA_HOME/logs/<service>/<service>.log`** — on-disk files,
  rotated daily at UTC midnight, **5-day retention**. Nothing reaches
  out to external log services without your configuration.

This doc covers the file sink, the support-bundle tool that reads it,
and the two upload paths (air-gapped email vs connected HTTPS).

## Layout

```
/opt/memclaw/logs/
├── core-api/
│   ├── core-api.log                # live
│   ├── core-api.log.2026-04-19
│   ├── core-api.log.2026-04-18
│   ├── core-api.log.2026-04-17
│   └── core-api.log.2026-04-16
├── core-storage-api/
├── platform-admin-api/
├── platform-auth-api/
├── platform-audit-api/
├── platform-storage-api/
└── gateway/                        # nginx access/error logs
```

Each service runs as `appuser` inside its container. `install.sh`
creates the host directories with mode `0777` so whatever UID
`useradd` resolves to (typically 1000 on Debian-slim bases) can write.
The files themselves land at `0644` owned by the container UID.

## Log format

Every platform/core service uses `common.structlog_config` which
emits one JSON line per event:

```json
{
  "event": "onprem_heartbeat_ok",
  "level": "info",
  "timestamp": "2026-04-22T10:14:03Z",
  "logger": "caura_ops.api.routes.onprem_heartbeat",
  "license_id": "lic-abc-123",
  "deployment_id": "dep-def-456",
  "version": "1.2.3"
}
```

`LOG_FORMAT_JSON=false` in `.env` switches to structlog's coloured
console renderer — useful for local development, never in production
(breaks grep pipelines).

## Disabling the file sink

Remove `LOG_FILE=` from a service's environment in
`docker-compose.yml` OR set `LOG_FILE=""` in your `.env` override.
Stdout keeps working. We recommend leaving the file sink on — it's
cheap (≤ a few GB/day at normal traffic), and the support-bundle tool
needs it.

## Rotation parameters

Hard-coded in `common.structlog_config.configure_logging`:

- `when="midnight"`, `interval=1`, `utc=True` — daily at 00:00 UTC
- `backupCount=5` — keep 5 previous days plus the live file
- `delay=True` — the handler doesn't open the file until the first
  log event, so a clean restart after a disk fill doesn't immediately
  re-fail

To change retention, rebuild the service images with a patched
`structlog_config.py` — there is no runtime override on purpose (the
5-day window is intentionally aligned with the support-bundle window
so every bundle contains the full retention history).

## Support bundles

`memclawctl support bundle` produces a redacted tarball for Caura
support triage:

```
$ memclawctl support bundle
Collecting from /opt/memclaw → /tmp
Bundle ready: /tmp/memclaw-support-host-20260422T114502Z.tar.gz  (14.3 MiB)
Secrets redacted. Review with: memclawctl support review …
```

### What's inside

```
memclaw-support-<host>-<timestamp>.tar.gz
├── manifest.json                    # collected_at, license_id, version, sha256
├── logs/
│   ├── core-api/core-api.log        # redacted; 5 days
│   └── …/
├── compose/
│   ├── ps.txt
│   ├── config.txt                   # ← `.env` secrets redacted
│   ├── top.txt
│   └── images.txt
├── host/
│   ├── uname.txt
│   ├── disk.txt
│   └── docker-info.txt
└── config/env.redacted              # JWT/password/API-key values replaced
```

### Redaction

Runs in-memory, per file, before it hits the tarball. Patterns cover:

- JSON fields: `password`, `api_key`, `jwt_secret`, `settings_encryption_key`,
  `openai_api_key`, `anthropic_api_key`, `gemini_api_key`,
  `postgres_password`, `github_client_secret`, `email_api_key`,
  `smtp_password`, `license_key`, `authorization`, `cookie`, `token`, `secret`.
- `.env` shell lines for the same names.
- Generic stragglers: `Bearer <token>`, `sk-…`, `mc_admin_…`, 3-part JWTs.

`memclawctl support review <bundle>` runs a second-pass shape scanner
on the already-redacted bytes and exits non-zero if anything slips
through. Chain it in your delivery process:

```
memclawctl support bundle --out-dir /tmp --notes "recall 503 under load"
memclawctl support review /tmp/memclaw-support-*.tar.gz
```

Oversized files (> 50 MiB after redaction) are trimmed to head + tail
with a visible marker line between them. This is a hard cap — the
goal is an emailable bundle.

### Delivery — air-gapped

`memclawctl support bundle` writes a tarball to `/tmp`. Copy it off
the host via whatever your operations allows (USB, SFTP jumpbox,
signed transfer) and email it to support@caura.ai. No network access
from the on-prem host is needed.

### Delivery — connected

```
memclawctl support upload /tmp/memclaw-support-*.tar.gz
```

HMAC-SHA256 signed with a key derived from `(license_id, issued_at)`
from the installed license. The endpoint verifies against the matching
license row server-side — no shared secret is needed. Default
endpoint is `https://support.caura.ai/api/onprem/support`; override
with `--endpoint` or `CAURA_SUPPORT_URL` for self-hosted support
portals.

Uploads are gated on the leak scanner by default. If the scanner
flags a shape that would leak a secret, `support upload` refuses with
exit code 1. Pass `--skip-leak-scan` after manually reviewing.

### What we do with bundles

Each upload lands in a dedicated GCS bucket under
`onprem/<license_id>/<YYYY>/<MM>/<sha256>.tar.gz` with 15-minute
signed download URLs issued only when a superadmin opens the bundle
in the dashboard. Bundles are retained for 90 days unless escalated
to a formal support case, at which point they're archived alongside
the case. Access is audited.

Delete-on-request: email support@caura.ai referencing the bundle's
`sha256` to have it purged from GCS and the metadata row soft-deleted.

## Observability outside the bundle

- **Prometheus scrape** — `/metrics` on each service; opt-in.
  See `day2-ops.md` for the scrape config snippet.
- **Health endpoints** — `/healthz` (liveness), `/readyz` (readiness).
- **License status** — `GET /api/license/status` returns the active
  license's claims, days remaining, and current usage vs limits.
