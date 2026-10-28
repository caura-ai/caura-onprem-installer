# Host proof for first-run setup

Both setup writes require `setup_token` in the HTTPS JSON body alongside the
existing license proof. The first-run storage gate closes writes with 404 after
an organization exists. This capability covers the setup window; it is not a
per-request nonce or a new atomic first-admin lock.

The installer creates `setup-token` in the chosen install directory, outside
the writable license volume. New tokens use 32 random bytes as 64 hex characters.
Existing tokens are retained and may contain 32–256 printable non-space ASCII
characters. Symlinked, hard-linked, nonregular or malformed token files are refused.

The selected auth image must advertise `ai.caura.setup-proof=file-v1`. After
pulling/loading it, a short-lived helper using that image validates the file,
sets mode 0600 and assigns it to the image's actual appuser UID/GID. That helper
runs without networking or container logging, with a read-only root filesystem
and host mounts for only the proof file and helper script. Auth-api keeps its normal
non-root user and mounts only the proof file read-only. `SETUP_TOKEN_FILE` names
its container path, `/run/caura/setup-token`.

The proof does not enter `.env`, Compose interpolation, image arguments or
frontend runtime configuration. Existing `.env` permissions, ownership and
handling are unchanged. Compose operators do not need host read access to the
proof file; the Docker daemon mounts it for the service. Reading it for browser
or CLI setup requires authorized root/service-owner access. The installer prints
the file path, not its contents. Do not put proof in URLs, logs or support bundles.

Silent install captures the proof over the helper's attached stdout with
container logging disabled, then streams setup JSON to curl on stdin.
`cauractl setup` reads the host file by default; use `--setup-token-file` or the
CLI's `SETUP_TOKEN_FILE` override for another path, and `--password-stdin` for the
admin password. The CLI skips license upload when a license is already mounted.
The web wizard asks for the proof and keeps it only in component state.

Release the compatible auth image before publishing this installer bundle and
the updated CLI. The installer refuses an older image before service restart.
Publish both `scripts/setup-token.sh` and `scripts/setup_token.py` with matching
Compose/install/upgrade files. Local bundles are checked before backup; a failed
proof preparation after refresh restores the previous version/Compose files.
Dry-run and recorded rollback do not provision new proof. Forward upgrades
provision a missing file even on an incomplete older install.

Manual deployments can still use `SETUP_TOKEN` in auth-api's environment, with
the server's `SETUP_TOKEN_FILE` unset. The CLI still needs a local proof file:
use your authorized secret-management process to write the **same existing
environment-token value** into a mode-0600 file owned by the CLI operator. Do
not generate a different token or change `.env` access. Pass that file explicitly;
the install root's default `setup-token` file need not exist:

```bash
cauractl setup --setup-token-file /secure/operator-proof \
  --license /secure/license.key --email operator@example.com --org-name Acme \
  --password-stdin < /secure/admin-password
```

The password input is a separate private file. Only paths appear in arguments;
the CLI does not print the proof. Retain or remove the operator's copy according
to your secret-management policy. This CLI input file does not need a container
mount when the server uses the environment-token alternative.

For file delivery, mount a private file readable by the service UID, read-only, and
explicitly set the container's `SETUP_TOKEN_FILE`. An explicit file takes
precedence; unavailable or invalid file proof answers 503 without falling back
to the environment. Missing or mismatched caller proof answers 403. Completed
installs need no manual backfill or database migration. Deploy compatible setup
clients before exposing an incomplete host to setup traffic.
