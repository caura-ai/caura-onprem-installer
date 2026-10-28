"""Rendering the stack with docker compose, for the tests that check what it resolves.

The variables the compose file requires are listed here once. Each test used to
carry its own list: when the file began requiring ``GATEWAY_SHARED_SECRET`` on
2026-08-25, two of those tests skipped on every run until 2026-10-07, and
nothing said so.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The `:?` variables of docker-compose.yml, without which compose will not
# render it. None is under test; a test about one of them overrides it.
_REQUIRED = {
    "GATEWAY_SHARED_SECRET": "g",
    "POSTGRES_PASSWORD": "x",
    "JWT_SECRET": "y" * 40,
    "CORE_ADMIN_API_KEY": "z",
    "SETTINGS_ENCRYPTION_KEY": "k",
    "CORE_STORAGE_SHARED_SECRET": "s",
}


# What docker needs to find its compose plugin, which lives under
# $DOCKER_CONFIG/cli-plugins (by default ~/.docker/cli-plugins), and nothing
# compose interpolates. The probe and the render both run with it, so they
# cannot disagree about whether compose is there.
_DOCKER_ENV = {
    k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_CONFIG") if k in os.environ
}


@functools.cache
def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        env=_DOCKER_ENV,
        check=False,
    )
    return probe.returncode == 0


@pytest.fixture
def compose_required_env() -> dict[str, str]:
    """The required variables, for a test that writes them into a .env itself."""
    return dict(_REQUIRED)


@pytest.fixture
def compose_services() -> Callable[..., dict[str, Any]]:
    """Render ``docker-compose.yml`` in ``cwd`` and return its services.

    Compose sees what docker needs to find it, the required variables unless
    ``required=False``, and ``env``: nothing else from the shell running the
    tests. Skips only when there is no compose to ask: a file compose cannot
    render fails the test, because skipping on that is how the two tests went
    quiet.
    """
    if not _compose_available():
        pytest.skip("needs docker compose")

    def render(
        env: dict[str, str] | None = None,
        cwd: Path = REPO_ROOT,
        required: bool = True,
    ) -> dict[str, Any]:
        base = _REQUIRED if required else {}
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env={**_DOCKER_ENV, **base, **(env or {})},
            cwd=cwd,
            check=False,
        )
        assert proc.returncode == 0, (
            f"compose could not render the file: {proc.stderr.strip()}"
        )
        services: dict[str, Any] = json.loads(proc.stdout)["services"]
        return services

    return render


# -- The release signature airgap-load.sh checks -------------------------------
#
# The on-prem release signs each tarball's checksum keyless, as
# caura-enterprise's release-onprem.yml at the version's onprem-v* tag. Tests
# cannot mint that certificate, so a stand-in for cosign models what the real
# check decides: a bundle verifies only for the identity, issuer and bytes it
# was made for, against a trusted root that exists. Flags it does not know --
# an identity regexp, skipping the transparency log -- are refused.

RELEASE_IDENTITY = (
    "https://github.com/caura-ai/caura-enterprise/.github/workflows/"
    "release-onprem.yml@refs/tags/onprem-"
)
OIDC_ISSUER = "https://token.actions.githubusercontent.com"

_FAKE_COSIGN = r"""#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.environ["COSIGN_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if not args or args[0] != "verify-blob":
    sys.exit("only verify-blob")
values = {"--bundle", "--certificate-identity", "--certificate-oidc-issuer", "--trusted-root"}
opts, blobs, i = {}, [], 1
while i < len(args):
    if args[i] in values:
        opts[args[i]] = args[i + 1]
        i += 2
    elif args[i] == "--new-bundle-format":
        i += 1
    elif args[i].startswith("-"):
        sys.exit("unexpected flag " + args[i])
    else:
        blobs.append(args[i])
        i += 1
if set(opts) != values or len(blobs) != 1:
    sys.exit("incomplete verification")
if not os.path.isfile(opts["--trusted-root"]):
    sys.exit("no trusted root")
with open(opts["--bundle"]) as f:
    bundle = json.load(f)
with open(blobs[0], "rb") as f:
    digest = hashlib.sha256(f.read()).hexdigest()
signed = {
    "identity": opts["--certificate-identity"],
    "issuer": opts["--certificate-oidc-issuer"],
    "sha256": digest,
}
sys.exit(0 if bundle == signed else "signature does not verify")
"""


def fake_cosign(directory: Path) -> Path:
    """A bin directory holding the stand-in ``cosign``; it logs to $COSIGN_LOG."""
    bin_dir = directory / "cosign-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "cosign"
    fake.write_text(_FAKE_COSIGN, encoding="utf-8")
    fake.chmod(0o755)
    return bin_dir


@pytest.fixture
def cosign_bin(tmp_path: Path) -> Path:
    return fake_cosign(tmp_path)


def signed_release(
    directory: Path,
    version: str,
    *,
    payload: bytes | None = None,
    identity: str | None = None,
    issuer: str = OIDC_ISSUER,
    prefix: str = "caura-onprem",
) -> Path:
    """A release tarball with its checksum and that checksum's bundle, as published."""
    directory.mkdir(parents=True, exist_ok=True)
    if payload is None:
        payload = gzip.compress(b"images", mtime=0)
    tarball = directory / f"{prefix}-{version}.tar.gz"
    tarball.write_bytes(payload)
    checksum = tarball.with_name(tarball.name + ".sha256")
    checksum.write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="utf-8")
    bundle = checksum.with_name(checksum.name + ".sigstore.json")
    bundle.write_text(
        json.dumps(
            {
                "identity": identity or RELEASE_IDENTITY + version,
                "issuer": issuer,
                "sha256": hashlib.sha256(checksum.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return tarball


def installer_files(dest: Path) -> None:
    """airgap-load.sh and the trusted root it verifies with, as the bundle ships them."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "airgap-load.sh", dest)
    root = REPO_ROOT / "scripts/sigstore/trusted_root.json"
    if root.exists():
        (dest / "scripts/sigstore").mkdir(parents=True, exist_ok=True)
        shutil.copy(root, dest / "scripts/sigstore/trusted_root.json")
