"""memclawctl CLI entrypoint."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table

from memclawctl.support import register as _register_support

console = Console()

# Each of these reads both spellings, new name first, and takes the first
# NON-EMPTY one — which is what ``or`` gives, since "" is falsy. Never nest the
# old lookup as the *default argument* of the new one: that resolves to the
# first name which is merely defined, and an exported-but-blank new name would
# beat a working old-name value. These names reach operators through .env and
# install.conf templates, so "present and blank" is the ordinary half-migrated
# state rather than an exotic one.
#
# DEFAULT_ADMIN_KEY is where that distinction has teeth. ``_client`` below
# attaches an Authorization header only ``if admin_key`` — an empty key is not
# an error, it silently sends the request unauthenticated. So blank here means
# "drop the credential", not "refuse", which puts it firmly on the
# first-non-empty side.
DEFAULT_URL = os.environ.get("CAURA_URL") or os.environ.get(
    "MEMCLAW_URL", "http://localhost"  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)
DEFAULT_ADMIN_KEY = os.environ.get("CAURA_ADMIN_KEY") or os.environ.get(
    "MEMCLAW_ADMIN_KEY", ""  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)
# The install root's old-name lookup is pinned verbatim by
# scripts/do_not_touch_sentinel.py — nothing on disk records where a customer
# installed, so this default IS the record. It is left character-for-character
# intact and the new name is layered in front of it.
DEFAULT_HOME = Path(
    os.environ.get("CAURA_HOME")
    or os.environ.get("MEMCLAW_HOME", "/opt/memclaw")  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)


def _client(url: str, admin_key: str | None) -> httpx.Client:
    headers = {"Accept": "application/json"}
    if admin_key:
        headers["Authorization"] = f"Bearer {admin_key}"
    return httpx.Client(base_url=url, headers=headers, timeout=30, verify=False)


@click.group(help="Day-2 operations for on-prem Caura.")
@click.option(
    "--url", default=DEFAULT_URL, show_default=True, help="Base URL of the stack."
)
@click.option(
    "--admin-key",
    default=DEFAULT_ADMIN_KEY,
    help="Admin JWT; can also come from CAURA_ADMIN_KEY (or MEMCLAW_ADMIN_KEY).",  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)
@click.pass_context
def cli(ctx: click.Context, url: str, admin_key: str) -> None:
    ctx.ensure_object(dict)
    ctx.obj["url"] = url
    ctx.obj["admin_key"] = admin_key


# ── status ──────────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print /setup/status + /license/status side by side."""
    with _client(ctx.obj["url"], ctx.obj["admin_key"]) as c:
        setup = _get_json(c, "/api/setup/status")
        lic = _get_json(c, "/api/license/status")

    t = Table(show_header=False, title="Caura")
    t.add_row("setup.admin_exists", _fmt(setup.get("admin_exists")))
    t.add_row("setup.license_loaded", _fmt(setup.get("license_loaded")))
    t.add_row("license.configured", _fmt(lic.get("configured")))
    if lic.get("configured"):
        t.add_row("license.org_name", _fmt(lic.get("org_name")))
        t.add_row("license.severity", _fmt(lic.get("severity")))
        t.add_row("license.expires_at", _fmt(lic.get("expires_at")))
        t.add_row("license.days_remaining", _fmt(lic.get("days_remaining")))
    console.print(t)


# ── setup wizard (CLI alternative to /setup) ───────────────────────────────


@cli.command()
@click.option(
    "--license",
    "license_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the .key file.",
)
@click.option("--email", required=True)
@click.option(
    "--password-stdin",
    is_flag=True,
    help="Read password from stdin (safer than --password).",
)
@click.option("--password")
@click.option("--org-name", required=True)
@click.pass_context
def setup(
    ctx: click.Context,
    license_path: str,
    email: str,
    password_stdin: bool,
    password: str | None,
    org_name: str,
) -> None:
    """First-run setup: upload license + create the first admin."""
    pw = _resolve_password(password, password_stdin)

    with _client(ctx.obj["url"], None) as c:  # /setup/* is unauthenticated
        key = Path(license_path).read_text().strip()
        _post(c, "/api/setup/license", {"license_key": key})

        resp = _post(
            c,
            "/api/setup/admin",
            {"email": email, "password": pw, "org_name": org_name},
        )
    console.print(
        f"[green]Setup complete.[/green] API key:\n  [bold]{resp['api_key']}[/bold]"
    )
    console.print("[yellow]Copy this now — it is shown only once.[/yellow]")


