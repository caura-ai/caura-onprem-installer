"""The shipped offline stack and backups no longer require a retired broker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _preflight(script: str) -> str:
    source = (REPO_ROOT / script).read_text()
    start = source.index("_check_admin_audit_transport() {\n")
    stop = source.index("\n}\n", start) + 3
    return source[start:stop]


def test_both_scripts_share_the_same_image_capability_preflight():
    assert _preflight("install.sh") == _preflight("upgrade.sh")


@pytest.mark.parametrize("defaulted", [False, True])
def test_historical_default_is_refused_with_version_guidance_before_pulling(defaulted):
    source = (REPO_ROOT / "install.sh").read_text()
    start = source.index("_require_explicit_audit_version() {\n")
    stop = source.index("\n}\n", start) + 3
    proc = subprocess.run(
        ["bash", "-c", '\n'.join([
            'die() { echo "$1" >&2; exit "${2:-1}"; }', source[start:stop],
            f'_require_explicit_audit_version {str(defaulted).lower()}',
        ])],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == (2 if defaulted else 0)
    if defaulted:
        assert "Pass --version" in proc.stderr
        assert "No image has been pulled" in proc.stderr


def test_preflight_image_names_match_the_rendered_compose_contract(
    compose_services, compose_required_env
):
    version = "v99.0.0"
    connected = compose_services(env={"CAURA_VERSION": version})
    proc = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.airgap.yml",
         "config", "--format", "json"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        env={**{k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_CONFIG") if k in os.environ},
             **compose_required_env, "CAURA_VERSION": version},
    )
    assert proc.returncode == 0, proc.stderr
    offline = json.loads(proc.stdout)["services"]
    assert connected["platform-admin-api"]["image"] == (
        f"ghcr.io/caura-ai/caura-onprem-platform-admin:{version}"
    )
    assert offline["platform-admin-api"]["image"] == (
        f"memclaw-onprem/platform-admin:{version}"  # legacy-name-floor: actual offline alias checked by preflight
    )


def test_admin_and_audit_can_start_without_a_rabbit_service(compose_services):
    services = compose_services()
    assert "rabbitmq" not in services
    for service in services.values():
        assert "rabbitmq" not in service.get("depends_on", {})
        assert "RABBITMQ_URL" not in service.get("environment", {})
    assert services["platform-admin-api"]["environment"]["PLATFORM_STORAGE_API_URL"] == (
        "http://platform-storage-api:8003"
    )
    assert "platform-audit-api" in services  # still serves broker HTTP uploads


@pytest.fixture
def backup_host(tmp_path: Path):
    host = tmp_path / "host"
    (host / "scripts").mkdir(parents=True)
    for name in ("backup.sh", "restore.sh"):
        shutil.copyfile(REPO_ROOT / "scripts" / name, host / "scripts" / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'echo "$*" >> "$AUDIT_TEST_CALLS"\n'
        'case "$*" in\n'
        '  *rabbitmq*) exit 99 ;;\n'
        '  *pg_dump*) printf "synthetic dump" ;;\n'
        '  *pg_restore*) cat > "$AUDIT_TEST_RESTORED" ;;\n'
        '  *LASTSAVE*) date +%s%N ;;\n'
        '  "compose cp redis:"*) printf "synthetic redis" > "${@: -1}" ;;\n'
        'esac\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    restored = tmp_path / "restored"
    env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CAURA_HOME": str(host),
        "AUDIT_TEST_CALLS": str(calls),
        "AUDIT_TEST_RESTORED": str(restored),
    }
    return host, env, calls, restored


def test_backup_needs_only_postgres_and_redis(backup_host):
    host, env, calls, _ = backup_host
    proc = subprocess.run(
        ["bash", str(host / "scripts/backup.sh")],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    archive = next((host / "backups").glob("*.tar.gz"))
    with tarfile.open(archive) as bundle:
        names = {Path(name).name for name in bundle.getnames()}
    assert {"postgres.dump", "redis.rdb"} <= names
    assert "rabbitmq-defs.json" not in names
    assert "rabbitmq" not in calls.read_text()


@pytest.mark.parametrize("legacy_definitions", [False, True], ids=["current", "legacy"])
def test_restore_keeps_old_archives_usable_without_the_broker(
    backup_host, tmp_path: Path, legacy_definitions: bool
):
    host, env, calls, restored = backup_host
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "postgres.dump").write_text("synthetic dump")
    (payload / "redis.rdb").write_text("synthetic redis")
    if legacy_definitions:
        (payload / "rabbitmq-defs.json").write_text("{}")
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname=".")
    original = archive.read_bytes()
    proc = subprocess.run(
        ["bash", str(host / "scripts/restore.sh"), str(archive)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert restored.read_text() == "synthetic dump"
    assert "compose start redis" in calls.read_text()
    assert "rabbitmq" not in calls.read_text()
    assert archive.read_bytes() == original
    assert ("Ignoring legacy RabbitMQ" in proc.stdout) is legacy_definitions


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
@pytest.mark.parametrize("offline", [False, True], ids=["connected", "offline"])
@pytest.mark.parametrize("capability", ["storage-http-inprocess-v1", "", "unknown"])
def test_real_preflight_rejects_old_images_in_both_modes(
    tmp_path: Path, script: str, offline: bool, capability: str
):
    calls = tmp_path / "calls"
    body = '\n'.join([
        "set -euo pipefail",
        'die() { echo "$1" >&2; exit "${2:-1}"; }',
        'docker() { echo "$*" >> "$CALLS"; if [ "${3:-}" = --format ]; then printf "%s\\n" "$CAPABILITY"; fi; }',
        _preflight(script),
        f'_check_admin_audit_transport v99.0.0 {str(offline).lower()}',
    ])
    proc = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"], "CALLS": str(calls), "CAPABILITY": capability},
    )
    assert proc.returncode == (0 if capability == "storage-http-inprocess-v1" else 1)
    if proc.returncode:
        assert "lacks direct audit persistence" in proc.stderr
    assert ("pull " in calls.read_text()) is (not offline)
    assert "compose" not in calls.read_text()
