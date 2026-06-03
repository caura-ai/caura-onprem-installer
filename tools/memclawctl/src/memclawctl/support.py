"""Support-bundle collection for on-prem MemClaw deployments.

A support bundle is a redacted tarball containing everything a Caura
support engineer needs to triage an incident without shell access:
service logs (last 5 days, already rotated by TimedRotatingFileHandler),
compose state, license status, install manifest, host fingerprint.

Works offline (air-gapped customer runs `memclawctl support bundle`,
emails us the tarball) and connected (`memclawctl support upload`
ships it to support.caura.ai — implemented in a later step).

Design choices:
- Read log **files**, not `docker compose logs`. Files are already
  partitioned per service, already rotated, and already on disk.
  `docker logs` would require the docker socket and give back a
  less-structured blob.
- Redact in-place while streaming into the tarball — never write an
  unredacted intermediate file to /tmp, where a `support review`
  reader could pick up secrets by accident.
- Include a machine-readable `manifest.json` at the tarball root so
  CauraOps can index bundles by license_id + collected_at without
  re-parsing every file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console

console = Console()

DEFAULT_HOME = Path(os.environ.get("MEMCLAW_HOME", "/opt/memclaw"))

# Log sinks we expect to find under $MEMCLAW_HOME/logs/.  Keep in sync with
# docker-compose.yml log bind-mounts.
SERVICES = (
    "platform-storage-api",
    "platform-auth-api",
    "platform-admin-api",
    "platform-audit-api",
    "core-storage-api",
    "core-api",
    "gateway",
)

# Maximum size of any single file (after redaction) accepted into the
# bundle. Caps pathological log explosions from making the bundle
# un-emailable. When exceeded we include the HEAD + TAIL (most-recent
# events are usually what's needed) with a gap marker in the middle.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB

# Patterns stripped during redaction. Each is the field name (in JSON
# log lines or env files) whose value should be replaced with `***`.
# Regex is greedy on purpose — we'd rather over-redact than leak.
_JSON_FIELD_REDACT = re.compile(
    r'("(?:password|api_key|admin_key|jwt_secret|jwt|secret|token|authorization|'
    r"cookie|settings_encryption_key|openai_api_key|anthropic_api_key|gemini_api_key|"
    r"postgres_password|github_client_secret|email_api_key|smtp_password|license_key|"
    r'license|phone_home_url|x-license-signature)"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)

_ENV_LINE_REDACT = re.compile(
    r"^(JWT_SECRET|POSTGRES_PASSWORD|CORE_ADMIN_API_KEY|SETTINGS_ENCRYPTION_KEY|"
    r"OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|"
    r"PLATFORM_LLM_API_KEY|PLATFORM_EMBEDDING_API_KEY|"
    r"GITHUB_CLIENT_SECRET|EMAIL_API_KEY|SMTP_PASSWORD)=.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Generic "Bearer <token>" / "sk-..." / "mc_admin_..." stragglers that
# slip through field-based redaction (for example when an exception
# formatter prints a dict repr without JSON-quoting).
_GENERIC_TOKEN_REDACT = re.compile(
    r"(Bearer\s+[A-Za-z0-9._\-]+|sk-[A-Za-z0-9_\-]{20,}|mc_admin_[A-Za-z0-9]+|"
    r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})",
)


def _redact(blob: bytes) -> bytes:
    """Apply all redaction patterns to a byte blob.

    We work on bytes (not str) so binary log files — or a stray
    non-UTF-8 exception — don't raise in the middle of a bundle run.
    Decode permissively, redact, re-encode.
    """
    text = blob.decode("utf-8", errors="replace")
    text = _JSON_FIELD_REDACT.sub(r'\1"***"', text)
    text = _ENV_LINE_REDACT.sub(lambda m: m.group(0).split("=", 1)[0] + "=***", text)
    text = _GENERIC_TOKEN_REDACT.sub("***", text)
    return text.encode("utf-8", errors="replace")


def _cap_size(data: bytes, path: str) -> bytes:
    """Trim oversize files to HEAD + TAIL with a visible gap marker."""
    if len(data) <= MAX_FILE_BYTES:
        return data
    half = MAX_FILE_BYTES // 2
    head = data[:half]
    tail = data[-half:]
    gap = (
        f"\n\n--- [memclawctl: file {path} truncated — "
        f"{len(data):,} bytes, showing first + last {half:,}] ---\n\n"
    ).encode()
    return head + gap + tail


def _tar_add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _run(
    cmd: list[str], cwd: Path | None = None, timeout: int = 30
) -> tuple[int, bytes]:
    """Run a subprocess with output captured. Returns (returncode, stdout+stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        return 127, f"command not found: {e}\n".encode()
    except subprocess.TimeoutExpired:
        return 124, f"command timed out after {timeout}s: {' '.join(cmd)}\n".encode()
    return proc.returncode, (proc.stdout or b"") + (proc.stderr or b"")


def _iter_log_files(home: Path) -> Iterable[tuple[str, Path]]:
    """Yield (arcname, path) for every log file under $MEMCLAW_HOME/logs/.

    TimedRotatingFileHandler names rotated files <name>.<YYYY-MM-DD> so
    the glob catches today's live file + the 5-day retention window.
    """
    root = home / "logs"
    if not root.is_dir():
        return
    for svc in SERVICES:
        svc_dir = root / svc
        if not svc_dir.is_dir():
            continue
        for p in sorted(svc_dir.iterdir()):
            if p.is_file():
                yield f"logs/{svc}/{p.name}", p


def _deployment_fingerprint(home: Path) -> dict[str, Any]:
    """Identify the deployment without leaking anything sensitive.

    license_id is the JWT's `license_id` claim (an already-public UUID
    we signed into the license). We read it from `install.state.json`
    if present, otherwise parse the JWT header/payload (no signature
    check — we're not authorising anything, just tagging the bundle).
    """
    out: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": _safe_read_text(Path("/etc/os-release")) or "",
        "installed_at": None,
        "version": None,
        "license_id": None,
        "org_name": None,
    }
    state = home / "install.state.json"
    if state.is_file():
        try:
            s = json.loads(state.read_text())
            out["installed_at"] = s.get("installed_at")
            out["version"] = s.get("version")
        except (OSError, json.JSONDecodeError):
            pass

    lic_key = home / "license" / "license.key"
    if lic_key.is_file():
        claims = _unsafe_parse_jwt_payload(lic_key.read_text().strip())
        if claims:
            out["license_id"] = claims.get("license_id")
            out["org_name"] = claims.get("org_name")
    return out