# ── license management ─────────────────────────────────────────────────────


@cli.group()
def license() -> None:  # noqa: A001 - CLI verb
    """License operations."""


@license.command("status")
@click.pass_context
def license_status(ctx: click.Context) -> None:
    with _client(ctx.obj["url"], ctx.obj["admin_key"]) as c:
        data = _get_json(c, "/api/license/status")
    console.print_json(data=data)


@license.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def license_load(path: str) -> None:
    """Hot-reload a license by copying it to the mounted volume.

    Works from the host — copies the file into $MEMCLAW_HOME/license/
    and the platform-admin-api / platform-auth-api background-refresh
    loops pick it up within the hour.
    """
    dst = DEFAULT_HOME / "license" / "license.key"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dst)
    console.print(f"Copied to {dst}. Services will re-verify within an hour.")


# ── backup / restore / upgrade ─────────────────────────────────────────────


@cli.command()
@click.option("--out", default=None, help="Output directory.")
def backup(out: str | None) -> None:
    """Run scripts/backup.sh in $MEMCLAW_HOME."""
    cmd = [str(DEFAULT_HOME / "scripts" / "backup.sh")]
    if out:
        cmd.append(out)
    subprocess.check_call(cmd)


@cli.command()
@click.option(
    "--from",
    "src",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Backup tarball to restore from.",
)
@click.option(
    "--replace-config", is_flag=True, help="Also overwrite .env and license.key."
)
def restore(src: str, replace_config: bool) -> None:
    """Run scripts/restore.sh."""
    cmd = [str(DEFAULT_HOME / "scripts" / "restore.sh")]
    if replace_config:
        cmd.append("--replace-config")
    cmd.append(src)
    subprocess.check_call(cmd)


@cli.command()
@click.option("--to", "version", required=True, help="Target version tag, e.g. v1.2.0.")
@click.option("--dry-run", is_flag=True, help="Print the plan without applying.")
@click.option("--no-backup", is_flag=True, help="Skip pre-upgrade DB snapshot.")
@click.option("-y", "--yes", is_flag=True, help="Assume yes on prompts.")
def upgrade(version: str, dry_run: bool, no_backup: bool, yes: bool) -> None:
    """Snapshot DB, pull target images, roll services, auto-rollback on health failure.

    Delegates to ``$MEMCLAW_HOME/upgrade.sh`` — the same script customers
    run via ``curl | bash``. Running it through memclawctl lets ops call
    it from an operator's shell with the same flags, without memorising
    the URL.
    """
    script = DEFAULT_HOME / "upgrade.sh"
    if not script.is_file():
        _fail(
            f"upgrade.sh missing at {script}. Refresh the bundle: "
            f"curl -fsSL https://onprem.caura.ai/bundle.tar.gz | "
            f"sudo tar -xz -C {DEFAULT_HOME}"
        )
    cmd = [str(script), "--to", version]
    if dry_run:
        cmd.append("--dry-run")
    if no_backup:
        cmd.append("--no-backup")
    if yes:
        cmd.append("--yes")
    subprocess.check_call(cmd)


# ── rollback ────────────────────────────────────────────────────────────────


@cli.command()
@click.option("-y", "--yes", is_flag=True, help="Assume yes on prompts.")
def rollback(yes: bool) -> None:
    """Roll back to the version recorded in .memclaw-prev-version.

    Written by upgrade.sh right before any mutation, so we always know
    the last-good tag. Errors out if no marker exists (fresh install with
    no upgrades yet).
    """
    marker = DEFAULT_HOME / ".memclaw-prev-version"
    if not marker.is_file():
        _fail(
            f"No {marker.name} found — nothing to roll back to. "
            "Either no upgrade has run yet, or the marker was deleted."
        )
    prev = marker.read_text().strip()
    if not prev:
        _fail(f"{marker.name} is empty.")
    script = DEFAULT_HOME / "upgrade.sh"
    if not script.is_file():
        _fail(f"upgrade.sh missing at {script}; bundle refresh required.")
    console.print(f"[yellow]Rolling back to {prev}…[/yellow]")
    cmd = [str(script), "--to", prev]
    if yes:
        cmd.append("--yes")
    subprocess.check_call(cmd)


# ── plugin install helper ──────────────────────────────────────────────────


