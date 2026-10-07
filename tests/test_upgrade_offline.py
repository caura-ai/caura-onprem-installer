"""upgrade.sh --offline: upgrading a host that has no internet access.

upgrade.sh could not run offline. It downloaded the bundle, pulled from the
registry, and never chose the air-gap overlay that install.sh --offline had.
So the documented air-gap upgrade was a hand-run subset of it, and that subset
kept the old compose files and the old .env. A host installed from a bundle
older than 2026-09-03 has CORE_STORAGE_SHARED_SECRET in neither, and the
core-api in v2.13.0 (OSS backend-v3.24.1) refuses to start without it.

These run the real upgrade.sh end to end. ``docker`` is a stand-in that passes
``compose config`` through to the real docker compose, so the image check reads
what compose renders, and that records every other call. ``curl`` is a stand-in
too, so a download offline is caught rather than attempted.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]

FROM, TO = "v2.13.0", "v2.14.0"
# The namespace docker-compose.airgap.yml names the loaded images under.
OLD_NS = "memclaw-onprem"  # legacy-name-ok: the namespace the air-gap overlay resolves, which these tests check against
SERVICES = [
    "platform-storage",
    "platform-auth",
    "platform-admin",
    "platform-audit",
    "core-storage",
    "core-api",
    "app",
    "core-operations",
]
UPSTREAM = ["pgvector/pgvector:pg16", "redis:7-alpine", "rabbitmq:3-management-alpine"]
GATEWAY_BASE = "nginx:1.27-alpine"
# Marks the install's own compose file, so a refresh or a restore shows.
AUGUST = "# from the August bundle"
# A Dockerfile from before the gateway build worked offline.
OLD_DOCKERFILE = "FROM nginx:1.27-alpine\nRUN apk add --no-cache gettext\n"

_FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE/calls"
case "$1" in
  info) exit 0 ;;
  image) [ "$2" = inspect ] && grep -qxF "$3" "$FAKE/loaded"; exit ;;
  compose) ;;
  *) exit 0 ;;
esac
args=("$@")
shift
while [ "${1:-}" = -f ]; do shift 2; done
case "${1:-}" in
  config) exec "$REAL_DOCKER" "${args[@]}" ;;
  exec) printf 'dump' ;;
  pull) [ "${FAKE_PULL:-}" = ok ] || exit 1 ;;
  ps) printf '{"State":"running","Health":"%s"}\n' "${FAKE_HEALTH:-healthy}" ;;
esac
exit 0
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE/curl"
case "$*" in *bundle.tar.gz*) [ -n "${FAKE_BUNDLE:-}" ] && exec cat "$FAKE_BUNDLE" ;; esac
exit 6
"""

_FAKES = {
    "docker": _FAKE_DOCKER,
    "curl": _FAKE_CURL,
    # GNU df's -BG output, which upgrade.sh's disk check parses.
    "df": "#!/bin/sh\nprintf 'Filesystem 1G-blocks Used Available Use%% Mounted\\n"
    "/dev/x 200G 10G 100G 5%% /\\n'\n",
    "sleep": "#!/bin/sh\nexit 0\n",
}


def _release(version: str, *, embedder: bool = False) -> list[str]:
    """The images a release tarball loads for ``version``, under the overlay's names."""
    names = [
        "core-api-embedder" if embedder and n == "core-api" else n for n in SERVICES
    ]
    return [f"{OLD_NS}/{n}:{version}" for n in names]


def _bundle(path: Path, *, without: str = "") -> Path:
    """An installer bundle made from this checkout, laid out as the channel's.

    Its members are named ``./docker-compose.yml`` and so on, as in the
    published bundle.tar.gz.
    """
    files = [
        *sorted(REPO_ROOT.glob("docker-compose*.yml")),
        *sorted(p for p in (REPO_ROOT / "nginx").rglob("*") if p.is_file()),
        REPO_ROOT / "airgap-load.sh",
        REPO_ROOT / ".env.example",
    ]
    with tarfile.open(path, "w:gz") as tar:
        for f in files:
            name = str(f.relative_to(REPO_ROOT))
            if name != without:
                tar.add(f, arcname=f"./{name}")
    return path


Run = Callable[..., tuple[subprocess.CompletedProcess[str], dict[str, Any]]]


