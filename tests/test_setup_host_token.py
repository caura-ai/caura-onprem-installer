"""Host setup proof is delivered only to auth-api and the setup payload."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_setup_payload import _serializer

TOKEN = "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def test_only_auth_api_receives_host_setup_proof(compose_services):
    services = compose_services({"SETUP_TOKEN": TOKEN})
    given = {
        name
        for name, service in services.items()
        if "/run/caura/setup-token" in json.dumps(service)
    }
    assert given == {"platform-auth-api"}


def test_silent_setup_payload_carries_host_proof():
    result = subprocess.run(
        [
            "bash",
            "-c",
            _serializer() + '\nsetup_admin_payload "$@"',
            "setup",
            "operator@example.com",
            "Correct-password1",
            "Acme",
            "test-license",
            TOKEN,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout).get("setup_token") == TOKEN


def _provision(path, hard_links=True):
    program = """set -eu
die() { printf "%s" "$1" >&2; exit "$2"; }
random_hex() { printf '%s\\n' "$TEST_TOKEN"; }
if [ "$HARD_LINKS" = no ]; then ln() { return 1; }; fi
docker() {
  if [ "$1" = image ]; then printf file-v1; return; fi
  python - "$ROOT/scripts/setup_token.py" "$TOKEN_PATH" <<'PYMOCK'
import os, pwd, runpy, sys
from pathlib import Path
from types import SimpleNamespace
ns = runpy.run_path(sys.argv[1])
path = Path(sys.argv[2])
ns['main'].__globals__['Path'] = lambda _: path
pwd.getpwnam = lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
sys.argv = ['helper', '--print']
raise SystemExit(ns['main']())
PYMOCK
}
source "$ROOT/scripts/setup-token.sh"
stage_setup_token_file "$TOKEN_PATH"
prepare_setup_token_for_auth "$TOKEN_PATH" test-auth-image --print
"""
    return subprocess.run(
        ["bash", "-c", program],
        env={
            "PATH": os.environ["PATH"],
            "TEST_TOKEN": TOKEN,
            "TOKEN_PATH": str(path),
            "ROOT": str(ROOT),
            "HARD_LINKS": "yes" if hard_links else "no",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_provisioning_keeps_the_existing_private_host_token(tmp_path):
    path = tmp_path / "setup-token"
    for _ in range(2):
        result = _provision(path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == TOKEN
        assert path.read_text().strip() == TOKEN
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_provisioning_refuses_a_symlink_without_touching_its_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep-me")
    path = tmp_path / "setup-token"
    path.symlink_to(target)
    result = _provision(path)
    assert result.returncode == 2
    assert target.read_text() == "keep-me"
    assert TOKEN not in result.stderr


@pytest.mark.parametrize(
    "invalid", ["weak", "a" * 257, "a" * 31 + " b", "a" * 31 + "\nb"]
)
def test_provisioning_refuses_a_malformed_existing_token(tmp_path, invalid):
    path = tmp_path / "setup-token"
    path.write_text(invalid)
    result = _provision(path)
    assert result.returncode == 2
    assert path.read_text() == invalid
    assert invalid not in result.stderr


@pytest.mark.parametrize(
    "custom",
    ["g" * 32, "Custom_" + "T" * 33, "B" * 64, "g" * 256, "Special_$'&/" + "x" * 30],
)
def test_provisioning_retains_a_valid_custom_host_token(tmp_path, custom):
    path = tmp_path / "setup-token"
    path.write_text(custom)
    result = _provision(path)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == custom
    assert result.stdout == custom
    assert path.stat().st_mode & 0o777 == 0o600


def test_token_provisioning_works_without_hard_links(tmp_path):
    path = tmp_path / "setup-token"
    result = _provision(path, hard_links=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == TOKEN
    assert path.read_text().strip() == TOKEN
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("available", [False, True])
def test_incomplete_bundle_is_refused_before_copying(tmp_path, available):
    bundle = tmp_path / "bundle"
    (bundle / "scripts").mkdir(parents=True)
    if available:
        (bundle / "scripts/setup-token.sh").write_text("# compatible helper\n")
        (bundle / "scripts/setup_token.py").write_text("# compatible helper\n")
    source = (ROOT / "install.sh").read_text()
    start = source.index("# Validate the matching helper before copying")
    end = source.index("for f in docker-compose.yml", start)
    assert end < source.index("# ── Materialize TLS certs")
    program = 'die() { printf "%s" "$1" >&2; exit "$2"; }\n' + source[start:end]
    result = subprocess.run(
        ["bash", "-c", program],
        env={
            "PATH": os.environ["PATH"],
            "SRC_DIR": str(bundle),
            "CAURA_HOME": str(tmp_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if available else 3)
    if not available:
        assert "incomplete or mismatched" in result.stderr