@cli.group(name="plugin")
def plugin_group() -> None:
    """Helpers for provisioning OpenClaw nodes against this stack."""


@plugin_group.command("install-url")
@click.option("--fleet-id", required=True, help="Fleet to join, e.g. 'prod'.")
@click.option(
    "--api-url",
    default=None,
    help="Public URL of this stack. Defaults to PUBLIC_HOSTNAME from .env.",
)
@click.option(
    "--api-key",
    default=None,
    help="API key the node will use. Generate one from the dashboard first.",
)
def plugin_install_url(fleet_id: str, api_url: str | None, api_key: str | None) -> None:
    """Print the copy-paste curl command a customer runs on an OpenClaw VM.

    No secrets are invented — if --api-key is omitted, the output includes
    a ``<PASTE_API_KEY>`` placeholder the operator must fill in. This keeps
    key material out of shell history and CI logs.
    """
    if not api_url:
        env_file = DEFAULT_HOME / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                if line.startswith("PUBLIC_HOSTNAME="):
                    host = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if host:
                        api_url = f"http://{host}" if "://" not in host else host
                        break
        if not api_url:
            api_url = "http://<YOUR_HOST>"

    key_value = api_key or "<PASTE_API_KEY>"
    body = json.dumps(
        {"fleet_id": fleet_id, "api_url": api_url, "api_key": key_value},
        separators=(",", ":"),
    )
    cmd = (
        f"curl -s -X POST \"{api_url}/api/v1/install-plugin\" "
        f"-H \"Content-Type: application/json\" "
        f"-d '{body}' | bash"
    )
    console.print(cmd)
    if not api_key:
        console.print(
            "\n[yellow]Replace <PASTE_API_KEY> with an mc_... key issued from "
            "the dashboard (API Keys → Create new key).[/yellow]"
        )


# ── memory export / import (bulk ops) ──────────────────────────────────────


@cli.group(name="memory")
def memory_group() -> None:
    """Bulk memory operations. For per-memory writes use the plugin or REST API."""


@memory_group.command("export")
@click.argument("tenant_id")
@click.option(
    "--api-key",
    # A list, not a string: click resolves it to the first envvar with a
    # non-empty value (``if rv:``), so a blank CAURA_API_KEY falls through to a
    # working old-name value rather than resolving to "" and tripping
    # ``required``. Pinned by test_env_dual_read.py, because that is a library
    # behaviour rather than one this file implements.
    envvar=["CAURA_API_KEY", "MEMCLAW_API_KEY"],  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
    required=True,
    help="Per-tenant API key (mc_...). Env: CAURA_API_KEY (or MEMCLAW_API_KEY).",  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)
