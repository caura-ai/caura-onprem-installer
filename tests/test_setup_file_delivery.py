"""File delivery keeps proof out of Compose interpolation and existing .env."""

import csv
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "q" * 64


@pytest.mark.parametrize("caller", ["install.sh", "upgrade.sh"])
def test_prepare_uses_exact_auth_image_not_dependency_images(
    compose_services, compose_required_env, caller
):
    expected = compose_services()["platform-auth-api"]["image"]
    assignment = next(
        line.strip().split(" ||", 1)[0].removesuffix("\\").rstrip()
        for line in (ROOT / caller).read_text().splitlines()
        if line.strip().startswith("_setup_image=$(")
    )
    program = (
        'source "$ROOT/scripts/setup-token.sh"\n'
        'COMPOSE_FILES=(--env-file /dev/null -f "$ROOT/docker-compose.yml")\n'
        + assignment
        + '\nprintf "%s" "$_setup_image"'
    )
    result = subprocess.run(
        ["bash", "-c", program],
        env={
            **{
                key: os.environ[key]
                for key in ("PATH", "HOME", "DOCKER_CONFIG")
                if key in os.environ
            },
            **compose_required_env,
            "ROOT": str(ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_auth_receives_a_read_only_proof_file_without_interpolating_secret(
    compose_services,
):
    services = compose_services({"SETUP_TOKEN": TOKEN})
    auth = services["platform-auth-api"]
    assert "SETUP_TOKEN" not in auth["environment"]
    assert auth["environment"]["SETUP_TOKEN_FILE"] == "/run/caura/setup-token"
    mounts = [v for v in auth["volumes"] if v["target"] == "/run/caura/setup-token"]
    assert len(mounts) == 1
    assert mounts[0]["type"] == "bind" and mounts[0]["read_only"]
    assert mounts[0]["source"].endswith("/setup-token")
    assert not mounts[0]["bind"].get("create_host_path", True)
    for name, service in services.items():
        if name != "platform-auth-api":
            assert all(
                v["target"] != "/run/caura/setup-token"
                for v in service.get("volumes", [])
            )


@pytest.mark.parametrize("symlink", [False, True])
def test_environment_writer_preserves_existing_access_and_excludes_proof(
    tmp_path, symlink
):
    target = tmp_path / "managed-config" if symlink else tmp_path / ".env"
    target.write_text("old configuration")
    target.chmod(0o640)
    original = target.stat()
    env = tmp_path / ".env"
    if symlink:
        env.symlink_to(target)
    source = (ROOT / "install.sh").read_text()
    start = source.index("# Resolved here rather than in the heredoc")
    end = source.index("# Capture install state", start)
    result = subprocess.run(
        ["bash", "-c", "set -e\n" + source[start:end]],
        env={
            "PATH": os.environ["PATH"],
            "CAURA_HOME": str(tmp_path),
            "SETUP_TOKEN": TOKEN,
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SETUP_TOKEN=" not in env.read_text()
    assert TOKEN not in env.read_text()
    assert env.is_symlink() == symlink
    current = target.stat()
    assert (current.st_mode, current.st_uid, current.st_gid) == (
        original.st_mode,
        original.st_uid,
        original.st_gid,
    )


def test_preparation_assigns_only_the_private_token_to_service_uid(
    tmp_path, monkeypatch
):
    prepare = runpy.run_path(str(ROOT / "scripts/setup_token.py"))["prepare_token"]
    path = tmp_path / "setup-token"
    path.write_text(TOKEN + "\n")
    calls = []
    monkeypatch.setattr(
        os,
        "fchown",
        lambda fd, uid, gid: calls.append((uid, gid, os.fstat(fd).st_mode & 0o777)),
    )
    assert prepare(path, 1234, 5678) == TOKEN
    assert calls == [(1234, 5678, 0o600)]


def test_preparation_refuses_a_hard_link_without_changing_other_file(
    tmp_path, monkeypatch
):
    prepare = runpy.run_path(str(ROOT / "scripts/setup_token.py"))["prepare_token"]
    original = tmp_path / "operator-file"
    original.write_text(TOKEN)
    original.chmod(0o640)
    token = tmp_path / "setup-token"
    os.link(original, token)
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    with pytest.raises(ValueError):
        prepare(token, 1234, 5678)
    assert original.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "value", [b"short", b"a" * 257, b"a" * 32 + b" b", b"a" * 32 + b"\nb", b"\xff" * 64]
)
def test_container_preparation_refuses_invalid_proof_without_changing_it(
    tmp_path, monkeypatch, value
):
    prepare = runpy.run_path(str(ROOT / "scripts/setup_token.py"))["prepare_token"]
    path = tmp_path / "setup-token"
    path.write_bytes(value)
    calls = []
    monkeypatch.setattr(os, "fchown", lambda *args: calls.append(args))
    with pytest.raises((ValueError, UnicodeError)):
        prepare(path, 1234, 5678)
    assert path.read_bytes() == value
    assert calls == []


@pytest.mark.parametrize("capability", ["file-v1", "old-image"])
@pytest.mark.parametrize("special_paths", [False, True])
def test_file_helper_uses_selected_image_without_network_or_secret_arguments(
    tmp_path, capability, special_paths
):
    binary = tmp_path / "docker"
    binary.write_text("""#!/bin/sh
if [ "$1" = image ]; then printf '%s' "$CAPABILITY"; else printf '%s\\n' "$@" > "$CALLS"; fi
""")
    binary.chmod(0o755)
    helper_root = ROOT
    token = tmp_path / "setup-token"
    if special_paths:
        helper_root = tmp_path / 'install, "quoted"'
        (helper_root / "scripts").mkdir(parents=True)
        (helper_root / "scripts/setup-token.sh").write_text(
            (ROOT / "scripts/setup-token.sh").read_text()
        )
        token = helper_root / "setup-token"
    token.write_text(TOKEN)
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/scripts/setup-token.sh"; prepare_setup_token_for_auth "$2" test-auth-image',
            "test",
            str(helper_root),
            str(token),
        ],
        env={
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "CAPABILITY": capability,
            "CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert TOKEN not in result.stdout + result.stderr
    if capability == "old-image":
        assert result.returncode != 0
        assert not calls.exists()
    else:
        assert result.returncode == 0, result.stderr
        args = calls.read_text().splitlines()
        assert args[:6] == [
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
        ]
        assert args[args.index("--user") + 1] == "0:0"
        assert args[args.index("--log-driver") + 1] == "none"
        assert "test-auth-image" in args
        assert TOKEN not in calls.read_text()
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        decoded = [
            {
                field.partition("=")[0]: field.partition("=")[2]
                if "=" in field
                else True
                for field in next(csv.reader([mount], strict=True))
            }
            for mount in mounts
        ]
        assert decoded[0]["src"] == str(token)
        assert decoded[1]["src"] == str(helper_root / "scripts/setup_token.py")
        assert decoded[1]["readonly"] is True
        if real_docker := shutil.which("docker"):
            # --help and no IMAGE argument ensure this only parses CLI flags;
            # it cannot contact a daemon to launch a container.
            for mount in mounts:
                parsed = subprocess.run(
                    [real_docker, "run", "--mount", mount, "--help"],
                    capture_output=True,
                    check=False,
                )
                assert parsed.returncode == 0, parsed.stderr
