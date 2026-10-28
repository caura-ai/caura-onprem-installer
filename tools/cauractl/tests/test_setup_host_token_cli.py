"""The setup CLI supplies operator proof without displaying it."""

import importlib
import json

import httpx
from click.testing import CliRunner

cli_mod = importlib.import_module("cauractl.cli")
TOKEN = "b" * 64


def _setup(
    monkeypatch,
    tmp_path,
    *,
    token_exists,
    license_loaded=False,
    token=TOKEN,
    explicit_file=False,
):
    license_file = tmp_path / "license.key"
    license_file.write_text("test-signed-license")
    token_file = tmp_path / ("operator-proof" if explicit_file else "setup-token")
    if token_exists:
        token_file.write_text(token + "\n")
    if explicit_file:
        monkeypatch.delenv("SETUP_TOKEN_FILE", raising=False)
        monkeypatch.setattr(cli_mod, "DEFAULT_HOME", tmp_path / "no-install-root")
    else:
        monkeypatch.setenv("SETUP_TOKEN_FILE", str(token_file))
    requests = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"admin_exists": False, "license_loaded": license_loaded}
            )
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "api_key": "mc_test-key"})

    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda url, admin_key: httpx.Client(
            base_url=url, transport=httpx.MockTransport(handle)
        ),
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "setup",
            "--license",
            str(license_file),
            "--email",
            "operator@example.com",
            "--password",
            "Correct-password1",
            "--org-name",
            "Acme",
        ]
        + (["--setup-token-file", str(token_file)] if explicit_file else []),
    )
    return result, requests


def test_setup_sends_proof_on_both_requests(monkeypatch, tmp_path):
    result, requests = _setup(monkeypatch, tmp_path, token_exists=True)
    assert result.exit_code == 0, result.output
    assert len(requests) == 2
    assert all(
        json.loads(request.content).get("setup_token") == TOKEN for request in requests
    )
    assert TOKEN not in result.output
    assert all(TOKEN not in str(request.url) for request in requests)


def test_setup_for_environment_token_server_uses_explicit_private_file(
    monkeypatch, tmp_path
):
    result, requests = _setup(
        monkeypatch, tmp_path, token_exists=True, explicit_file=True
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "no-install-root/setup-token").exists()
    assert len(requests) == 2
    assert all(
        json.loads(request.content)["setup_token"] == TOKEN for request in requests
    )
    assert TOKEN not in result.output


def test_setup_without_host_proof_does_not_contact_server(monkeypatch, tmp_path):
    result, requests = _setup(monkeypatch, tmp_path, token_exists=False)
    assert result.exit_code != 0
    assert requests == []
    assert TOKEN not in result.output


def test_setup_with_a_mounted_license_only_creates_admin(monkeypatch, tmp_path):
    result, requests = _setup(
        monkeypatch, tmp_path, token_exists=True, license_loaded=True
    )
    assert result.exit_code == 0, result.output
    assert [request.url.path for request in requests] == ["/api/setup/admin"]
    assert json.loads(requests[0].content)["setup_token"] == TOKEN