@click.option(
    "--out", default=None, help="Output file. Default: stdout (JSONL, one row per line)."
)
@click.option("--page-size", default=200, show_default=True)
@click.pass_context
def memory_export(
    ctx: click.Context,
    tenant_id: str,
    api_key: str,
    out: str | None,
    page_size: int,
) -> None:
    """Stream every memory for a tenant as JSONL.

    Uses the same per-tenant key a plugin would use — keeps this safe to
    run by customers without leaking cross-tenant data. Paginates via
    the ``cursor`` the /memories endpoint returns.
    """
    url = ctx.obj["url"]
    fh = open(out, "w") if out else sys.stdout
    count = 0
    cursor: str | None = None
    with httpx.Client(base_url=url, timeout=60, verify=False) as c:
        while True:
            params: dict[str, Any] = {"tenant_id": tenant_id, "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            r = c.get("/api/memories", params=params, headers={"X-API-Key": api_key})
            if r.status_code >= 400:
                _fail(f"GET /api/memories → {r.status_code}: {r.text[:200]}")
            data = r.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            for row in items:
                fh.write(json.dumps(row) + "\n")
                count += 1
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or not items:
                break
    if out:
        fh.close()
    console.print(f"[green]Exported {count} memories to {out or 'stdout'}.[/green]")


@memory_group.command("import")
@click.argument("tenant_id")
@click.option(
    "--api-key",
    # A list, not a string: click resolves it to the first envvar with a
    # non-empty value (``if rv:``), so a blank CAURA_API_KEY falls through to a
    # working old-name value rather than resolving to "" and tripping
    # ``required``. Pinned by test_env_dual_read.py, because that is a library
    # behaviour rather than one this file implements.
    envvar=["CAURA_API_KEY", "MEMCLAW_API_KEY"],  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
    required=True,
    help="Per-tenant API key (mc_...). Env: CAURA_API_KEY (or MEMCLAW_API_KEY).",  # legacy-name-ok: dual-read of the old spelling, which rule 3 keeps working
)
@click.option(
    "--file",
    "src",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSONL file — one memory per line.",
)
@click.option("--agent-id", default="memclawctl-import", show_default=True)
@click.option(
    "--dry-run", is_flag=True, help="Validate the file without writing anything."
)
@click.pass_context
def memory_import(
    ctx: click.Context,
    tenant_id: str,
    api_key: str,
    src: str,
    agent_id: str,
    dry_run: bool,
) -> None:
    """Import memories from a JSONL file produced by ``memory export``.

    Only content + tags + memory_type + (optional) agent_id are reused
    from each row. IDs, timestamps, embeddings, and per-tenant metadata
    are re-generated server-side so the same file can be replayed into
    a different tenant.
    """
    url = ctx.obj["url"]
    total = ok = failed = 0
    with open(src) as fh, httpx.Client(base_url=url, timeout=30, verify=False) as c:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                failed += 1
                console.print(f"[red]line {total}: bad JSON: {exc}[/red]")
                continue
            body = {
                "tenant_id": tenant_id,
                "agent_id": row.get("agent_id") or agent_id,
                "content": row.get("content"),
            }
            if not body["content"]:
                failed += 1
                console.print(f"[red]line {total}: missing content[/red]")
                continue
            for opt in ("tags", "memory_type", "weight", "source_uri"):
                if row.get(opt) is not None:
                    body[opt] = row[opt]
            if dry_run:
                ok += 1
                continue
            r = c.post("/api/memories", json=body, headers={"X-API-Key": api_key})
            if r.status_code >= 400:
                failed += 1
                console.print(
                    f"[red]line {total}: {r.status_code} {r.text[:120]}[/red]"
                )
            else:
                ok += 1
    verb = "validated" if dry_run else "imported"
    console.print(
        f"[green]{verb} {ok}/{total}[/green]" + (f" [red]({failed} failed)[/red]" if failed else "")
    )


# ── generic API passthrough ────────────────────────────────────────────────


@cli.command()
@click.argument("method")
@click.argument("path")
@click.option(
    "--body",
    "body_arg",
    default=None,
    help="Request body. Pass a file path, or '-' for stdin. JSON is sent as-is.",
)
@click.option(
    "--api-key", default=None, help="X-API-Key header (for memory/search endpoints)."
)
@click.pass_context
def api(
    ctx: click.Context,
    method: str,
    path: str,
    body_arg: str | None,
    api_key: str | None,
) -> None:
    """Send an authenticated request to the running stack.

    Admin JWT is attached by default; pass --api-key to override with a
    per-tenant mc_... key for memory/search endpoints. Response body is
    written to stdout verbatim; exits non-zero on HTTP ≥ 400.
    """
    if not path.startswith("/"):
        path = "/" + path
    body: str | None = None
    if body_arg == "-":
        body = sys.stdin.read()
    elif body_arg:
        body = Path(body_arg).read_text()

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    elif ctx.obj.get("admin_key"):
        headers["Authorization"] = f"Bearer {ctx.obj['admin_key']}"

    with httpx.Client(base_url=ctx.obj["url"], timeout=30, verify=False) as c:
        r = c.request(method.upper(), path, content=body, headers=headers)
    sys.stdout.write(r.text)
    if not r.text.endswith("\n"):
        sys.stdout.write("\n")
    if r.status_code >= 400:
        sys.exit(1)


# ── helpers ─────────────────────────────────────────────────────────────────


def _get_json(c: httpx.Client, path: str) -> dict[str, Any]:
    r = c.get(path)
    if r.status_code >= 400:
        _fail(f"{path} → {r.status_code}: {r.text[:200]}")
    return r.json()


def _post(c: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = c.post(path, json=body)
    if r.status_code >= 400:
        _fail(f"{path} → {r.status_code}: {r.text[:200]}")
    return r.json()


def _fail(msg: str) -> None:
    console.print(f"[red]{msg}[/red]")
    sys.exit(1)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✓" if v else "✗"
    return str(v)


def _resolve_password(password: str | None, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    if not password:
        _fail("Pass --password or --password-stdin")
    return password  # type: ignore[return-value]


_register_support(cli)


if __name__ == "__main__":
    cli()
