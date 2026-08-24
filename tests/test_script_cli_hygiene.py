#!/usr/bin/env python3
"""The shipped scripts' own command-line surface: --help and error messages.

Two defects that had been in `install.sh` and `upgrade.sh` since they were
written, both visible to every operator and neither caught by anything:

  * ``die()`` printed ``"$*"``, so the exit code each call site passes as ``$2``
    was joined into the message — ``ERROR Unknown flag: --bogus 2``.
  * ``--help`` ran its comment-stripping ``sed`` over line 1, so the shebang
    came out as ``!/usr/bin/env bash`` above the real usage text.

Neither can break an install, which is exactly why they survived: nothing fails,
so nothing reports them. They are pinned here rather than left to a reader,
because the next person to copy one of these scripts copies the defect too —
which is how they reached ``scripts/set-version.sh``.

``warn()`` deliberately keeps ``"$*"``: it takes no exit code, and one call in
install.sh passes three separate message arguments that must be joined.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every shipped script with a --help and a die(). smoke-connected.sh has
# neither; the verify scripts are not operator entry points.
ENTRY_POINTS = ("install.sh", "upgrade.sh", "scripts/set-version.sh")

# Only the ones that actually define warn(), derived rather than listed.
# set-version.sh has a die() but no warn(), so putting it in the list below
# would add a parametrised case whose loop body never runs — green, and
# checking nothing. Deriving the list also means a script that grows a warn()
# is covered without anyone remembering to add it.
WARN_DEFINERS = tuple(
    rel
    for rel in ENTRY_POINTS
    if re.search(r"^\s*warn\(\)", (REPO_ROOT / rel).read_text(encoding="utf-8"), re.M)
)


def _run(rel: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO_ROOT / rel), *args],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.mark.parametrize("rel", ENTRY_POINTS)
def test_help_does_not_print_the_shebang(rel):
    """`sed 's/^# //'` over line 1 leaves `!/usr/bin/env bash`."""
    proc = _run(rel, "--help")
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines()]
    assert lines, "--help printed nothing"
    assert "usr/bin/env" not in lines[0], f"{rel} --help starts with the shebang: {lines[0]!r}"
    assert lines[0].strip(), f"{rel} --help starts with a blank line"


@pytest.mark.parametrize("rel", ENTRY_POINTS)
def test_help_still_prints_the_usage_block(rel):
    """The fix trims line 1 — it must not have trimmed the content too.

    Without this, deleting the whole help text would pass the test above.
    """
    out = _run(rel, "--help").stdout
    assert len(out.splitlines()) >= 3, f"{rel} --help is suspiciously short:\n{out}"
    assert "sage" in out or "usage" in out.lower(), (
        f"{rel} --help no longer shows a usage block:\n{out}"
    )


@pytest.mark.parametrize("rel", ENTRY_POINTS)
def test_die_does_not_append_the_exit_code_to_the_message(rel):
    """Every call site passes the code as $2; "$*" printed it as text."""
    proc = _run(rel, "--a-flag-that-does-not-exist")
    assert proc.returncode != 0, "an unknown flag was accepted"
    msg = proc.stderr.strip().splitlines()[-1]
    assert "a-flag-that-does-not-exist" in msg, f"unexpected error text: {msg!r}"
    assert not re.search(r"\s\d+\s*$", msg), (
        f"{rel} leaked the exit code into the message: {msg!r}"
    )


def test_the_warn_definer_list_is_not_empty():
    """Derived lists can derive to nothing; then the test below checks nobody."""
    assert WARN_DEFINERS, (
        "no script defines warn() any more — either that is wrong, or the "
        "assertion below has quietly stopped protecting anything"
    )


@pytest.mark.parametrize("rel", WARN_DEFINERS)
def test_warn_keeps_joining_all_its_arguments(rel):
    """The counterpart, and the reason die() could not just be swept.

    ``warn()`` takes no exit code and install.sh calls it with three separate
    message arguments, so it must keep ``"$*"``. Changing both together in a
    sweep would silently drop two thirds of that message.
    """
    body = (REPO_ROOT / rel).read_text(encoding="utf-8")
    for line in body.splitlines():
        if re.match(r"^\s*warn\(\)", line):
            assert '"$*"' in line, (
                f"{rel}: warn() no longer joins its arguments — a multi-argument "
                f"call would lose all but the first: {line.strip()}"
            )


def test_the_multi_argument_warn_call_still_exists():
    """Guards the guard: the test above is vacuous if nothing calls warn that way."""
    body = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert re.search(r'warn "[^"]*"\s*\\\n\s*"', body), (
        "no multi-argument warn() call left in install.sh — if that is "
        "deliberate, the warn() assertion above no longer protects anything"
    )