def _safe_read_text(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _unsafe_parse_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode the payload of a JWT WITHOUT verifying the signature.

    We only use this to extract the `license_id` + `org_name` for
    bundle metadata. Signature verification happens in
    common.license.verifier inside the running service.
    """
    import base64

    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None


def build_bundle(
    home: Path,
    out_dir: Path,
    *,
    include_logs: bool = True,
    include_compose_state: bool = True,
    extra_notes: str | None = None,
) -> Path:
    """Build a support bundle tarball under out_dir. Returns its path.

    Exposed as a library function too so the `support upload` subcommand
    can call it without shelling back to the CLI.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    fp = _deployment_fingerprint(home)

    safe_host = re.sub(r"[^A-Za-z0-9_.\-]+", "-", fp["hostname"] or "unknown")
    bundle_name = f"memclaw-support-{safe_host}-{ts}.tar.gz"
    bundle_path = out_dir / bundle_name

    sha = hashlib.sha256()
    byte_total = 0
    file_total = 0

    # Stream into a tempfile first so a partial bundle never appears
    # at the final path. Rename on success.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".memclaw-support-", suffix=".tar.gz", dir=out_dir
    )
    os.close(tmp_fd)
    try:
        with tarfile.open(tmp_name, "w:gz") as tar:
            # ── logs ────────────────────────────────────────────────
            if include_logs:
                for arcname, path in _iter_log_files(home):
                    try:
                        raw = path.read_bytes()
                    except OSError as e:
                        _tar_add_bytes(
                            tar,
                            f"{arcname}.read-error.txt",
                            f"could not read {path}: {e}\n".encode(),
                        )
                        continue
                    redacted = _cap_size(_redact(raw), arcname)
                    _tar_add_bytes(tar, arcname, redacted)
                    sha.update(redacted)
                    byte_total += len(redacted)
                    file_total += 1

            # ── compose state ───────────────────────────────────────
            if include_compose_state:
                for label, cmd in _compose_snapshots(home):
                    rc, out = _run(cmd, cwd=home)
                    data = _redact(out)
                    header = f"# {' '.join(cmd)}  (rc={rc})\n".encode()
                    _tar_add_bytes(tar, f"compose/{label}.txt", header + data)
                    byte_total += len(data)
                    file_total += 1

            # ── host + deployment fingerprint ───────────────────────
            _tar_add_bytes(tar, "host/uname.txt", _host_uname())
            _tar_add_bytes(tar, "host/disk.txt", _host_disk(home))
            _tar_add_bytes(tar, "host/docker-info.txt", _docker_info())

            # ── redacted .env snapshot (for config triage) ──────────
            env_file = home / ".env"
            if env_file.is_file():
                _tar_add_bytes(
                    tar, "config/env.redacted", _redact(env_file.read_bytes())
                )

            # ── manifest ────────────────────────────────────────────
            manifest = {
                "schema_version": 1,
                "collected_at": datetime.now(UTC).isoformat(),
                "collector": "memclawctl",
                "collector_version": _self_version(),
                "deployment": fp,
                "byte_total": byte_total,
                "file_total": file_total,
                "sha256_of_redacted_payload": sha.hexdigest(),
                "notes": extra_notes or "",
            }
            _tar_add_bytes(
                tar, "manifest.json", json.dumps(manifest, indent=2).encode()
            )

        os.replace(tmp_name, bundle_path)
    except Exception:
        # Leave no orphan tempfile behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return bundle_path


