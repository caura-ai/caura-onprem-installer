# Security model — Caura on-prem

Reference for customer CISOs, SOC teams, and auditors. Scope: the
on-prem deployment as shipped by this bundle, not Caura's hosted service.

## Trust boundaries

```
┌──── customer network ─────────────────────────────────────────────┐
│                                                                   │
│  agents / OpenClaw clients                                        │
│        │ X-API-Key (agent or tenant scope)                        │
│        ▼                                                          │
│  [ gateway (port 80) ]                                            │
│        │ auth_request → auth-api                                  │
│        │ inject X-Tenant-ID + X-Org-Read-Only                     │
│        ▼                                                          │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐         │
│  │ auth-api │admin-api │storage-  │audit-api │ core-api │         │
│  │          │          │  api     │          │          │         │
│  └────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘         │
│       │          │          │          │          │               │
│       ▼          ▼          ▼          ▼          ▼               │
│     postgres  redis                                              │
│                                                                   │
│  license.key  (RS256 JWT, mounted read-only into auth/admin-api)  │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
           ▲                                         │
           │ TLS terminated by customer's reverse    │ optional daily
           │ proxy (Caddy, Traefik, cloud LB) —      │ phone-home to
           │ gateway speaks plain HTTP internally    │ ops.caura.ai
           │                                         │ (HMAC-signed,
                                                     │ opt-out per license)
```

Everything inside the box is the customer's trust domain. The bundle
ships no outbound dependencies except the optional phone-home
(off by default on air-gap licenses).

## Data at rest

| Store         | Encryption | Notes |
|---|---|---|
| Postgres      | customer-managed (filesystem encryption / cloud disk) | no app-level DB encryption |
| Redis         | customer-managed                                        | STM + read-only status cache only — no persistent data |
| tenant settings | **Fernet (AES-128-CBC + HMAC-SHA256)**               | encrypted via `SETTINGS_ENCRYPTION_KEY` at the application layer — the key is in `.env`, rotate by re-keying and re-encrypting (tool shipping in a later version) |
| license.key   | signed + timestamped, not encrypted                     | contents are not sensitive; signature is verified at every service start + hourly thereafter |
| backups       | `pg_dump -Fc` + Redis RDB + config; customer-encrypted  | `scripts/backup.sh` produces a tarball; encrypt at the customer's choice of tool |

## Admin audit transport

With the admin audit transport fix, admin-api writes audit rows directly to
platform-storage over the internal network when `EVENT_BUS_BACKEND` resolves to
`inprocess`. Separate containers cannot exchange events through a process-local
bus. RabbitMQ is no longer part of the stack; audit-api remains for broker HTTP
uploads. Hosted deployments using Pub/Sub retain their consumer delivery path.

Direct writes are awaited but best-effort. Storage failures are logged with
`dropped=True` and do not block the originating admin operation; there is no
local queue or automatic retry/backfill. Postgres backups include persisted
audit rows. Use a service release containing this fix together with this
installer; see [compatible images and rollout](admin-audit-transport.md).

## Authentication + authorization

- **Users** — Argon2id password hashes, optional GitHub OAuth, optional
  TOTP 2FA.
- **Agents / API clients** — SHA-256-hashed API keys (prefix `mc_`), 
  scope is a single tenant. Rotated via `cauractl issue-api-key`.
- **License-driven read-only**: when the license is expired or the org
  is explicitly marked read-only, the gateway's `auth_request` surfaces
  an `X-Org-Read-Only: true` header that `enforce_usage_limits()` in
  core-api translates to HTTP 403 on every write. Reads unaffected.

Auth flow at the wire:
1. Client hits `/api/*` with `X-API-Key` or `Authorization: Bearer <JWT>`.
2. Gateway's `auth_request` subrequests auth-api's `/api/v1/auth/validate`.
3. auth-api validates (SHA-256 hash lookup or JWT signature), computes
   read-only state (in-memory 30s cache per tenant), returns the
   identity headers.
4. Gateway forwards the request + identity headers to the upstream; the
   raw API key is **stripped** on the `/mcp` path so core-api never
   sees it.

## Auth request rate limiting

