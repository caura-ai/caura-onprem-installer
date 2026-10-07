"""``POSTGRES_REQUIRE_SSL`` in .env reaches core-storage-api under the name it reads.

core-storage-api reads ``POSTGRES_REQUIRE_SSL`` (``postgres_require_ssl`` in its
settings, no prefix) since v2.13.0. The compose passed the .env value on as
``ALLOYDB_REQUIRE_SSL``, a name core-storage-api has never read, so an
operator who set it got neither TLS enforcement nor an error. Resolved by
compose itself, since the interpolation is compose's semantics, not ours.

That turns a setting that did nothing into one that stops core-storage-api
starting, so upgrade.sh asks the database first; the last block runs its real
check against a fake ``docker``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]

# What compose needs set to render the file at all; none of it is under test.
_REQUIRED = {
    "GATEWAY_SHARED_SECRET": "g",
    "POSTGRES_PASSWORD": "x",
    "JWT_SECRET": "y" * 40,
    "CORE_ADMIN_API_KEY": "z",
    "PLATFORM_OPERATIONS_INTERNAL_TOKEN": "t",
    "SETTINGS_ENCRYPTION_KEY": "k",
    "PUBLIC_HOSTNAME": "h.example",
    "CORE_STORAGE_SHARED_SECRET": "s",
}


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    )
    return probe.returncode == 0


# Skip only when there is no compose to ask. A render that fails is a failure:
# skipping on it is how a missing required variable turns a test into one that
# never runs and never says so.
needs_compose = pytest.mark.skipif(
    not _compose_available(), reason="needs docker compose"
)


def _services(env: dict[str, str]) -> dict[str, Any]:
    proc = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **_REQUIRED, **env},
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, (
        f"compose could not render the file: {proc.stderr.strip()}"
    )
    services: dict[str, Any] = json.loads(proc.stdout)["services"]
    return services


def _require_ssl(env: dict[str, str]) -> str | None:
    return _services(env)["core-storage-api"]["environment"].get("POSTGRES_REQUIRE_SSL")


@needs_compose
def test_core_storage_api_gets_the_setting_under_the_name_it_reads():
    assert _require_ssl({"POSTGRES_REQUIRE_SSL": "true"}) == "true"


@needs_compose
@pytest.mark.parametrize(
    "env", [{}, {"POSTGRES_REQUIRE_SSL": ""}], ids=["unset", "blank"]
)
def test_blank_or_unset_resolves_to_false(env: dict[str, str]):
    # install.sh writes the key blank unless asked for TLS, and the service
    # refuses to start on a blank boolean, so blank must become "false".
    assert _require_ssl(env) == "false"


@needs_compose
def test_no_service_is_given_the_name_nothing_reads():
    services = _services({"POSTGRES_REQUIRE_SSL": "true"})
    given = sorted(
        name
        for name, svc in services.items()
        if "ALLOYDB_REQUIRE_SSL" in (svc.get("environment") or {})
    )
    # core-api read it until OSS backend-v2.18.0, older than any image this
    # compose can run (test_db_credentials.py has the evidence).
    assert not given, (
        f"nothing this compose runs reads ALLOYDB_REQUIRE_SSL, but {given} get it"
    )


# -- upgrade.sh: stop before anything changes if the database refuses TLS -----


def _upgrade_sh_function(name: str) -> str:
    """A shell function from upgrade.sh, by brace matching at column 0."""
    lines = (REPO_ROOT / "upgrade.sh").read_text(encoding="utf-8").splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip() == f"{name}() {{"]
    assert len(idx) == 1, f"upgrade.sh: {name}() matched {len(idx)} definitions"
    close = next(i for i in range(idx[0] + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[idx[0] : close + 1])


def _check_db_tls(
    tmp_path: Path, env_line: str, *, to: str = "v2.13.1", probe_rc: int = 3
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run upgrade.sh's ``_check_db_tls`` over a .env holding ``env_line``.

    ``docker`` is a shell function standing in for ``docker compose exec``,
    which passes the probe's exit code through: 3 is a refusal of TLS.
    Returns the run and every docker command it was asked for.
    """
    home = tmp_path / "install"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text(f"CAURA_VERSION=v2.11.19\n{env_line}\n", encoding="utf-8")
    calls = tmp_path / "docker-calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            f'cd "{home}"',
            'log() { echo "LOG $*"; }',
            'warn() { echo "WARN $*" >&2; }',
            'die() { echo "DIE $1" >&2; exit "${2:-1}"; }',
            (
                f'docker() {{ echo "$*" >>"{calls}"; '
                'echo "ConnectionError: rejected SSL upgrade" >&2; '
                f"return {probe_rc}; }}"
            ),
            "COMPOSE_FILES=(-f docker-compose.yml)",
            f'TO_VERSION="{to}"',
            f'CAURA_HOME="{home}"',
            _upgrade_sh_function("_GET"),
            _upgrade_sh_function("_check_db_tls"),
            "_check_db_tls",
            'echo "CONTINUED"',
        ]
    )
    before = env_file.read_text(encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=False,
    )
    assert env_file.read_text(encoding="utf-8") == before, "the check changed .env"
    return proc, calls.read_text() if calls.exists() else ""


