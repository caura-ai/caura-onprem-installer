"""airgap-load.sh loads a release tarball only after proving the release built it.

The release signs each tarball's checksum keyless, and publishes the checksum
and its Sigstore bundle next to the tarball. Those sit on the same bucket as
the tarball, so neither is trusted for what it says about itself: the identity
is fixed in the script and names the version being loaded, and the trusted
root is the one this installer ships. Verification needs no network, because
an air-gapped host has none.

Run under bash against fake ``docker`` and ``cosign`` (see conftest.py).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import OIDC_ISSUER, RELEASE_IDENTITY, installer_files, signed_release

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = "v2.14.0"

# Loads nothing of its own, and lists one on-prem image so the script's
# "something was loaded" check passes. Every call is logged.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
printf 'docker %s\n' "$1" >> "$CALL_LOG"
case "$1" in
  load) cat >/dev/null ;;
  images) case "$*" in *ID*) echo "caura-onprem/core-api:v2.14.0 c1" ;;
                       *) echo "caura-onprem/core-api:v2.14.0" ;; esac ;;
  image) exit 0 ;;
esac
"""


def _run(
    tmp_path: Path,
    tarball: Path,
    *args: str,
    cosign: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[list[str]]]:
    installer = tmp_path / "installer"
    installer_files(installer)
    docker_bin = tmp_path / "docker-bin"
    docker_bin.mkdir(exist_ok=True)
    (docker_bin / "docker").write_text(_FAKE_DOCKER, encoding="utf-8")
    (docker_bin / "docker").chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.touch()
    cosign_log = tmp_path / "cosign.log"
    cosign_log.touch()
    path = [str(docker_bin), *([str(cosign)] if cosign else []), os.environ["PATH"]]
    proc = subprocess.run(
        ["bash", str(installer / "airgap-load.sh"), *args, str(tarball)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            # The host's own tools stay reachable; a cosign there does not.
            "PATH": os.pathsep.join(p for p in path if not _has_cosign(p, cosign)),
            "CALL_LOG": str(calls),
            "COSIGN_LOG": str(cosign_log),
        },
        check=False,
    )
    verifications = [json.loads(ln) for ln in cosign_log.read_text().splitlines()]
    return proc, calls.read_text().splitlines(), verifications


def _has_cosign(directory: str, allowed: Path | None) -> bool:
    return directory != str(allowed) and (Path(directory) / "cosign").exists()


def _loaded(calls: list[str]) -> bool:
    return "docker load" in calls


def test_a_signed_tarball_is_verified_against_this_release_then_loaded(
    tmp_path: Path, cosign_bin: Path
):
    tarball = signed_release(tmp_path / "release", VERSION)
    # A trusted root beside the tarball came from the bucket: never used.
    (tarball.parent / "trusted_root.json").write_text("{}", encoding="utf-8")
    proc, calls, verifications = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode == 0, proc.stderr
    checksum = f"{tarball}.sha256"
    assert verifications == [
        [
            "verify-blob",
            "--new-bundle-format",
            "--trusted-root",
            str(tmp_path / "installer/scripts/sigstore/trusted_root.json"),
            "--bundle",
            f"{checksum}.sigstore.json",
            "--certificate-identity",
            RELEASE_IDENTITY + VERSION,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            checksum,
        ]
    ]
    assert _loaded(calls)


def test_the_legacy_filename_is_verified_the_same_way(tmp_path, cosign_bin):
    # Every release is published under both names, with the same bundle.
    tarball = signed_release(
        tmp_path / "release",
        VERSION,
        prefix="memclaw-onprem",  # legacy-name-ok: the old tarball name every release still publishes, which rule 3 keeps loadable
    )
    proc, calls, verifications = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode == 0, proc.stderr
    assert len(verifications) == 1
    assert RELEASE_IDENTITY + VERSION in verifications[0]
    assert _loaded(calls)


def test_a_checksum_in_sha256sum_format_is_read_by_its_hash(
    tmp_path: Path, cosign_bin: Path
):
    # "<hash>  <name>", re-signed as a release would sign it.
    tarball = signed_release(tmp_path / "release", VERSION)
    checksum = Path(f"{tarball}.sha256")
    checksum.write_text(f"{checksum.read_text().strip()}  {tarball.name}\n")
    bundle = Path(f"{checksum}.sigstore.json")
    signed = json.loads(bundle.read_text())
    signed["sha256"] = hashlib.sha256(checksum.read_bytes()).hexdigest()
    bundle.write_text(json.dumps(signed))
    proc, calls, _ = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode == 0, proc.stderr
    assert _loaded(calls)


def test_a_modified_tarball_is_not_loaded(tmp_path: Path, cosign_bin: Path):
    tarball = signed_release(tmp_path / "release", VERSION)
    tarball.write_bytes(gzip.compress(b"other images", mtime=0))
    proc, calls, _ = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode != 0
    assert not _loaded(calls)
    assert "checksum" in proc.stderr


def test_a_modified_tarball_with_a_matching_checksum_is_not_loaded(
    tmp_path: Path, cosign_bin: Path
):
    # Whoever can replace the tarball can replace its checksum too; only the
    # signature over the checksum stops them.
    honest = signed_release(tmp_path / "release", VERSION)
    forged = signed_release(
        tmp_path / "forged", VERSION, payload=gzip.compress(b"other images", mtime=0)
    )
    honest.write_bytes(forged.read_bytes())
    Path(f"{honest}.sha256").write_bytes(Path(f"{forged}.sha256").read_bytes())
    proc, calls, verifications = _run(tmp_path, honest, cosign=cosign_bin)
    assert proc.returncode != 0
    assert len(verifications) == 1
    assert not _loaded(calls)


