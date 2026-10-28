"""Restore must stop before mutation when the required proof mount is absent."""

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "proof", ["missing", "directory", "symlink", "regular", "old-bundle"]
)
def test_restore_checks_proof_before_database_mutation(tmp_path, proof):
    host = tmp_path / "install"
    host.mkdir()
    (host / "docker-compose.yml").write_text(
        "services:\n  platform-auth-api:\n    environment:\n      SETUP_TOKEN_FILE: /run/caura/setup-token\n"
        if proof != "old-bundle"
        else "services: {}\n"
    )
    token = host / "setup-token"
    if proof == "directory":
        token.mkdir()
    elif proof == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic proof" * 4)
        token.symlink_to(target)
    elif proof == "regular":
        token.write_text("synthetic proof" * 4)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "postgres.dump").write_text("synthetic dump")
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname=".")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\ncat >/dev/null\n')
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/restore.sh"), str(archive)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAURA_HOME": str(host),
            "CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if proof in {"regular", "old-bundle"}:
        assert result.returncode == 0, result.stderr
        assert "pg_restore" in calls.read_text()
    else:
        assert result.returncode == 2
        assert not calls.exists()
        assert (
            "docs/troubleshooting.md#missing-setup-token-bind-source" in result.stderr
        )
        assert not token.exists() if proof == "missing" else token.exists()