def _compose_snapshots(home: Path) -> list[tuple[str, list[str]]]:
    """Compose-level state we want in every bundle.

    Split into discrete invocations so one failing command (e.g. no
    docker socket) doesn't black out the rest.
    """
    return [
        ("ps", ["docker", "compose", "ps", "--all", "--format", "json"]),
        ("config", ["docker", "compose", "config"]),
        ("top", ["docker", "compose", "top"]),
        ("images", ["docker", "compose", "images"]),
    ]


def _host_uname() -> bytes:
    rc, out = _run(["uname", "-a"])
    return out if rc == 0 else b"uname unavailable\n"


def _host_disk(home: Path) -> bytes:
    rc, out = _run(["df", "-h", str(home)])
    return out if rc == 0 else b"df unavailable\n"


def _docker_info() -> bytes:
    rc, out = _run(["docker", "info"])
    return out if rc == 0 else b"docker info unavailable\n"


def _self_version() -> str:
    try:
        from importlib.metadata import version

        return version("caura-memclawctl")
    except Exception:  # noqa: BLE001
        return "unknown"


# ── Click commands ──────────────────────────────────────────────────────────


@click.group(help="Collect and ship support bundles.")
def support() -> None:
    pass


@support.command("bundle")
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("/tmp"),
    show_default=True,
    help="Where to write the tarball.",
)
@click.option(
    "--no-logs",
    is_flag=True,
    help="Skip log files (metadata + compose state only).",
)
@click.option(
    "--no-compose",
    is_flag=True,
    help="Skip 'docker compose ps/config/top/images' snapshots.",
)
@click.option(
    "--notes",
    default=None,
    help="Free-form context written into manifest.json.",
)
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_HOME,
    show_default=True,
    help="MEMCLAW_HOME (where logs/ and .env live).",
)
def support_bundle(
    out_dir: Path,
    no_logs: bool,
    no_compose: bool,
    notes: str | None,
    home: Path,
) -> None:
    """Build a redacted tarball for Caura support.

    Safe to run on air-gapped hosts: does not talk to the network, does
    not require admin_key. Attach the tarball to your support email.
    """
    if not home.is_dir():
        raise click.ClickException(
            f"MEMCLAW_HOME not found: {home}. Pass --home or set MEMCLAW_HOME."
        )
    # Refuse if out_dir is the home dir itself — the tarball would get
    # captured by its own next run.
    if out_dir.resolve() == home.resolve():
        raise click.ClickException(
            "--out-dir cannot be MEMCLAW_HOME (that directory is included in bundles)."
        )

    console.print(f"Collecting from [bold]{home}[/bold] → [bold]{out_dir}[/bold]")
    path = build_bundle(
        home,
        out_dir,
        include_logs=not no_logs,
        include_compose_state=not no_compose,
        extra_notes=notes,
    )
    size_mb = path.stat().st_size / (1024 * 1024)
    console.print(
        f"[green]Bundle ready[/green]: {path}  ({size_mb:.1f} MiB)\n"
        "Secrets redacted. Review with: [bold]memclawctl support review "
        f"{path}[/bold]"
    )