def test_a_database_that_refuses_tls_stops_the_upgrade(tmp_path: Path):
    proc, calls = _check_db_tls(tmp_path, "POSTGRES_REQUIRE_SSL=true")
    assert "exec -T core-storage-api python -c" in calls
    assert proc.returncode == 1, proc.stderr
    assert "refuses TLS" in proc.stderr and "Nothing has been changed" in proc.stderr
    assert "CONTINUED" not in proc.stdout


def test_a_database_that_accepts_tls_lets_it_continue(tmp_path: Path):
    proc, calls = _check_db_tls(tmp_path, "POSTGRES_REQUIRE_SSL=true", probe_rc=0)
    assert calls
    assert proc.returncode == 0 and "CONTINUED" in proc.stdout, proc.stderr


def test_a_check_that_cannot_run_only_warns(tmp_path: Path):
    # e.g. core-storage-api is not running, so `compose exec` itself fails.
    proc, calls = _check_db_tls(tmp_path, "POSTGRES_REQUIRE_SSL=true", probe_rc=1)
    assert calls
    assert proc.returncode == 0 and "CONTINUED" in proc.stdout, proc.stderr
    assert "WARN Could not check" in proc.stderr


@pytest.mark.parametrize(
    "env_line",
    [
        "POSTGRES_REQUIRE_SSL=TRUE",
        "POSTGRES_REQUIRE_SSL=1",
        'POSTGRES_REQUIRE_SSL="true"',
        "POSTGRES_REQUIRE_SSL=true   # require TLS",
    ],
)
def test_every_spelling_compose_passes_as_true_is_checked(
    tmp_path: Path, env_line: str
):
    proc, calls = _check_db_tls(tmp_path, env_line)
    assert calls and proc.returncode == 1, proc.stderr


@pytest.mark.parametrize(
    "env_line",
    [
        "POSTGRES_REQUIRE_SSL=false",
        "POSTGRES_REQUIRE_SSL=",
        "",
        # The line .env.example ships, inline comment and all.
        "POSTGRES_REQUIRE_SSL=false             # true: core-storage-api refuses",
    ],
)
def test_off_asks_nothing(tmp_path: Path, env_line: str):
    proc, calls = _check_db_tls(tmp_path, env_line)
    assert not calls
    assert proc.returncode == 0 and "CONTINUED" in proc.stdout, proc.stderr


def test_an_older_target_is_not_blocked(tmp_path: Path):
    """It does not read the setting, and a rollback runs through this script."""
    proc, calls = _check_db_tls(tmp_path, "POSTGRES_REQUIRE_SSL=true", to="v2.11.20")
    assert not calls
    assert proc.returncode == 0 and "CONTINUED" in proc.stdout, proc.stderr


@pytest.mark.parametrize("to", ["v2.13.0", "v2.13.0-rc1", "v3.0.0", "latest"])
def test_targets_that_enforce_it_are_checked(tmp_path: Path, to: str):
    proc, calls = _check_db_tls(tmp_path, "POSTGRES_REQUIRE_SSL=true", to=to)
    assert calls and proc.returncode == 1, proc.stderr