Both `/api/auth/` and `/api/v1/auth/` share a per-peer-IP budget. The
gateway/compose fallback is 100 requests/second with burst 200; direct TLS fresh
installs explicitly write 10/20. Rejected requests receive HTTP 429, a
`RATE_LIMITED` JSON error and `Retry-After: 1` before reaching auth-api. This
applies to both HTTP-only and built-in TLS installs, including login, signup,
recovery, OAuth and credential verification. The budget is local to one gateway
instance; it is not a distributed or per-account control. Auth-api's session,
TOTP and account-lockout checks still apply.

The key uses the connection's peer address. Client-supplied `X-Forwarded-For`
and `X-Real-IP` do not create new budgets. A reverse proxy or NAT makes clients
share a budget. For busy shared ingress, set `AUTH_RATE_LIMIT_RPS` and
`AUTH_RATE_LIMIT_BURST` in the deployment's `.env` before recreating the gateway;
compose passes them through to the renderer. Gateway/compose fallbacks are `100` and `200`; empty values use those fallbacks. Both must be integers from `1` to `99999`, without
leading zeros; invalid values fail startup with a clear message. For example,
`AUTH_RATE_LIMIT_RPS=100` and `AUTH_RATE_LIMIT_BURST=200` raise the aggregate
budget while retaining a finite bound. Tune to observed traffic and monitor 429s.

`install.sh --tls-letsencrypt` always routes through its Caddy sidecar, so all
clients share one peer budget. For that mode, unspecified settings default to
100/200 from the installer; explicit env/config values still win. HTTP-only fresh installs and existing pre-limiter reruns also use 100/200;
other direct TLS fresh installs use 10/20. A manually applied Let's Encrypt overlay does not run the
installer's default resolution: set an appropriate budget explicitly in `.env`.

For existing installs upgraded by `upgrade.sh`, missing auth-limit keys are
backfilled as `AUTH_RATE_LIMIT_RPS=100` and `AUTH_RATE_LIMIT_BURST=200`, with a
notice showing each added value. Existing keys are preserved, including blank
values (which explicitly select the gateway fallback). Direct TLS fresh installs
keep 10/20. Manual upgrade operators should set an appropriate finite budget in
`.env` before rebuilding, rather than relying on the fresh-install fallback.