@pytest.fixture
def upgrade(tmp_path: Path, compose_services, compose_required_env) -> Run:
    """Run upgrade.sh on a host installed offline from an August bundle.

    Its compose files are this checkout's, marked so a refresh shows. Its .env
    has neither shared secret, and its nginx/Dockerfile still installs gettext.
    With ``network``, the download serves the bundle and pulls succeed.
    """
    del compose_services  # requested only to skip when there is no compose
    real_docker = shutil.which("docker")
    assert real_docker
    home = tmp_path / "home"
    (home / "nginx").mkdir(parents=True)
    for f in REPO_ROOT.glob("docker-compose*.yml"):
        shutil.copy(f, home / f.name)
    with (home / "docker-compose.yml").open("a", encoding="utf-8") as fh:
        fh.write(f"{AUGUST}\n")
    (home / "nginx/Dockerfile").write_text(OLD_DOCKERFILE, encoding="utf-8")
    env_lines = {
        k: v
        for k, v in compose_required_env.items()
        if k not in ("GATEWAY_SHARED_SECRET", "CORE_STORAGE_SHARED_SECRET")
    }
    env_lines["CAURA_VERSION"] = FROM

    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    for name, body in _FAKES.items():
        (fake / "bin" / name).write_text(body, encoding="utf-8")
        (fake / "bin" / name).chmod(0o755)
    bundle = _bundle(tmp_path / "bundle.tar.gz")

    def run(
        *args: str,
        loaded: list[str] | None = None,
        offline_install: bool = True,
        extra_env: dict[str, str] | None = None,
        network: bool = False,
        healthy: bool = True,
        via: str = "file",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        lines = {**env_lines, **(extra_env or {})}
        (home / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8"
        )
        (home / "install.state.json").write_text(
            f'{{\n  "version": "{FROM}",\n  "offline": {str(offline_install).lower()}\n}}\n',
            encoding="utf-8",
        )
        (fake / "loaded").write_text(
            "".join(f"{i}\n" for i in (loaded or [])), encoding="utf-8"
        )
        for log in ("calls", "curl"):
            (fake / log).write_text("", encoding="utf-8")
        script = str(REPO_ROOT / "upgrade.sh")
        bash = {
            "file": ["bash", script],
            # Read off stdin, as under `curl ... | sudo bash -s --`.
            "stdin": ["bash", "-s", "--"],
            # Read from a pipe bash can name, /dev/fd/N, which is not a file.
            "fd": ["bash", "-c", 'bash <(cat "$0") "$@"', script],
        }[via]
        proc = subprocess.run(
            [*bash, "--yes", *args],
            input=Path(script).read_text(encoding="utf-8") if via == "stdin" else None,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                "PATH": f"{fake / 'bin'}{os.pathsep}{os.environ['PATH']}",
                "HOME": os.environ.get("HOME", str(tmp_path)),
                **{k: os.environ[k] for k in ("DOCKER_CONFIG",) if k in os.environ},
                "CAURA_HOME": str(home),
                "REAL_DOCKER": real_docker,
                "FAKE": str(fake),
                **({"FAKE_BUNDLE": str(bundle), "FAKE_PULL": "ok"} if network else {}),
                **({} if healthy else {"FAKE_HEALTH": "unhealthy"}),
            },
            check=False,
        )
        env_after = dict(
            ln.split("=", 1)
            for ln in (home / ".env").read_text(encoding="utf-8").splitlines()
            if "=" in ln
        )
        return proc, {
            "calls": (fake / "calls").read_text(encoding="utf-8").splitlines(),
            "curl": (fake / "curl").read_text(encoding="utf-8").splitlines(),
            "env": env_after,
            "home": home,
            "bundle": bundle,
        }

    return run


def _compose_calls(calls: list[str], verb: str) -> list[str]:
    return [c for c in calls if c.startswith("compose ") and f" {verb}" in c]


OFFLINE = ("--offline", "--bundle", "bundle.tar.gz", "--to", TO)
EVERYTHING = [*_release(TO), *UPSTREAM, GATEWAY_BASE]


# -- the upgrade itself --------------------------------------------------------


def test_an_august_host_upgrades_with_nothing_from_the_network(upgrade: Run):
    proc, r = upgrade(*OFFLINE, loaded=EVERYTHING)
    assert proc.returncode == 0, proc.stderr
    assert r["curl"] == [], "an offline upgrade downloaded something"
    assert not _compose_calls(r["calls"], "pull"), "an offline upgrade pulled"
    # The stack is built and started from the loaded images, not the registry's.
    build = _compose_calls(r["calls"], "build --no-cache gateway")
    up = _compose_calls(r["calls"], "up -d")
    assert build and up
    for call in (*build, *up):
        assert "-f docker-compose.airgap.yml" in call, call
    assert r["calls"].index(build[0]) < r["calls"].index(up[0])


