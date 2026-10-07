"""An air-gapped host must end up running the gateway it builds from nginx/.

Two things stopped that, both found by running them offline:

- Tarballs up to v2.13.0 carried another gateway image under the very tag the
  host builds. ``docker compose up`` runs a loaded image instead of building,
  so an air-gapped upgrade ran it, and it restarted forever: it routes a stack
  on-prem does not have. airgap-load.sh now removes whatever gateway tag the
  load set, and the upgrade docs build the gateway before ``up``.
- The build itself needed the network: ``apk add gettext`` in nginx/Dockerfile,
  for an ``envsubst`` the base already ships, and a base image no tarball
  carried. The Dockerfile now installs nothing, and install.sh --offline and
  airgap-load.sh check that the base is loaded.

Shell is run under bash against a fake ``docker``, as test_postgres_tls.py
runs upgrade.sh's TLS check.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "nginx/Dockerfile"

# The namespace older compose files resolve, and so the one older tarballs
# loaded the gateway under next to caura-onprem.
OLD_NS = "memclaw-onprem"  # legacy-name-ok: the namespace tarballs up to v2.13.0 loaded the gateway under, which these tests remove
BASE = "nginx:1.27-alpine"


def _function(rel: str, name: str) -> str:
    """A shell function from ``rel``, by brace matching at column 0."""
    lines = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip() == f"{name}() {{"]
    assert len(idx) == 1, f"{rel}: {name}() matched {len(idx)} definitions"
    close = next(i for i in range(idx[0] + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[idx[0] : close + 1])


def _bash(
    script: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **(env or {})},
        check=False,
    )


# -- nginx/Dockerfile builds from its base alone --------------------------------


def test_the_gateway_dockerfile_fetches_nothing_while_building():
    # Instructions, with continuation lines joined: a package install split
    # across lines is still one RUN.
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    runs = [ln for ln in text.splitlines() if ln.strip().upper().startswith("RUN ")]
    fetchers = re.compile(r"\b(apk|apt|apt-get|yum|dnf|pip3?|npm|curl|wget|git)\b")
    offending = [r for r in runs if fetchers.search(r)]
    assert not offending, (
        "an air-gapped host builds this image, so no step may reach a network: "
        f"{offending}"
    )


def test_the_dockerfile_checks_the_base_ships_envsubst():
    # The entrypoint needs envsubst and nothing installs it any more, so the
    # build has to fail if a new base stops shipping it.
    entrypoint = (REPO_ROOT / "nginx/docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "envsubst" in entrypoint
    assert "command -v envsubst" in DOCKERFILE.read_text(encoding="utf-8")


# -- reading the base out of the Dockerfile ------------------------------------


def test_both_scripts_read_the_base_the_same_way():
    assert _function("install.sh", "gateway_bases") == _function(
        "airgap-load.sh", "gateway_bases"
    )


@pytest.mark.parametrize(
    ("dockerfile", "bases"),
    [
        (None, [BASE]),
        (
            "FROM --platform=linux/amd64 nginx:1.31-alpine AS base\nRUN true\nFROM base\n",
            ["nginx:1.31-alpine"],
        ),
        (
            "from alpine:3.20 as build\nFROM nginx:1.27-alpine\nCOPY --from=build /x /y\n",
            ["alpine:3.20", BASE],
        ),
        ("FROM scratch AS empty\nFROM nginx:1.27-alpine\n", [BASE]),
    ],
    ids=["the-real-one", "flag-and-stage-alias", "two-bases", "scratch"],
)
def test_gateway_bases(tmp_path: Path, dockerfile: str | None, bases: list[str]):
    path = DOCKERFILE
    if dockerfile is not None:
        path = tmp_path / "Dockerfile"
        path.write_text(dockerfile, encoding="utf-8")
    proc = _bash(
        _function("install.sh", "gateway_bases") + f'\ngateway_bases "{path}"', tmp_path
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == bases


# -- install.sh --offline ------------------------------------------------------


def _check_offline_images(
    tmp_path: Path, present: list[str], *, dockerfile: bool = True
) -> subprocess.CompletedProcess[str]:
    # install.sh makes nginx/ itself and fills it only if the bundle has one.
    (tmp_path / "nginx").mkdir()
    if dockerfile:
        shutil.copy(DOCKERFILE, tmp_path / "nginx/Dockerfile")
    script = "\n".join(
        [
            "set -euo pipefail",
            'die() { echo "DIE $1" >&2; exit "${2:-1}"; }',
            # `docker image inspect X`: present or not.
            f'docker() {{ case " {" ".join(present)} " in *" $3 "*) return 0 ;; esac; return 1; }}',
            _function("install.sh", "gateway_bases"),
            _function("install.sh", "check_offline_images"),
            "check_offline_images",
            'echo "PASSED"',
        ]
    )
    return _bash(script, tmp_path)


UPSTREAM = ["pgvector/pgvector:pg16", "redis:7-alpine", "rabbitmq:3-management-alpine"]


def test_offline_install_passes_with_every_base_loaded(tmp_path: Path):
    proc = _check_offline_images(tmp_path, [*UPSTREAM, BASE])
    assert proc.returncode == 0, proc.stderr
    assert "PASSED" in proc.stdout


def test_offline_install_stops_without_the_gateway_base(tmp_path: Path):
    proc = _check_offline_images(tmp_path, UPSTREAM)
    assert proc.returncode == 3
    assert BASE in proc.stderr
    assert "docs/install-airgap.md" in proc.stderr


def test_offline_install_still_stops_without_an_upstream_base(tmp_path: Path):
    proc = _check_offline_images(tmp_path, [UPSTREAM[0], UPSTREAM[2], BASE])
    assert proc.returncode == 3
    assert UPSTREAM[1] in proc.stderr


def test_offline_install_says_so_when_the_bundle_has_no_gateway_dockerfile(
    tmp_path: Path,
):
    # Not awk's "can't open file" and its exit status: the installer's own
    # error, as for every other incomplete bundle.
    proc = _check_offline_images(tmp_path, [*UPSTREAM, BASE], dockerfile=False)
    assert proc.returncode == 3, proc.stderr
    assert proc.stderr.startswith("DIE --offline: nginx/Dockerfile missing")


# -- airgap-load.sh ------------------------------------------------------------

# Keeps images in a file as "repo:tag id" lines. `load` adds $FAKE_LOAD's lines,
# replacing any tag they reuse, as docker load does.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
db="$FAKE_DOCKER_DIR/images"; touch "$db"
case "$1" in
  load)
    cat >/dev/null
    while read -r ref id; do
      [ -n "$ref" ] || continue
      grep -v "^$ref " "$db" > "$db.new" || true; mv "$db.new" "$db"
      echo "$ref $id" >> "$db"
      echo "Loaded image: $ref"
    done <<< "$FAKE_LOAD" ;;
  images)
    case "$*" in *ID*) cat "$db" ;; *) cut -d' ' -f1 "$db" ;; esac ;;
  image)
    case "$2" in
      inspect) grep -q "^$3 " "$db" ;;
      rm) echo "$3" >> "$FAKE_DOCKER_DIR/removed"
          grep -v "^$3 " "$db" > "$db.new" || true; mv "$db.new" "$db" ;;
    esac ;;
esac
"""