# Leak-scan patterns used by `support review`. These are looser than the
# redactor (which consumes raw logs and knows the field names) — they
# scan the already-redacted output for **shapes** that smell like a
# secret that slipped through. Hits should be rare; when they are, we
# refuse to call the bundle reviewed so the admin can either re-bundle
# with an updated redactor or strip the offender by hand.
_LEAK_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("mc_admin_key", re.compile(r"mc_admin_[A-Za-z0-9]{16,}")),
    (
        "jwt_token",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    ("bearer_header", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    # Base64-ish 32+ byte blobs that are likely Fernet / JWT secrets.
    # Guarded by context words to avoid flagging random CSS hashes.
    (
        "high_entropy_secret",
        re.compile(
            r"(?:jwt|fernet|secret|password|key)['\"\s=:]{0,4}"
            r"[A-Za-z0-9+/=_\-]{32,}",
            re.IGNORECASE,
        ),
    ),
)


def scan_for_leaks(tarball: Path) -> list[tuple[str, str, str]]:
    """Open the bundle and look for redaction shapes that escaped.

    Returns a list of (member, leak_type, sample) tuples — empty means
    clean. Kept as a library function so the upload path can gate on it.
    """
    hits: list[tuple[str, str, str]] = []
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # manifest.json contains no redacted content — skip it so
            # the sha256 doesn't false-match a high_entropy_secret.
            if member.name == "manifest.json":
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                data = fh.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - don't let one file sink the scan
                continue
            for leak_type, pat in _LEAK_SHAPES:
                m = pat.search(data)
                if m:
                    sample = m.group(0)
                    if len(sample) > 60:
                        sample = sample[:57] + "..."
                    hits.append((member.name, leak_type, sample))
    return hits


@support.command("review")
@click.argument(
    "bundle",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--extract-to",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="If set, also extract the bundle to this directory for browsing.",
)
def support_review(bundle: Path, extract_to: Path | None) -> None:
    """Verify a bundle is safe to ship.

    Lists every file + size + the manifest, then runs a leak-shape scan
    on the already-redacted content. Exits non-zero if any shape hits,
    so `memclawctl support bundle && memclawctl support review …` can
    be chained in CI.
    """
    with tarfile.open(bundle, "r:gz") as tar:
        manifest_member = _find_member(tar, "manifest.json")
        if manifest_member is None:
            raise click.ClickException("No manifest.json — not a memclawctl bundle?")
        manifest_fh = tar.extractfile(manifest_member)
        assert manifest_fh is not None
        manifest = json.loads(manifest_fh.read())

    size_mb = bundle.stat().st_size / (1024 * 1024)
    console.print(f"[bold]{bundle.name}[/bold]  ({size_mb:.1f} MiB)")
    dep = manifest.get("deployment") or {}
    console.print(f"  collected_at: {manifest.get('collected_at')}")
    console.print(
        f"  deployment:   license_id={dep.get('license_id')}  "
        f"org={dep.get('org_name')}  version={dep.get('version')}"
    )
    console.print(
        f"  payload:      {manifest.get('file_total')} files, "
        f"{manifest.get('byte_total'):,} bytes, "
        f"sha256={(manifest.get('sha256_of_redacted_payload') or '')[:16]}…"
    )

    with tarfile.open(bundle, "r:gz") as tar:
        files = sorted((m.name, m.size) for m in tar.getmembers() if m.isfile())
    console.print(f"\n[dim]{len(files)} members[/dim]")
    for name, size in files[:30]:
        console.print(f"  {size:>10,}  {name}")
    if len(files) > 30:
        console.print(f"  [dim]… and {len(files) - 30} more[/dim]")

    hits = scan_for_leaks(bundle)
    if not hits:
        console.print("\n[green]Leak-scan: clean.[/green] Safe to ship.")
    else:
        console.print("\n[red]Leak-scan: FAILED.[/red] Do NOT ship this bundle.")
        for member, leak_type, sample in hits[:20]:
            console.print(f"  {member} → {leak_type}: [red]{sample}[/red]")
        if len(hits) > 20:
            console.print(f"  [dim]… and {len(hits) - 20} more hits[/dim]")
        raise click.exceptions.Exit(1)

    if extract_to is not None:
        extract_to.mkdir(parents=True, exist_ok=True)
        with tarfile.open(bundle, "r:gz") as tar:
            # data_filter landed in Python 3.12; falls back on older
            # versions. The bundle is produced locally by memclawctl so
            # the risk of a path-traversal member is low, but the filter
            # gives us defence-in-depth at zero cost.
            try:
                tar.extractall(extract_to, filter="data")
            except TypeError:
                tar.extractall(extract_to)  # noqa: S202 - old Python fallback
        console.print(f"\nExtracted to [bold]{extract_to}[/bold]")


def _find_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo | None:
    try:
        return tar.getmember(name)
    except KeyError:
        return None


# ── upload ─────────────────────────────────────────────────────────────────

DEFAULT_SUPPORT_ENDPOINT = os.environ.get(
    "MEMCLAW_SUPPORT_URL", "https://support.caura.ai/api/onprem/support"
)


def _hmac_key(license_id: str, issued_at: datetime) -> bytes:
    """Mirror of CauraOps `onprem.heartbeat_auth.derive_hmac_key`."""
    return hashlib.sha256(f"{license_id}:{issued_at.isoformat()}".encode()).digest()


def _sign(body: bytes, license_id: str, issued_at: datetime) -> str:
    import hmac

    return hmac.new(_hmac_key(license_id, issued_at), body, hashlib.sha256).hexdigest()


def _extract_license_jwt_fields(home: Path) -> tuple[str, datetime]:
    """Read license.key → (license_id, issued_at)."""
    lic_path = home / "license" / "license.key"
    if not lic_path.is_file():
        raise click.ClickException(
            f"license file not found at {lic_path}; support upload requires a valid license."
        )
    claims = _unsafe_parse_jwt_payload(lic_path.read_text().strip())
    if not claims:
        raise click.ClickException("license file could not be parsed as JWT")
    license_id = claims.get("license_id")
    issued_raw = claims.get("issued_at") or claims.get("iat")
    if not license_id or issued_raw is None:
        raise click.ClickException(
            "license JWT missing required claims (license_id + issued_at/iat)"
        )
    # issued_at may come as ISO string (our signer) OR unix-epoch int (standard iat)
    if isinstance(issued_raw, int | float):
        issued_at = datetime.fromtimestamp(int(issued_raw), tz=UTC)
    else:
        # fromisoformat handles '2026-04-22T10:00:00+00:00' and the trailing-Z form
        issued_str = str(issued_raw).replace("Z", "+00:00")
        issued_at = datetime.fromisoformat(issued_str)
    # Ensure tz-aware; HMAC key is keyed on iso format so timezone matters.
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    return license_id, issued_at


@support.command("upload")
@click.argument(
    "bundle",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--endpoint",
    default=DEFAULT_SUPPORT_ENDPOINT,
    show_default=True,
    help="Support ingest URL. Override for self-hosted or testing.",
)
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_HOME,
    show_default=True,
    help="MEMCLAW_HOME (needed to find license/license.key for HMAC auth).",
)
@click.option(
    "--skip-leak-scan",
    is_flag=True,
    help="Upload even if the leak scanner flags the bundle. Use with extreme care.",
)
@click.option(
    "--timeout",
    type=int,
    default=120,
    show_default=True,
    help="HTTP timeout in seconds.",
)
def support_upload(
    bundle: Path,
    endpoint: str,
    home: Path,
    skip_leak_scan: bool,
    timeout: int,
) -> None:
    """Ship a support bundle to support.caura.ai.

    HMAC-signs the upload with the installed license — the endpoint
    verifies against the matching license record server-side. No admin
    key or shared secret needed. Air-gapped deployments should email
    the tarball instead of using this command.
    """
    # Gate on leak scan by default — prevents a regressed redactor
    # from shipping customer secrets to Caura infra.
    if not skip_leak_scan:
        hits = scan_for_leaks(bundle)
        if hits:
            console.print(
                "[red]Leak-scan flagged the bundle; refusing to upload.[/red]"
            )
            for member, leak_type, sample in hits[:10]:
                console.print(f"  {member} → {leak_type}: [red]{sample}[/red]")
            console.print(
                "\nEither re-bundle after upgrading memclawctl, or pass "
                "--skip-leak-scan after manual review."
            )
            raise click.exceptions.Exit(1)

    license_id, issued_at = _extract_license_jwt_fields(home)

    with tarfile.open(bundle, "r:gz") as tar:
        manifest_member = _find_member(tar, "manifest.json")
        if manifest_member is None:
            raise click.ClickException("No manifest.json — not a memclawctl bundle?")
        manifest_fh = tar.extractfile(manifest_member)
        assert manifest_fh is not None
        manifest_bytes = manifest_fh.read()

    # Manifest goes as a JSON string (same text the server will sign).
    # Re-serialise from the parsed object so whitespace between client &
    # server-side signing is canonicalised — a pretty-printed manifest
    # in the tarball produced by build_bundle() is fine on disk, but we
    # strip it for the signature.
    manifest_obj = json.loads(manifest_bytes)
    manifest_for_wire = json.dumps(manifest_obj, separators=(",", ":"))

    data = bundle.read_bytes()
    bundle_sha = hashlib.sha256(data).hexdigest()
    signed_body = f"{license_id}.{manifest_for_wire}.{bundle_sha}".encode()
    signature = _sign(signed_body, license_id, issued_at)

    import httpx

    console.print(
        f"Uploading [bold]{bundle.name}[/bold] ({len(data):,} bytes) → {endpoint}"
    )
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(
                endpoint,
                files={
                    "bundle": (bundle.name, data, "application/gzip"),
                },
                data={
                    "license_id": license_id,
                    "manifest": manifest_for_wire,
                },
                headers={"X-License-Signature": signature},
            )
    except httpx.HTTPError as e:
        raise click.ClickException(f"upload failed: {e}") from e

    if r.status_code >= 400:
        raise click.ClickException(f"upload rejected ({r.status_code}): {r.text[:300]}")

    body = r.json()
    console.print(
        f"[green]Uploaded[/green]  bundle_id={body.get('bundle_id')}  "
        f"sha256={body.get('sha256')}"
    )


def register(cli_group: click.Group) -> None:
    """Wire the support subgroup into the main memclawctl CLI."""
    cli_group.add_command(support)
