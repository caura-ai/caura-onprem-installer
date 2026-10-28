"""Prepare only the mounted proof file as the auth image's appuser (not .env)."""

from __future__ import annotations

import os
import pwd
import stat
import sys
from pathlib import Path


def prepare_token(path: Path, uid: int, gid: int) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("not a single regular proof file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(259)
        proof = raw.decode("ascii").rstrip("\r\n")
        if (
            len(raw) > 258
            or not 32 <= len(proof) <= 256
            or not all(33 <= ord(char) <= 126 for char in proof)
        ):
            raise ValueError("invalid proof")
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        return proof
    finally:
        os.close(fd)


def main() -> int:
    try:
        user = pwd.getpwnam("appuser")
        proof = prepare_token(Path("/run/caura/setup-token"), user.pw_uid, user.pw_gid)
    except (OSError, ValueError, KeyError, UnicodeError):
        print(
            "Cannot prepare host proof: use a regular private file containing 32–256 printable non-space ASCII characters.",
            file=sys.stderr,
        )
        return 2
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(proof)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