def test_the_upgrade_brings_the_bundle_and_the_secrets_the_release_needs(
    upgrade: Run,
):
    proc, r = upgrade(*OFFLINE, loaded=EVERYTHING)
    assert proc.returncode == 0, proc.stderr
    home = r["home"]
    for name in ("docker-compose.yml", "docker-compose.airgap.yml", "nginx/Dockerfile"):
        assert (home / name).read_bytes() == (REPO_ROOT / name).read_bytes(), (
            f"{name} was not refreshed from the bundle"
        )
    assert r["env"]["CAURA_VERSION"] == TO
    # Without these core-api refuses to start, and an August .env has neither.
    assert r["env"].get("CORE_STORAGE_SHARED_SECRET")
    assert r["env"].get("GATEWAY_SHARED_SECRET")


def test_the_rollback_it_prints_runs_offline_too(upgrade: Run):
    proc, r = upgrade(*OFFLINE, loaded=EVERYTHING)
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "Rollback:" in ln)
    assert f"upgrade.sh' --offline --bundle '{r['bundle']}' --to {FROM}" in line
    assert "curl" not in line


def test_a_rollback_line_with_no_file_to_name_says_what_to_fill_in(upgrade: Run):
    proc, r = upgrade(*OFFLINE, loaded=EVERYTHING, via="fd")
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "Rollback:" in ln)
    assert f"'<path to upgrade.sh>' --offline --bundle '{r['bundle']}'" in line
    assert "/dev/fd" not in line


def test_an_offline_run_piped_into_bash_is_told_to_run_the_file(upgrade: Run):
    # Piped, the script re-runs from a copy it downloads, and offline there is
    # nothing to download from.
    proc, r = upgrade(*OFFLINE, loaded=EVERYTHING, via="stdin")
    assert proc.returncode == 2, proc.stderr
    assert "sudo bash ./upgrade.sh --offline" in proc.stderr
    assert r["curl"] == [] and r["calls"] == []


def test_local_embeddings_use_the_air_gap_embedder_overlay(upgrade: Run):
    proc, r = upgrade(
        *OFFLINE,
        loaded=[*_release(TO, embedder=True), *UPSTREAM, GATEWAY_BASE],
        extra_env={"EMBEDDING_PROVIDER": "local"},
    )
    assert proc.returncode == 0, proc.stderr
    for call in _compose_calls(r["calls"], "up -d"):
        assert "-f docker-compose.embedder.airgap.yml" in call
        assert "docker-compose.embedder.yml" not in call


def test_a_missing_air_gap_embedder_overlay_stops_before_anything(
    upgrade: Run, tmp_path: Path
):
    # Only a hand deletion removes it. Going on would start a core-api that
    # cannot embed offline, and nothing would fail.
    overlay = tmp_path / "home/docker-compose.embedder.airgap.yml"
    overlay.unlink()
    loaded = [*_release(TO), *_release(TO, embedder=True), *UPSTREAM, GATEWAY_BASE]
    local = {"EMBEDDING_PROVIDER": "local"}
    proc, r = upgrade(*OFFLINE, loaded=loaded, extra_env=local)
    assert proc.returncode == 1
    assert "docker-compose.embedder.airgap.yml is missing" in proc.stderr
    assert not _compose_calls(r["calls"], "exec"), "it took a backup first"
    assert r["env"]["CAURA_VERSION"] == FROM
    # The command it prints puts the file back, and the upgrade then runs.
    restore = proc.stderr.split("run this again: ", 1)[1].strip()
    subprocess.run(["sh", "-c", restore], check=True)
    assert overlay.read_bytes() == (REPO_ROOT / overlay.name).read_bytes()
    proc, r = upgrade(*OFFLINE, loaded=loaded, extra_env=local)
    assert proc.returncode == 0, proc.stderr
    assert (
        "-f docker-compose.embedder.airgap.yml"
        in _compose_calls(r["calls"], "up -d")[0]
    )


# -- what it refuses -----------------------------------------------------------


def test_a_release_not_yet_loaded_stops_before_anything_restarts(upgrade: Run):
    proc, r = upgrade(*OFFLINE, loaded=[*_release(FROM), *UPSTREAM, GATEWAY_BASE])
    assert proc.returncode == 3
    assert f"{OLD_NS}/core-api:{TO}" in proc.stderr
    assert "airgap-load.sh" in proc.stderr
    assert not _compose_calls(r["calls"], "build")
    assert not _compose_calls(r["calls"], "up")
    assert r["env"]["CAURA_VERSION"] == FROM


def test_a_stop_before_restart_puts_the_compose_files_back(upgrade: Run):
    # The stop is made to be run again, and that run snapshots these files.
    proc, r = upgrade(*OFFLINE, loaded=[*_release(FROM), *UPSTREAM, GATEWAY_BASE])
    assert proc.returncode == 3
    assert AUGUST in (r["home"] / "docker-compose.yml").read_text(encoding="utf-8")