For a fresh install or a rerun of `install.sh`, put `auth_rate_limit_rps` and
`auth_rate_limit_burst` in `install.conf`, or supply the uppercase environment
variables. Resolution is environment > config file > existing saved value > defaults; the installer
validates them and writes the resolved values to `.env`. A rerun regenerates
`.env` but preserves nonblank saved auth-limit values unless overridden. Only
these two numeric keys are read, without sourcing the file; saved values pass
the same validation. Saved-value resolution runs after the Docker/sudo handoff; only explicit
operator inputs cross that handoff. An unreadable existing file then fails
safely rather than silently resetting the budget. `upgrade.sh` preserves existing `.env`
tuning while rewriting version keys and prints a notice before gateway rebuild
or restart when the new bundle enables the limiter. Invalid saved upgrade values
restore the pre-upgrade version/config and stop before gateway build or any
service restart. See the example config and
[upgrade notice](upgrade.md#gateway-auth-rate-limit-upgrade-notice).

The dashboard's `/auth/me` resolution and `/auth/workspaces` reads are driven
by mount, sign-in, workspace changes and explicit refresh events, rather than a
continuous auth polling loop. Normal single-user navigation has no sustained
10-request/second auth poll; simultaneous users/tabs or reconnect/error bursts
behind one peer can still exhaust the shared budget and need operator tuning.
This is a static caller check, not a load-test or customer-traffic guarantee.

Enforce per-client limits at a trusted upstream ingress if needed. The shipped
templates do not configure real-IP trust from a CIDR list; forwarded addresses
remain untrusted. Do not use arbitrary forwarding headers as the limiter key.
Local 429 responses retain HSTS in TLS mode; backend lockouts retain their own
recovery hints.

Both prefixes clear all caller identity headers and replace caller-supplied
`X-Gateway-Secret` with the gateway's configured value. This attests to the hop; auth-api still authenticates the
caller itself. The installer keeps auth-api's existing empty-secret behavior;
it does not enable a new auth-api perimeter requirement or provision secrets.
If an operator separately enables that requirement, deploy/rebuild the updated
gateway first and verify matching configuration before updating auth-api.

Install/upgrade from a bundle containing these templates and rebuild/recreate
the gateway; pulling only newer service images leaves its config unchanged.
Use the [upgrade guide](upgrade.md) for online and air-gapped steps. Confirm
normal sign-in/recovery through the gateway with designated test accounts and
check 429 monitoring separately. CI uses synthetic local upstreams and does not
prove a customer's deployed ingress, mail delivery or account recovery behavior.

## License mechanism

- **RS256 JWT** signed by CauraOps. Public key baked into every
  service image at `/app/common/license/keys/license_public.pem`.
- Claims include: `license_id`, `org_name`, `issued_at`, `expires_at`,
  per-limit caps (`max_users`, `max_writes_per_month`, etc.), `features`
  list, `phone_home_enabled` flag.
- Loader fails **closed** on signature mismatch (service refuses to
  start — the customer cannot edit or forge a license).
- Expiry has a **24-hour clock-drift tolerance** so NTP-adjacent
  glitches don't lock people out. Past 24h, the service flips to
  read-only until a valid license is dropped in.
- **Tampering** = loud failure at startup. `caura_write` returns 403
  within 30s of the license flipping via the existing 30s read-only cache.

## Phone-home (optional)

Only applies when `license.phone_home_enabled = true`. Air-gap licenses
are issued with this disabled; the on-prem client skips the scheduled
task entirely.

When enabled:
- HTTPS POST to `https://ops.caura.ai/api/onprem/heartbeat` once per 24h
- Body is aggregates only: writes/searches/recalls counters, current
  version, host OS family — **no tenant names, no memory contents, no
  user PII**
- Body is signed with HMAC-SHA256 using a key derived from the
  license_id + issued_at (both also in the JWT) — receiver verifies
  before recording
- Failures are silent + retried with jittered backoff. Never blocks
  the stack.

## Threat model — what we defend against

| Threat | Mitigation |
|---|---|
| License forgery | RS256 signature (private key is in CauraOps GCP Secret Manager, never leaves that boundary) |
| License tampering | Loader refuses to load on any signature mismatch, service doesn't start |
| License replay past expiry | Expiry enforced with 24h clock-drift tolerance; after that, writes return 403 |
| Stolen API key | Keys are scoped per-tenant; rotation via `cauractl issue-api-key`; revoke via admin-api |
| Token exfiltration via logs | JWTs + API keys are masked in structured logs (prefix-only) |
| Phone-home replay / spoof | HMAC-SHA256 over raw body with a key unique per-license; constant-time verify on CauraOps side |
| Malicious plugin source | `install-plugin` / `plugin-source` serve files from the committed `plugin/` tree — verify the `plugin-source-hash` matches on the client if you want an integrity gate |
| Gateway bypass | All `/api/*` routes are behind the single gateway; services are on an internal Docker network with no published ports by default (only `gateway:80` is exposed) |
| Privilege escalation within tenant | RBAC: `role ∈ {owner, admin, member}`; admin-api enforces org_role on every mutation; superadmin is cross-tenant only via explicit `super_admin` JWT claim |

## Threat model — what we DO NOT defend against

- **Compromised customer VM**: if an attacker has root on the VM, all
  bets are off (they can read `.env`, license, DB files).
- **Malicious operator with superadmin access**: internal threat; rely
  on your own SSO / IAM controls.
- **DoS**: the auth-path rate limit above is an application abuse bound, not
  L4 DDoS protection or a general API limiter. Put a publicly reachable stack
  behind suitable ingress protection.
- **Supply chain**: images are pulled from `ghcr.io/caura-ai/*`; verify
  provenance with `cosign verify` (signatures are keyless via sigstore
  OIDC) — we sign + publish SBOMs per release but don't re-verify
  runtime.

## Compliance posture

Not formally certified. Architecture is compatible with:
- **SOC 2 Type II**: audit trail (`platform-audit-api` + `enterprise.audit_log`),
  RBAC, at-rest encryption (customer-managed), password hashing.
- **HIPAA**: BAA needed; on-prem removes the hosted-service data-sharing
  boundary, but the customer assumes all PHI responsibilities.
- **FIPS 140-2**: Python `cryptography` + `python-jose` use OpenSSL;
  use a FIPS-validated OpenSSL build in the container base image if
  required. Not shipped as a default.

## Reporting vulnerabilities

`security@caura.ai` — GPG key at https://caura.ai/.well-known/security.txt.
Coordinated disclosure, 90 days.
