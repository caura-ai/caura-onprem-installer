"""Smoke tests for the memclawctl CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

# Make src/ importable when running `pytest` from the tools/memclawctl dir
# OR from the repo root.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from memclawctl.cli import cli  # noqa: E402


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
    from memclawctl import cli as cli_mod

    monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["rollback", "-y"])
    assert result.exit_code == 1
    assert "No .memclaw-prev-version" in result.output


def test_plugin_install_url_emits_copy_paste(monkeypatch, tmp_path):
    """install-url should print a ready-to-paste curl line and flag missing api-key."""
    from memclawctl import cli as cli_mod

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
    from memclawctl import cli as cli_mod

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
    from memclawctl import cli as cli_mod

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
            "--password", "correct-horse-battery-staple",
            "--org-name", "Acme",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mc_smoketestkey" in result.output
