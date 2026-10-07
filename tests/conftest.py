"""Rendering the stack with docker compose, for the tests that check what it resolves.

The variables the compose file requires are listed here once. Each test used to
carry its own list: when the file began requiring ``GATEWAY_SHARED_SECRET`` on
2026-08-25, two of those tests skipped on every run until 2026-10-07, and
nothing said so.
"""

from __future__ import annotations

import functools
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