def _airgap_load(
    tmp_path: Path, *, before: list[str], loads: list[str]
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    bundle = tmp_path / "bundle"
    (bundle / "nginx").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "airgap-load.sh", bundle)
    shutil.copy(DOCKERFILE, bundle / "nginx/Dockerfile")
    tarball = tmp_path / "images.tar.gz"
    tarball.write_bytes(gzip.compress(b"images"))
    fake_dir = tmp_path / "docker"
    (fake_dir / "bin").mkdir(parents=True)
    fake = fake_dir / "bin/docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    (fake_dir / "images").write_text(
        "".join(f"{ln}\n" for ln in before), encoding="utf-8"
    )
    proc = subprocess.run(
        ["bash", str(bundle / "airgap-load.sh"), str(tarball)],
        cwd=bundle,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{fake_dir / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_DIR": str(fake_dir),
            "FAKE_LOAD": "\n".join(loads),
        },
        check=False,
    )
    removed_file = fake_dir / "removed"
    removed = removed_file.read_text().split() if removed_file.exists() else []
    left = (fake_dir / "images").read_text().split("\n")
    return proc, removed, [ln.split()[0] for ln in left if ln]


SERVICES = ["caura-onprem/core-api:v2.13.0 c1", f"{OLD_NS}/core-api:v2.13.0 c1"] + [
    f"{img} u{i}" for i, img in enumerate(UPSTREAM)
]


def test_an_older_tarballs_gateway_is_removed_and_the_build_kept(tmp_path: Path):
    proc, removed, left = _airgap_load(
        tmp_path,
        # Gateways this host built: one for an older version, one for the
        # version being loaded, whose tag the load takes over.
        before=[
            f"{OLD_NS}/gateway:v2.11.20 built1",
            f"{OLD_NS}/gateway:v2.13.0 built2",
        ],
        loads=[
            *SERVICES,
            "caura-onprem/gateway:v2.13.0 saas",
            f"{OLD_NS}/gateway:v2.13.0 saas",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(removed) == sorted(
        ["caura-onprem/gateway:v2.13.0", f"{OLD_NS}/gateway:v2.13.0"]
    )
    assert f"{OLD_NS}/gateway:v2.11.20" in left, "removed a gateway this host built"
    assert "caura-onprem/core-api:v2.13.0" in left


def test_a_missing_gateway_base_is_reported_with_how_to_bring_it(tmp_path: Path):
    proc, _, _ = _airgap_load(tmp_path, before=[], loads=SERVICES)
    assert proc.returncode == 0, proc.stderr
    assert f"docker pull --platform linux/amd64 {BASE}" in proc.stderr
    assert "docker load" in proc.stderr


def test_a_tarball_with_the_base_and_no_gateway_needs_nothing_more(tmp_path: Path):
    proc, removed, _ = _airgap_load(
        tmp_path, before=[], loads=[*SERVICES, f"{BASE} n1"]
    )
    assert proc.returncode == 0, proc.stderr
    assert removed == []
    assert "WARNING" not in proc.stderr
    listing = proc.stdout.split("==> Loaded images:", 1)[1].split("==> Ready", 1)[0]
    assert BASE in listing, "the loaded base is not in the listing"


def test_the_printed_next_steps_build_the_gateway_before_starting(tmp_path: Path):
    proc, _, _ = _airgap_load(tmp_path, before=[], loads=[*SERVICES, f"{BASE} n1"])
    out = proc.stdout
    assert "build --no-cache gateway" in out and "up -d" in out
    assert out.index("build --no-cache gateway") < out.index("up -d")


def test_the_air_gap_upgrade_docs_build_the_gateway_before_starting():
    text = (REPO_ROOT / "docs/upgrade.md").read_text(encoding="utf-8")
    section = text.split("## Air-gap upgrade", 1)[1].split("\n## ", 1)[0]
    assert "build --no-cache gateway" in section and "up -d" in section
    assert section.index("build --no-cache gateway") < section.index("up -d")