def test_a_rollback_after_a_retry_restores_what_the_old_version_ran(upgrade: Run):
    # Stopped once for a release not yet loaded, then run again with it loaded,
    # and that run fails its health wait: the rollback has to put back the
    # compose files the old version was running, not the bundle's.
    proc, _ = upgrade(*OFFLINE, loaded=[*_release(FROM), *UPSTREAM, GATEWAY_BASE])
    assert proc.returncode == 3
    proc, r = upgrade(
        *OFFLINE, "--health-timeout", "1", loaded=EVERYTHING, healthy=False
    )
    assert proc.returncode == 4, proc.stderr
    assert AUGUST in (r["home"] / "docker-compose.yml").read_text(encoding="utf-8")
    assert r["env"]["CAURA_VERSION"] == FROM


def test_a_missing_gateway_base_is_named(upgrade: Run):
    proc, r = upgrade(*OFFLINE, loaded=[*_release(TO), *UPSTREAM])
    assert proc.returncode == 3
    assert GATEWAY_BASE in proc.stderr
    assert "docs/install-airgap.md" in proc.stderr
    assert not _compose_calls(r["calls"], "build")


def test_offline_without_a_bundle_stops_before_anything(upgrade: Run):
    proc, r = upgrade("--offline", "--to", TO, loaded=EVERYTHING)
    assert proc.returncode == 2
    assert "--bundle" in proc.stderr
    assert r["calls"] == [] and r["curl"] == []
    assert r["env"]["CAURA_VERSION"] == FROM


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p.write_bytes(b"<html>not found</html>"), "not a gzipped tarball"),
        (
            lambda p: _bundle(p, without="docker-compose.airgap.yml"),
            "has no docker-compose.airgap.yml",
        ),
        (lambda p: _bundle(p, without="nginx/Dockerfile"), "has no nginx/Dockerfile"),
    ],
    ids=["not-a-tarball", "no-air-gap-overlay", "no-gateway-dockerfile"],
)
def test_a_file_that_is_not_the_bundle_is_refused_before_anything(
    upgrade: Run, tmp_path: Path, make: Callable[[Path], Any], message: str
):
    make(tmp_path / "other.tar.gz")
    proc, r = upgrade(
        "--offline", "--bundle", "other.tar.gz", "--to", TO, loaded=EVERYTHING
    )
    assert proc.returncode == 2
    assert message in proc.stderr
    assert r["calls"] == []
    assert (r["home"] / "nginx/Dockerfile").read_text(
        encoding="utf-8"
    ) == OLD_DOCKERFILE


def test_an_offline_install_upgraded_without_offline_is_told_how(upgrade: Run):
    # No network, as on the air-gapped host: the download fails.
    proc, r = upgrade("--to", TO, loaded=EVERYTHING)
    assert proc.returncode == 3
    assert "upgrade.sh --offline --bundle" in proc.stderr
    # The warning says it stops before these change; hold it to that.
    assert r["env"]["CAURA_VERSION"] == FROM
    assert not _compose_calls(r["calls"], "up")


# -- the connected path is unchanged ---------------------------------------------


def test_a_connected_upgrade_still_downloads_the_bundle_and_pulls(upgrade: Run):
    proc, r = upgrade("--to", TO, offline_install=False, network=True)
    assert proc.returncode == 0, proc.stderr
    assert any("bundle.tar.gz" in c for c in r["curl"])
    assert _compose_calls(r["calls"], "pull")
    assert not any("airgap" in c for c in r["calls"])
    assert "install.sh --offline" not in proc.stderr


def test_a_local_bundle_replaces_the_download_on_a_connected_host(upgrade: Run):
    proc, r = upgrade(
        "--bundle", "bundle.tar.gz", "--to", TO, offline_install=False, network=True
    )
    assert proc.returncode == 0, proc.stderr
    assert r["curl"] == []
    assert _compose_calls(r["calls"], "pull")
    assert (r["home"] / "nginx/Dockerfile").read_bytes() == (
        REPO_ROOT / "nginx/Dockerfile"
    ).read_bytes()


# -- what the image check relies on --------------------------------------------


def test_the_gateway_is_the_only_service_compose_builds(compose_services):
    # The offline check skips the gateway's image by name, because it is built,
    # not loaded. A second built service, or a renamed gateway image, would be
    # reported missing on every offline upgrade.
    services = compose_services()
    built = {name for name, svc in services.items() if "build" in svc}
    assert built == {"gateway"}
    assert fnmatch.fnmatch(services["gateway"]["image"], "*/gateway:*")