def test_another_versions_signed_tarball_is_not_loaded(tmp_path, cosign_bin):
    # A genuine older release, renamed as the version being installed.
    older = signed_release(tmp_path / "older", "v2.13.0")
    renamed = tmp_path / "release" / f"caura-onprem-{VERSION}.tar.gz"
    renamed.parent.mkdir()
    for suffix in ("", ".sha256", ".sha256.sigstore.json"):
        Path(f"{renamed}{suffix}").write_bytes(Path(f"{older}{suffix}").read_bytes())
    proc, calls, verifications = _run(tmp_path, renamed, cosign=cosign_bin)
    assert proc.returncode != 0
    assert verifications and RELEASE_IDENTITY + VERSION in verifications[0]
    assert not _loaded(calls)


@pytest.mark.parametrize(
    ("identity", "issuer"),
    [
        (
            "https://github.com/caura-ai/caura-enterprise/.github/workflows/"
            f"release-please.yml@refs/tags/onprem-{VERSION}",
            OIDC_ISSUER,
        ),
        (
            "https://github.com/caura-ai/caura-enterprise/.github/workflows/"
            "release-onprem.yml@refs/heads/dev",
            OIDC_ISSUER,
        ),
        (
            "https://github.com/someone/caura-enterprise/.github/workflows/"
            f"release-onprem.yml@refs/tags/onprem-{VERSION}",
            OIDC_ISSUER,
        ),
        (RELEASE_IDENTITY + VERSION, "https://accounts.google.com"),
    ],
    ids=["other-workflow", "other-ref", "other-repository", "other-issuer"],
)
def test_a_bundle_signed_by_anyone_else_is_not_loaded(
    tmp_path: Path, cosign_bin: Path, identity: str, issuer: str
):
    tarball = signed_release(
        tmp_path / "release", VERSION, identity=identity, issuer=issuer
    )
    proc, calls, verifications = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode != 0
    assert len(verifications) == 1
    assert not _loaded(calls)
    # A genuine release signed after a Sigstore rotation fails the same way, so
    # the message names the pinned root and where a current one comes from.
    assert "scripts/sigstore/trusted_root.json" in proc.stderr
    assert "https://onprem.caura.ai/bundle.tar.gz" in proc.stderr


@pytest.mark.parametrize("missing", [".sha256", ".sha256.sigstore.json"])
def test_a_missing_checksum_or_bundle_is_not_loaded(
    tmp_path: Path, cosign_bin: Path, missing: str
):
    tarball = signed_release(tmp_path / "release", VERSION)
    Path(f"{tarball}{missing}").unlink()
    proc, calls, _ = _run(tmp_path, tarball, cosign=cosign_bin)
    assert proc.returncode != 0
    assert not _loaded(calls)
    # Says where the file is published, and how to load a tarball from
    # before signed releases, which never had one.
    assert f"https://onprem.caura.ai/releases/{VERSION}/" in proc.stderr
    assert "--skip-verify" in proc.stderr


def test_without_cosign_nothing_is_loaded(tmp_path: Path):
    tarball = signed_release(tmp_path / "release", VERSION)
    proc, calls, _ = _run(tmp_path, tarball)
    assert proc.returncode != 0
    assert not _loaded(calls)
    assert "cosign" in proc.stderr


def test_a_tarball_whose_name_names_no_version_is_not_loaded(tmp_path, cosign_bin):
    tarball = signed_release(tmp_path / "release", VERSION)
    renamed = tarball.with_name("images.tar.gz")
    tarball.rename(renamed)
    proc, calls, _ = _run(tmp_path, renamed, cosign=cosign_bin)
    assert proc.returncode != 0
    assert not _loaded(calls)


def test_an_unknown_option_is_refused(tmp_path: Path, cosign_bin: Path):
    tarball = signed_release(tmp_path / "release", VERSION)
    proc, calls, _ = _run(tmp_path, tarball, "--no-verify", cosign=cosign_bin)
    assert proc.returncode != 0
    assert not _loaded(calls)


def test_skipping_verification_is_explicit_and_loud(tmp_path: Path):
    tarball = tmp_path / "release" / "images.tar.gz"
    tarball.parent.mkdir()
    tarball.write_bytes(gzip.compress(b"images", mtime=0))
    proc, calls, verifications = _run(tmp_path, tarball, "--skip-verify")
    assert proc.returncode == 0, proc.stderr
    assert verifications == []
    assert _loaded(calls)
    assert "WARNING" in proc.stderr and "--skip-verify" in proc.stderr
    docs = (REPO_ROOT / "docs/install-airgap.md").read_text(encoding="utf-8")
    assert "--skip-verify" in docs


def test_the_trusted_root_is_sigstores_public_instance():
    # Pinned here rather than fetched: offline there is nothing to fetch from,
    # and one read from the bucket would come from where the tarball came from.
    root = json.loads(
        (REPO_ROOT / "scripts/sigstore/trusted_root.json").read_text(encoding="utf-8")
    )
    assert root["mediaType"].startswith("application/vnd.dev.sigstore.trustedroot+json")
    assert "https://rekor.sigstore.dev" in [t["baseUrl"] for t in root["tlogs"]]
    assert "https://fulcio.sigstore.dev" in [
        ca["uri"] for ca in root["certificateAuthorities"]
    ]


def test_the_script_names_the_release_identity_exactly():
    # One fixed identity, completed only by the version; never a regexp.
    script = (REPO_ROOT / "airgap-load.sh").read_text(encoding="utf-8")
    assert RELEASE_IDENTITY in script
    assert OIDC_ISSUER in script
    assert "regexp" not in script
    assert "insecure-ignore-tlog" not in script
