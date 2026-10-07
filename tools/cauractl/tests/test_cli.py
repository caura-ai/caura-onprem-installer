"""Smoke tests for the cauractl CLI entrypoint."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from rich.console import Console

# Make src/ importable when running `pytest` from the tools/cauractl dir
# OR from the repo root.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cauractl import cli as cli_mod  # noqa: E402
from cauractl.cli import cli  # noqa: E402


def test_console_scripts_share_the_click_group():
    pyproject = (_HERE.parent / "pyproject.toml").read_text()
    scripts = pyproject.partition("[project.scripts]")[2].partition("\n[")[0]
    entrypoint = '"cauractl.cli:cli"'
    assert f"cauractl = {entrypoint}" in scripts
    # The old console-script name still has to resolve to the SAME implementation
    # -- an operator whose runbook still says the old name keeps working after
    # the module underneath it was renamed.
    assert f"memclawctl = {entrypoint}" in scripts  # legacy-name-ok: permanent console-script alias, which rule 3 keeps working


def test_cli_help_lists_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "status",
        "setup",
        "license",
        "backup",
        "restore",
        "upgrade",
        "rollback",
        "plugin",
        "memory",
        "api",
    ):
        assert cmd in result.output


def test_rollback_errors_without_marker(monkeypatch, tmp_path):
    """Fresh install has no .memclaw-prev-version — should refuse cleanly."""
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["rollback", "-y"])
    assert result.exit_code == 1
    assert "No .memclaw-prev-version" in result.output


def _stub_upgrade_sh(home: Path, exit_code: int = 0) -> Path:
    """An upgrade.sh that records its arguments, saved the way ``curl -o`` saves it."""
    script = home / "upgrade.sh"
    script.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{home}/args"\nexit {exit_code}\n'
    )
    script.chmod(0o644)
    return home / "args"


def test_upgrade_runs_a_downloaded_upgrade_sh_and_passes_offline_on(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    recorded = _stub_upgrade_sh(tmp_path)
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"")
    result = CliRunner().invoke(
        cli, ["upgrade", "--to", "v2.14.0", "-y", "--offline", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    assert recorded.read_text().splitlines() == [
        "--to",
        "v2.14.0",
        "--yes",
        "--offline",
        "--bundle",
        str(bundle),
    ]


def test_rollback_passes_offline_on(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    marker = tmp_path / ".memclaw-prev-version"  # legacy-name-floor: the marker file upgrade.sh writes on existing installs
    marker.write_text("v2.13.0\n")
    recorded = _stub_upgrade_sh(tmp_path)
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"")
    result = CliRunner().invoke(
        cli, ["rollback", "-y", "--offline", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    assert recorded.read_text().splitlines() == [
        "--to",
        "v2.13.0",
        "--yes",
        "--offline",
        "--bundle",
        str(bundle),
    ]


def test_upgrade_exits_with_the_code_upgrade_sh_exits_with(monkeypatch, tmp_path):
    # 5 is a failed upgrade whose rollback failed too: manual recovery needed.
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    _stub_upgrade_sh(tmp_path, exit_code=5)
    result = CliRunner().invoke(cli, ["upgrade", "--to", "v2.14.0"])
    assert result.exit_code == 5


def test_a_missing_upgrade_sh_says_how_to_fetch_it(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    # Wide enough that the message is not wrapped, so its end can be checked.
    monkeypatch.setattr(cli_mod, "console", Console(width=1000))
    marker = tmp_path / ".memclaw-prev-version"  # legacy-name-floor: the marker file upgrade.sh writes on existing installs
    marker.write_text("v2.13.0\n")
    fetch = f"sudo curl -fsSL https://onprem.caura.ai/upgrade.sh -o {tmp_path / 'upgrade.sh'}"
    for command in (["upgrade", "--to", "v2.14.0"], ["rollback", "-y"]):
        result = CliRunner().invoke(cli, command)
        assert result.exit_code == 1
        # Last, so a paste of it carries no punctuation: curl would take a
        # trailing "." for a second URL and fail after saving the file.
        assert result.output.rstrip().endswith(fetch), result.output
        # The bundle does not carry upgrade.sh, so refreshing it cannot help.
        assert "bundle.tar.gz" not in result.output
        assert "Rolling back" not in result.output


def test_plugin_install_url_emits_copy_paste(monkeypatch, tmp_path):
    """install-url should print a ready-to-paste curl line and flag missing api-key."""
    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    (tmp_path / ".env").write_text("PUBLIC_HOSTNAME=onprem.example\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["plugin", "install-url", "--fleet-id", "prod"])
    assert result.exit_code == 0
    assert "curl -s -X POST" in result.output
    assert "http://onprem.example/api/v1/install-plugin" in result.output
    assert '"fleet_id":"prod"' in result.output
    assert "<PASTE_API_KEY>" in result.output


def test_memory_export_paginates(monkeypatch, tmp_path):
    """export should follow next_cursor and emit JSONL."""
    import json

    monkeypatch.setattr(
        "httpx.Client",
        lambda *a, **kw: _FakeClient(
            [
                (200, {"items": [{"id": "1", "content": "a"}], "next_cursor": "c1"}),
                (200, {"items": [{"id": "2", "content": "b"}], "next_cursor": None}),
            ]
        ),
    )
    runner = CliRunner()
    out_path = tmp_path / "dump.jsonl"
    result = runner.invoke(
        cli,
        [
            "memory",
            "export",
            "t-x",
            "--api-key",
            "mc_fake",
            "--out",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(ln) for ln in out_path.read_text().splitlines()]
    assert [r["id"] for r in lines] == ["1", "2"]


class _FakeClient:
    """Minimal httpx.Client stand-in that replays a script of responses."""

    def __init__(self, script):
        self._script = list(script)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, *args, **kwargs):
        status, body = self._script.pop(0)
        return httpx.Response(status, json=body)


def test_status_calls_both_endpoints(monkeypatch):
    runner = CliRunner()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/setup/status" in str(request.url):
            return httpx.Response(
                200,
                json={"admin_exists": True, "license_loaded": True, "db_ready": True},
            )
        if "/license/status" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "configured": True,
                    "org_name": "Test Co",
                    "severity": "ok",
                    "expires_at": "2027-01-01T00:00:00Z",
                    "days_remaining": 200,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    # Patch httpx.Client to use our mock transport
    orig_client = cli_mod._client

    def fake_client(url, admin_key):
        return httpx.Client(base_url=url, transport=transport, timeout=5)

    monkeypatch.setattr(cli_mod, "_client", fake_client)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "Test Co" in result.output
    assert "ok" in result.output


def test_setup_reports_api_key(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    license_file = tmp_path / "license.key"
    license_file.write_text("eyJhbGc.stub.sig")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/setup/license" in str(request.url):
            return httpx.Response(200, json={"ok": True})
        if "/setup/admin" in str(request.url):
            body = json.loads(request.content)
            assert body["license_key"] == "eyJhbGc.stub.sig"
            assert body["password"] == "Correct-horse-battery-staple1"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user_id": "u1",
                    "org_id": "o1",
                    "org_slug": "acme",
                    "tenant_id": "t1",
                    "api_key": "mc_smoketestkey",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda url, admin_key: httpx.Client(base_url=url, transport=transport),
    )

    result = runner.invoke(
        cli,
        [
            "setup",
            "--license", str(license_file),
            "--email", "a@acme.example",
            "--password", "Correct-horse-battery-staple1",
            "--org-name", "Acme",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mc_smoketestkey" in result.output
    assert "eyJhbGc.stub.sig" not in result.output


def test_the_readme_installs_the_name_pyproject_declares():
    """The documented `pip install` names the distribution that is built.

    Added because a rename got this wrong: a bulk replace of the old command
    name rewrote it INSIDE the longer distribution name too, leaving the README
    telling operators to install a package that would never exist while
    pyproject declared a different one. Nothing else in the suite compares the
    two, so it was invisible until a reviewer read both files.

    Reads both sides rather than hardcoding either, so it keeps holding through
    the next rename.
    """
    import tomllib

    declared = tomllib.loads((_HERE.parent / "pyproject.toml").read_text())["project"]["name"]
    readme = (_HERE.parent / "README.md").read_text()

    installs = re.findall(r"^pip install (\S+)", readme, re.MULTILINE)
    assert installs, "README no longer shows a `pip install` line; this test is looking at nothing"
    for named in installs:
        assert named == declared, (
            f"README says `pip install {named}` but pyproject declares "
            f"name = {declared!r}. Following the README would install a "
            f"different package than the one this directory builds."
        )
