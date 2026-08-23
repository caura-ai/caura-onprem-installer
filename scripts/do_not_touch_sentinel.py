#!/usr/bin/env python3
"""Fail a PR that removes a string something outside this repo still depends on.

Hard rule 4 of the sunset plan: the do-not-touch list becomes CI. Half of it
shipped as ``scripts/legacy_name_ratchet.py``, which is *directional* — it fails a
file whose old-brand count goes **up**. A sweep that **deletes** a load-bearing
string makes that count go **down**, so the ratchet reads it as progress and
passes it. This is the other half: the same list, asserted in the other
direction.

The two gates are not redundant and neither subsumes the other. The ratchet
answers "did this change mint a new old-brand name?"; this one answers "did this
change remove a name something already depends on?" A rename wave trips the
first, a prose sweep trips the second, and Phase 7 is a prose sweep.

**Why this repo needs it more than the OSS one does.** Two reasons, neither
about tidiness:

*The failure lands where we cannot reach it.* In OSS a broken floor string is a
bad deploy, caught by us, fixed by us. Here it is a customer's stack that will
not start, on hardware we do not administer, discovered by them. For the
air-gapped names there is not even a network to fix it over.

*For the cross-repo contracts this list is the only place the coupling is
written down at all.* ``LICENSE_FILE`` is consumed by images built in another
repo; the bundle manifest's collector field is parsed by the support backend.
Nothing in this repo fails when either is renamed, and nobody on the other side
is grepping for it. There is no test that can catch those, here or there — only a list.

The riskiest entries are the Postgres defaults, and the risk is *partial*
application rather than total. Nothing else names the database — ``install.sh``
writes both keys to ``.env`` blank, so the shell default in the compose files is
what every container resolves — and ``backup.sh``, ``restore.sh`` and ``upgrade.sh``
each repeat it independently. Flip the compose files and miss ``restore.sh`` and
the restore silently targets a database that does not exist, discovered during
an incident. They are pinned per file for exactly that reason.

**On pinning expressions rather than names.** Several entries pin a surrounding
expression — the whole ``pg_isready`` invocation rather than the bare default,
the ``image:`` key with its value rather than the registry prefix. That is not
verbosity. The bare forms also appear in comments and prose here, so an entry
pinned that way passes a tree where the functional line is gone and only the
commentary survives — the same false pass this gate exists to prevent, inverted.
It matters most in YAML, which has no comment/code distinction a checker can
lean on and where the healthcheck value lives inside a ``CMD-SHELL`` list. The
accompanying test refuses any entry whose text appears on a comment-only line.

**The LOG_MESSAGE kind is unused here today.** It is retained rather than
stripped so this file stays diffable against the OSS copy, whose mechanism is
identical and where the kind guards three phrases a production monitor matches
on. The class can arise here — the support backend parses what these tools emit
— and when it does the machinery is already present and tested.

Usage::

    scripts/do_not_touch_sentinel.py            # fail on any missing string
    scripts/do_not_touch_sentinel.py --list     # print the list and exit 0
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A plain substring of the file's text. Right for a name that is a contract:
# whoever depends on it depends on the characters, not on where they sit.
LITERAL = "literal"

# A substring of a string literal passed to a logging call, checked through the
# AST rather than the file text. Unused in this repo's list today — see the
# module docstring — and kept because the mechanism is shared with the OSS copy,
# where the embedding service discusses its alert phrases in its own comments as
# well as emitting them: there, a text search passes on a tree where the
# ``logger.error`` call is gone and only the prose survives, which is precisely
# the tree that kills the alert. Only an emitted message can match a log filter.
LOG_MESSAGE = "log_message"

# Severity order, because a LOG_MESSAGE entry pins a floor as well as a phrase.
# The Datadog filter does not select on severity, which is exactly why the level
# has to be pinned here: what the level decides is whether the line is emitted at
# all. Downgrade a ``logger.error`` to ``logger.debug`` and the phrase is intact,
# the filter would still match it, and it never reaches Cloud Logging to be
# matched. Raising a level is harmless, so this is a minimum rather than a set.
_LEVELS = ("debug", "info", "warning", "error", "critical")
_LEVEL_RANK = {name: i for i, name in enumerate(_LEVELS)}
# ``warn`` is the deprecated alias for ``warning``; ``exception`` is ``error``
# with a traceback attached, and logs at ERROR.
_LEVEL_RANK["warn"] = _LEVEL_RANK["warning"]
_LEVEL_RANK["exception"] = _LEVEL_RANK["error"]

# ``logger.log(level, msg)`` names its level in an argument rather than in the
# method, and a call of that shape can pick it with a conditional — the OSS tree
# has one that does. There is no static answer for that, so it counts as an
# emitter of the phrase but can never satisfy a level floor: unverifiable is
# reported, not assumed. A message something outside the repo depends on should
# be emitted through an explicit level method anyway, and this is what says so.
_LOG_METHODS = frozenset(_LEVEL_RANK) | {"log"}


@dataclass(frozen=True)
class Sentinel:
    path: str
    text: str
    kind: str
    breaks: str
    # LOG_MESSAGE only: the least severe level that still reaches the monitor.
    min_level: str | None = None


# ── the list ─────────────────────────────────────────────────────────────────
#
# Every entry carries ``legacy-name-ok`` because this file is itself scanned by
# the ratchet, and the reason is the same one every time: the line exists to pin
# a string rule 3 keeps readable forever. Excluding the file from the ratchet
# instead would leave a hole in that scan, which is the trade its author already
# refused once for the same reason.

SENTINELS: tuple[Sentinel, ...] = (
    # -- The Postgres defaults. The highest-blast-radius strings in the repo. ---
    #
    # Nothing else names the database. install.sh writes POSTGRES_DB and
    # POSTGRES_USER to .env BLANK (:529-530, matching .env.example:27-28), and
    # ``:-`` treats blank as unset, so the literal below is what every container
    # and every day-2 script actually resolves. Phase 3's ALTER DATABASE never
    # reaches these machines because we do not administer them, which is why
    # rule 3 makes the defaults permanent and item 3.5 keeps 3.4 away from here.
    #
    # A PARTIAL flip is worse than a total one, which is why these are pinned per
    # file rather than once. backup.sh, restore.sh and upgrade.sh each repeat the
    # default independently: flip the compose files and miss restore.sh and the
    # restore silently targets a database that does not exist — discovered during
    # an incident, on hardware we cannot reach.
    Sentinel(
        path="docker-compose.yml",
        text='POSTGRES_DB: "${POSTGRES_DB:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the database the stack creates and connects to stops matching existing installs",
    ),
    Sentinel(
        path="docker-compose.yml",
        text='POSTGRES_USER: "${POSTGRES_USER:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the role the stack creates stops matching the one existing data is owned by",
    ),
    Sentinel(
        path="docker-compose.yml",
        # Pinned with the command it sits inside, not as a bare default. In YAML
        # there is no comment/code distinction to lean on and this value lives in
        # a "test: [CMD-SHELL, ...]" list, so the surrounding command is what
        # makes the entry unsatisfiable by anything but the healthcheck itself.
        text="pg_isready -U ${POSTGRES_USER:-memclaw}",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="postgres never reports healthy, so every dependent service hangs on startup",
    ),
    Sentinel(
        path="docker-compose.yml",
        text="postgresql+asyncpg://${POSTGRES_USER:-memclaw}",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="both DATABASE_URLs point at a role that does not exist on an existing install",
    ),
    Sentinel(
        path="docker-compose.yml",
        text='ALLOYDB_DATABASE: "${POSTGRES_DB:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the platform services address a database name no existing install has",
    ),
    Sentinel(
        path="scripts/backup.sh",
        text='pg_dump -U "${POSTGRES_USER:-memclaw}" -Fc "${POSTGRES_DB:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="nightly backups silently dump the wrong database, or nothing at all",
    ),
    Sentinel(
        path="scripts/restore.sh",
        text='pg_restore --clean --if-exists -U "${POSTGRES_USER:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="a restore targets a database that does not exist — during an incident",
    ),
    Sentinel(
        path="upgrade.sh",
        text='pg_dump -Fc -U "${POSTGRES_USER:-memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the pre-upgrade safety dump captures the wrong database or fails",
    ),
    # -- The install root. A script default with nothing on disk pinning it. ---
    #
    # The install root is never written into .env — it is a default repeated in
    # SEVEN places and nothing else. So an existing install has nothing recording
    # where it lives: flip the default and every day-2 tool looks in an empty
    # directory. backup.sh backs up nothing and reports success; status reports a
    # stack that is running fine as absent.
    #
    # Five are shell scripts and two are the CLI's own Python entry points, which
    # is the pairing most likely to be half-done: the shell copies are adjacent
    # and get found together, while cli.py and support.py sit in another tree and
    # drive backup/restore/upgrade/rollback whenever the operator has not exported
    # the variable. Same partial-application shape as the Postgres defaults.
    Sentinel(
        path="install.sh",
        text='MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="a re-run installs a second copy beside the customer's existing stack",
    ),
    Sentinel(
        path="upgrade.sh",
        text='MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="upgrade cannot find the install it is upgrading",
    ),
    Sentinel(
        path="scripts/backup.sh",
        text='MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="backups run against an empty directory and report success",
    ),
    Sentinel(
        path="scripts/restore.sh",
        text='MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="a restore unpacks into the wrong directory and the stack never sees it",
    ),
    Sentinel(
        path="scripts/verify/smoke-onprem.sh",
        text='MEMCLAW_HOME="${MEMCLAW_HOME:-/opt/memclaw}"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the smoke check reports a healthy install as missing",
    ),
    Sentinel(
        path="tools/memclawctl/src/memclawctl/cli.py",  # legacy-name-ok: the path of the pinned file
        text='os.environ.get("MEMCLAW_HOME", "/opt/memclaw")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the operator CLI's backup, restore, upgrade and rollback use the wrong root",
    ),
    Sentinel(
        path="tools/memclawctl/src/memclawctl/support.py",  # legacy-name-ok: the path of the pinned file
        text='os.environ.get("MEMCLAW_HOME", "/opt/memclaw")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="a support bundle is collected from the wrong install root, or comes back empty",
    ),
    # -- Air-gapped image names, until the local docker-tag alias exists. ------
    #
    # These are the names baked into a tarball the customer already holds, on a
    # machine with no network to fetch a corrected one. installer#9 §C shows the
    # rename is solvable — docker tag after docker load is a same-digest alias
    # that works offline — but until that lands, renaming the compose reference
    # strands the next upgrade with no recovery path. Pinned as image: key and
    # value together: the prefix alone also appears in four comments.
    Sentinel(
        path="docker-compose.airgap.yml",
        text='image: "memclaw-onprem/platform-storage:',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="an air-gapped upgrade cannot resolve an image the tarball already holds",
    ),
    Sentinel(
        path="docker-compose.embedder.airgap.yml",
        text='image: "memclaw-onprem/core-api-embedder:',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the offline embedder overlay stops matching the loaded image",
    ),
    Sentinel(
        path="install.sh",
        text='docker image inspect "memclaw-onprem/core-api-embedder:',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the --offline preflight passes an install whose embedder was never loaded",
    ),
    # -- Cross-repo data contracts. Nothing in THIS repo fails when they go. ---
    Sentinel(
        path="tools/memclawctl/src/memclawctl/support.py",  # legacy-name-ok: the path of the pinned file
        text='"collector": "memclawctl"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the support backend stops recognising every bundle already in flight",
    ),
    Sentinel(
        path="tools/memclawctl/src/memclawctl/support.py",  # legacy-name-ok: the path of the pinned file
        text='version("caura-memclawctl")',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="bundle building raises PackageNotFoundError if the distribution is renamed alone",
    ),
    Sentinel(
        path="tools/memclawctl/pyproject.toml",  # legacy-name-ok: the path of the pinned file
        text='name = "caura-memclawctl"',  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the distribution name and the runtime version() lookup stop agreeing",
    ),
    Sentinel(
        path="docker-compose.yml",
        text="LICENSE_FILE: /etc/memclaw/license.key",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="images built in other repos look for a licence at a path nothing mounts",
    ),
    Sentinel(
        path="docker-compose.yml",
        text="- ./license:/etc/memclaw",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="the licence bind mount stops landing where the platform images read it",
    ),
    # -- Rollback state written on customer disk. ------------------------------
    Sentinel(
        path="upgrade.sh",
        text="> .memclaw-prev-version",  # legacy-name-ok: pinned floor string
        kind=LITERAL,
        breaks="rollback cannot find the version marker, so a bad upgrade cannot be undone",
    ),
)


def _static_text(node: ast.expr) -> str | None:
    """The compile-time-known text of a string argument, if it has any.

    Adjacent literals are concatenated by the parser before we see them, so a
    message split across source lines — which all three of ours are — arrives as
    one string and matches as one string.

    An f-string arrives as a ``JoinedStr`` instead, and its literal segments are
    still emitted verbatim. Joining them keeps a future f-string conversion from
    reading as "the phrase is gone" — a false failure is the safe direction, but
    one that names the wrong cause costs somebody an afternoon mid-sweep.
    Interpolations are dropped, so a phrase broken up by one does not match,
    which is right: it is no longer emitted whole and would no longer be found
    by a substring filter either.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _log_calls(source: str, path: str) -> list[tuple[str | None, str]]:
    """``(level, message)`` for every logging call — level ``None`` if unknowable.

    Positional args only, and every one of them: ``logger.log`` takes the level
    first, so keying on argument position would miss the message while keying on
    "any string argument" costs nothing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # a file this gate cannot read is a failed gate
        raise RuntimeError(f"{path} does not parse: {exc}") from exc

    out: list[tuple[str | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            not isinstance(node.func, ast.Attribute)
            or node.func.attr not in _LOG_METHODS
        ):
            continue
        level = node.func.attr if node.func.attr in _LEVEL_RANK else None
        for arg in node.args:
            text = _static_text(arg)
            if text:
                out.append((level, text))
    return out


def _check(sentinel: Sentinel, root: Path) -> str | None:
    """``None`` if the string survives, else why it did not."""
    target = root / sentinel.path
    if not target.is_file():
        # Not "skip": a path that stopped existing is the loudest possible
        # version of the thing this gate exists to catch, and a gate that skips
        # what it cannot find passes every PR once the file moves.
        return "the file no longer exists"

    source = target.read_text(encoding="utf-8", errors="replace")

    if sentinel.kind == LITERAL:
        return None if sentinel.text in source else "the string is gone"

    emitting = [
        (level, text)
        for level, text in _log_calls(source, sentinel.path)
        if sentinel.text in text
    ]
    if not emitting:
        if sentinel.text in source:
            return "it survives only in prose — no logging call emits it any more"
        return "the string is gone"

    if not sentinel.min_level:
        return None

    required = _LEVEL_RANK[sentinel.min_level]
    if any(level and _LEVEL_RANK[level] >= required for level, _ in emitting):
        return None

    known = sorted({level for level, _ in emitting if level})
    if not known:
        return (
            "it is emitted through logger.log, whose level is an argument — use an "
            f"explicit .{sentinel.min_level}() so the level can be checked"
        )
    return (
        f"it is emitted at {', '.join(known)}, below {sentinel.min_level} — the phrase "
        "is intact but the line no longer reaches the sink the monitor reads"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert that load-bearing strings survive this change."
    )
    parser.add_argument("--list", action="store_true", help="print the list and exit 0")
    parser.add_argument(
        "--root", default=str(REPO_ROOT), help="repository root to check"
    )
    args = parser.parse_args()

    if args.list:
        print(f"{len(SENTINELS)} protected strings:")
        for s in SENTINELS:
            print(f"  {s.path}\n      {s.text!r} ({s.kind}) — {s.breaks}")
        return 0

    if not SENTINELS:
        # An empty list is a gate that passes everything while looking green.
        print("The sentinel list is empty, so this gate is checking nothing.")
        return 1

    root = Path(args.root).resolve()
    failures = [(s, why) for s in SENTINELS if (why := _check(s, root)) is not None]

    if not failures:
        print(f"All {len(SENTINELS)} protected strings survive.")
        return 0

    print(f"This change removes {len(failures)} string(s) that something depends on.\n")
    for s, why in failures:
        print(f"  {s.path}")
        print(f"      {s.text!r} — {why}")
        print(f"      breaks: {s.breaks}\n")
    print(
        "Rule 4 of the sunset plan: the do-not-touch list is CI. These strings are on\n"
        "the floor by design — a contract, an immutable migration, or prose a production\n"
        "monitor matches on — so nothing in the tree fails when they go.\n\n"
        "Restore the string. If it genuinely must change, the dependant has to move\n"
        "first and in its own repo, and this list has to change in the same PR as the\n"
        "code — never after it, and never instead of it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
