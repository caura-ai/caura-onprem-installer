# Admin audit transport and compatible images

The broker-free installer requires an admin image labeled
`ai.caura.admin-audit-transport=storage-http-inprocess-v1`. This capability marks
the direct storage audit implementation. No minimum version is guessed before
the first compatible release is tagged. Supply the version of a release carrying
this capability with `install.sh --version <version>` or
`upgrade.sh --to <version>`; older pinned versions are refused, including the
installer's historical default. A run without an explicit version fails before
any image pull with instructions to supply a compatible release. Obtain that
paired release from Caura.

Install checks the target admin image before writing install files. Upgrade
checks it before the database backup, compose refresh, or version update.
Connected mode pulls the target admin image to inspect its label. Offline mode
inspects the already-loaded `memclaw-onprem/platform-admin:<version>` image. <!-- legacy-name-floor: published offline alias retained for existing image bundles -->
Missing images, failed inspection, and missing/wrong capability all stop the
operation. Mutable `latest` tags are checked too. A recorded downgrade to an older numeric previous version restores its version-bound compose
and nginx snapshot. An unlabeled image is permitted only if that snapshot
restores RabbitMQ. Other older targets require the older matching installer
and image bundle; a broker-free snapshot still requires the capability.

With the capability present, admin events write directly to platform-storage
over HTTP when the resolved event bus is in-process. The separate audit
container remains for broker HTTP uploads. Pub/Sub deployments retain their
consumer delivery path. Storage must already have the audit identity/time
migration (042); follow the normal storage-first release procedure.

Direct writes are awaited but best-effort, with no durable retry queue. Monitor
admin logs for `Failed to persist admin audit event (non-fatal)` and
`dropped=True`; storage outages can still lose events. The change cannot recover
events already lost by older images. Verify an authorized admin mutation
produces exactly one audit row with its expected tenant/organization, action,
event ID and occurrence time before accepting the upgrade.

Postgres backups retain persisted audit rows. Restore accepts older archives,
restores Postgres/Redis, and ignores legacy RabbitMQ definitions without changing
the archive. See [day-2 operations](day2-ops.md#retired-broker-artifacts) for
orphan-container cleanup and offline rollback image/volume preservation.


## MCP entitlement reasons

With the paired MCP entitlement fix, auth and the gateway forward an explicit
read-only reason. Compatible core-api images always enforce missing/expired
licenses on MCP mutations, while reads remain available. Organization quota
restrictions follow REST's existing policy, including its update/deletion
exceptions. The usage rollout flag cannot disable an authoritative reason.

Use updated auth and core images together with this gateway template, and
rebuild the gateway through the normal upgrade procedure. An old auth/gateway
or old core retains the prior boolean-only limitation. Verify authorized
license-expiry and organization-read-only MCP calls and an unrestricted
follow-up request before accepting the rollout; CI does not verify a customer
installation. Existing license grace periods and quota thresholds are unchanged.
