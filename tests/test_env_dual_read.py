#!/usr/bin/env python3
"""Every name this repo reads is readable under both spellings, and blank loses.

Phase 5.3. Two properties, and they fail for different reasons:

**Completeness.** Every environment name the repo READS under the old brand has
a CAURA_* twin with the same suffix, and every CAURA_* name has an old-brand
twin. The first direction is the lane's own definition of done and keeps a later
addition from quietly re-opening it; the second catches the mistake that would
make the whole lane a no-op — a new name accepted somewhere and read nowhere,
which looks exactly like success from the outside.

**First non-empty, never first defined.** A dual-read must resolve to the first
name holding a NON-EMPTY value. Resolving to the first name that merely EXISTS
is wrong here, and wrong in the direction that fails silently: these names land
in ``install.conf`` and ``.env``, files a customer hand-edits, so a new name
present-and-blank beside a working old one is the ordinary half-migrated state
rather than an exotic one. Several consumers read empty as "skip this step" —
``_client`` sends no Authorization header, the letsencrypt overlay is not added,
the bundle-dir probe falls through to a network fetch — so resolving to "" turns
a filled-in config into a silently degraded install with nothing red anywhere.

Why the assertions here run against the SOURCE rather than a copy of the rules:
a list of expected pairs maintained beside the code is a list that goes stale
the first time someone adds a name, and it goes stale silently. Everything below
is scraped out of the tree at run time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]

OLD_PREFIX = "MEMCLAW_"  # legacy-name-ok: the prefix this test exists to pair up
NEW_PREFIX = "CAURA_"

# Either prefix followed by a SCREAMING_SNAKE suffix. Bounded on the left so a
# prefix embedded in a longer identifier is not read as a bare name.
_NAME_RE = re.compile(rf"(?<![A-Z0-9_])({OLD_PREFIX}|{NEW_PREFIX})([A-Z][A-Z0-9_]*)")

# Files scanned for names. Everything the installer actually executes, plus the
# templates it ships. Docs are deliberately out: teaching the new spelling in
# prose is item 5.4, and pulling docs in here would fail this test for the whole
# of 5.4's backlog rather than for anything 5.3 left undone.
SCANNED = (
    "install.sh",
    "upgrade.sh",
    "airgap-load.sh",
    "scripts/backup.sh",
    "scripts/restore.sh",
    "scripts/verify/smoke-onprem.sh",
    "scripts/verify/smoke-connected.sh",
    "docker-compose.yml",
    "docker-compose.airgap.yml",
    "docker-compose.embedder.yml",
    "docker-compose.embedder.airgap.yml",
    "docker-compose.tls-letsencrypt.yml",
    "tools/memclawctl/src/memclawctl/cli.py",  # legacy-name-ok: the path of the shipped CLI, which is floor
    "tools/memclawctl/src/memclawctl/support.py",  # legacy-name-ok: the path of the shipped CLI, which is floor
)

# Old-brand names this repo WRITES but never reads, so there is nothing here to
# make dual. All three are set on the app container in docker-compose.yml and
# consumed inside an image built in another repo, at a tag the customer has
# already pulled: adding the new spelling would be a writer change whose reader
# does not exist in any shipped image, and dropping the old one would break
# every existing install. They move when the app image moves, not before.
#
# A name may only sit here because nothing in this repo READS it — which the
# test below re-derives rather than trusting.
WRITE_ONLY = {
    "MEMCLAW_API_URL",  # legacy-name-ok: pinned write-only name, read by the app image
    "MEMCLAW_SITE_URL",  # legacy-name-ok: pinned write-only name, read by the app image
    "MEMCLAW_BILLING_ENABLED",  # legacy-name-ok: pinned write-only name, read by the app image
}

# Shapes that constitute READING a name rather than merely writing or naming it:
# a shell parameter expansion, a python environ lookup, or a click envvar entry.
_READ_SHAPES = (
    '${{{name}',  # a shell parameter expansion, with or without a default
    '"{name}"',  # os.environ.get("...") and a click envvar entry
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _names_in(text: str) -> set[str]:
    return {m.group(0) for m in _NAME_RE.finditer(text)}


def _suffixes(names: set[str], prefix: str) -> set[str]:
    return {n[len(prefix) :] for n in names if n.startswith(prefix)}


def _all_names() -> set[str]:
    found: set[str] = set()
    for rel in SCANNED:
        found |= _names_in(_read(rel))
    return found


def _is_read_somewhere(name: str) -> bool:
    """True when some scanned file dereferences ``name`` rather than just naming it."""
    shapes = [s.format(name=name) for s in _READ_SHAPES]
    for rel in SCANNED:
        text = _read(rel)
        if any(shape in text for shape in shapes):
            return True
    return False


def test_every_scanned_file_exists():
    """The scan list is real paths.

    A typo'd entry would silently shrink coverage, and every assertion below is
    an assertion about a set built from this list — a smaller set passes more
    easily, which is the wrong direction for a completeness check to fail in.
    """
    missing = [rel for rel in SCANNED if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"scan list names files that do not exist: {missing}"


def test_the_scan_actually_finds_names():
    """Guard against the regex or the path list silently matching nothing.

    Without this, breaking either one turns every pairing assertion below into a
    comparison of two empty sets, which passes. That is the same failure the
    ratchet guards against with its both-trees-empty check.
    """
    names = _all_names()
    assert len(names) > 30, f"expected the full name family, scraped only {names}"


def test_every_old_name_that_is_read_has_a_new_twin():
    """The lane's definition of done, re-derived from the tree on every run."""
    names = _all_names()
    unpaired = sorted(
        old
        for old in names
        if old.startswith(OLD_PREFIX)
        and old not in WRITE_ONLY
        and NEW_PREFIX + old[len(OLD_PREFIX) :] not in names
    )
    assert not unpaired, (
        "these old-brand names are read but have no CAURA_* twin — either add "
        f"the dual-read or, if nothing here reads them, WRITE_ONLY: {unpaired}"
    )


def test_every_new_name_has_an_old_twin():
    """No CAURA_* name may be a typo, and none may be minted here as a new knob.

    This is the direction that catches the failure which looks like success: a
    new name spelled one way where it is written and another where it is read is
    accepted everywhere and honoured nowhere, and nothing goes red.
    """
    names = _all_names()
    orphans = sorted(
        new
        for new in names
        if new.startswith(NEW_PREFIX)
        and OLD_PREFIX + new[len(NEW_PREFIX) :] not in names
    )
    assert not orphans, (
        f"CAURA_* names with no old-brand counterpart to alias: {orphans}"
    )


def test_write_only_exemptions_are_genuinely_never_read():
    """The exemption list stays honest.

    A name parked in WRITE_ONLY because it was write-only when this was written
    stays parked after someone adds a reader for it, and the completeness test
    above would skip exactly the name that just started needing a dual-read.
    """
    now_read = sorted(n for n in WRITE_ONLY if _is_read_somewhere(n))
    assert not now_read, (
        "these are listed as write-only but something now reads them — they need "
        f"a dual-read, and to come off the list: {now_read}"
    )


def test_both_spellings_are_written_out_in_full():
    """No name is assembled from a shared suffix at any call site.

    Building the pair from one suffix — ``"${prefix}_HOME"``, an f-string, a
    loop over suffixes — makes the old names ungreppable, and grepping for the
    old brand is how this migration is tracked. It also defeats every assertion
    above, which scrape literals.
    """
    offenders = []
    for rel in SCANNED:
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            # A prefix immediately followed by an expansion or a format slot.
            if re.search(rf"{OLD_PREFIX}(\$|\{{|%s|\{{\}})", line) or re.search(
                rf"{NEW_PREFIX}(\$|\{{|%s|\{{\}})", line
            ):
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
    assert not offenders, "names built from a suffix rather than spelled out:\n" + "\n".join(
        offenders
    )


# ── the empty-string trap, per layer ─────────────────────────────────────────
#
# Each block below drives the REAL source rather than a restatement of it: the
# shell blocks are extracted from install.sh / upgrade.sh and evaluated, the
# python defaults are re-imported under a patched environment, and the compose
# tags are resolved by compose itself. A test that re-implements the resolution
# it is checking passes whatever the shipped code does.

BLANK_NEW_BEATEN_BY_OLD = "the blank new name must lose to the filled old one"


def _run_bash(script: str, env: dict[str, str]) -> str:
    """Run ``script`` under bash with EXACTLY ``env``, and return its stdout."""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"bash exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


def _extract_block(rel: str, start: str, end: str) -> str:
    """Source lines between two anchors, exclusive of the end anchor.

    Anchors are matched as full stripped lines so a passing extraction cannot be
    a partial one; a moved anchor fails loudly here rather than silently
    shrinking the block under test.
    """
    lines = _read(rel).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == start]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == end]
    assert len(starts) == 1, f"{rel}: anchor {start!r} matched {len(starts)} lines"
    assert ends, f"{rel}: end anchor {end!r} not found"
    stop = next(i for i in ends if i > starts[0])
    return "\n".join(lines[starts[0] + 1 : stop])


def _extract_function(rel: str, name: str) -> str:
    """A shell function definition, by brace matching at column 0."""
    lines = _read(rel).splitlines()
    opener = f"{name}() {{"
    idx = [i for i, ln in enumerate(lines) if ln.strip() == opener]
    assert len(idx) == 1, f"{rel}: {name}() matched {len(idx)} definitions"
    close = next(i for i in range(idx[0] + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[idx[0] : close + 1])


# -- install.sh: the defaults block ------------------------------------------

_DEFAULTS = _extract_block(
    "install.sh",
    "# ── Defaults ────────────────────────────────────────────────────────────────",
    "# ── Helpers ─────────────────────────────────────────────────────────────────",
)


def _resolve_defaults(env: dict[str, str], var: str) -> str:
    return _run_bash(
        f'set -euo pipefail\n{_DEFAULTS}\nprintf "%s" "${var}"', {"PATH": os.environ["PATH"], **env}
    )


@pytest.mark.parametrize(
    ("var", "old", "new", "old_value", "new_value", "expected"),
    [
        # (shell var, old env name, new env name, old value, new value, expected)
        ("MEMCLAW_HOME", "MEMCLAW_HOME", "CAURA_HOME", "/srv/old", "", "/srv/old"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("MEMCLAW_HOME", "MEMCLAW_HOME", "CAURA_HOME", "/srv/old", "/srv/new", "/srv/new"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("TLS_MODE", "MEMCLAW_TLS_MODE", "CAURA_TLS_MODE", "byo", "", "byo"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("HOSTNAME", "MEMCLAW_HOSTNAME", "CAURA_HOSTNAME", "a.example", "", "a.example"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("MEMCLAW_VERSION", "MEMCLAW_VERSION", "CAURA_VERSION", "v1.2.3", "", "v1.2.3"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("MEMCLAW_VERSION", "MEMCLAW_VERSION", "CAURA_VERSION", "v1.2.3", "v9.9.9", "v9.9.9"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("ADMIN_PASSWORD", "MEMCLAW_ADMIN_PASSWORD", "CAURA_ADMIN_PASSWORD", "s3cret", "", "s3cret"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("BIND_ADDRESS", "MEMCLAW_BIND_ADDRESS", "CAURA_BIND_ADDRESS", "127.0.0.1", "", "127.0.0.1"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    ],
)
def test_install_defaults_take_the_first_non_empty_name(
    var, old, new, old_value, new_value, expected
):
    """A blank new name falls through; a filled one wins. Never the reverse."""
    got = _resolve_defaults({old: old_value, new: new_value}, var)
    assert got == expected, f"{var}: {BLANK_NEW_BEATEN_BY_OLD} (got {got!r})"


def test_install_defaults_still_apply_when_neither_name_is_set():
    """The shipped defaults are untouched by the dual-read."""
    assert _resolve_defaults({}, "MEMCLAW_HOME") == "/opt/memclaw"  # legacy-name-ok: test pins the floor install path
    assert _resolve_defaults({}, "TLS_MODE") == "self-signed"
    assert _resolve_defaults({}, "EMAIL_PROVIDER") == "log"


def test_new_name_alone_is_enough():
    """A customer who only ever sets the new spelling gets a working install."""
    assert _resolve_defaults({"CAURA_HOME": "/srv/new"}, "MEMCLAW_HOME") == "/srv/new"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _resolve_defaults({"CAURA_TLS_MODE": "byo"}, "TLS_MODE") == "byo"


# -- install.sh: the embedding auto-flip guard --------------------------------


def test_explicit_new_name_provider_is_not_overridden_by_the_openai_autoflip():
    """The auto-flip must see an explicit choice made under either spelling.

    install.sh flips EMBEDDING_PROVIDER from "local" to "openai" when an OpenAI
    key is present AND the operator did not name a provider. Testing only the
    old name reads an explicit new-name choice as "not set" and overwrites it —
    the operator asks for local embeddings and silently gets their key spent on
    OpenAI instead.
    """
    guard = [
        ln
        for ln in _read("install.sh").splitlines()
        if "EMBEDDING_PROVIDER:-}" in ln and "-z " in ln
    ]
    assert len(guard) == 1, f"expected one auto-flip guard, found {guard}"
    assert "CAURA_EMBEDDING_PROVIDER" in guard[0], (
        "the auto-flip tests only the old spelling, so an explicit "
        "CAURA_EMBEDDING_PROVIDER=local is read as unset and overridden"
    )
    script = f'set -euo pipefail\nEMBEDDING_PROVIDER=local\nOPENAI_API_KEY=sk-x\n{guard[0].strip().rstrip(" \\\\")}\n  echo FLIPPED\nfi\necho "provider=$EMBEDDING_PROVIDER"'
    # Reconstructed as a standalone `if` so the guard line itself is executed.
    script = (
        "set -euo pipefail\n"
        "EMBEDDING_PROVIDER=local\n"
        "OPENAI_API_KEY=sk-x\n"
        'if [ -n "${OPENAI_API_KEY:-}" ] \\\n'
        '   && [ "${EMBEDDING_PROVIDER}" = "local" ] \\\n'
        f"{guard[0]}\n"
        '  EMBEDDING_PROVIDER="openai"\n'
        "fi\n"
        'printf "%s" "$EMBEDDING_PROVIDER"'
    )
    env = {"PATH": os.environ["PATH"], "CAURA_EMBEDDING_PROVIDER": "local"}
    assert _run_bash(script, env) == "local", (
        "an explicit new-name provider was overridden by the OpenAI auto-flip"
    )


# -- upgrade.sh: the two .env reads -------------------------------------------

_ENV_KEY_FN = _extract_function("upgrade.sh", "_env_key")
_CURRENT_VERSION_FN = _extract_function("upgrade.sh", "current_version")


def _current_version(tmp_path: Path, env_body: str) -> str:
    home = tmp_path / "install"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_body, encoding="utf-8")
    script = (
        "set -euo pipefail\n"
        f'MEMCLAW_HOME="{home}"\n'  # legacy-name-ok: test drives the old spelling, which rule 3 keeps working
        f"{_ENV_KEY_FN}\n{_CURRENT_VERSION_FN}\n"
        "current_version"
    )
    return _run_bash(script, {"PATH": os.environ["PATH"]})


def test_upgrade_reads_the_version_an_existing_install_actually_has(tmp_path):
    """Every .env on a customer's disk today carries only the old key."""
    assert _current_version(tmp_path, "MEMCLAW_VERSION=v2.8.4\n") == "v2.8.4"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


def test_upgrade_version_ignores_a_blank_new_key(tmp_path):
    """The half-migrated .env: new key present and empty, old key filled.

    First-defined resolution returns "" here, and upgrade.sh then refuses a
    perfectly healthy install with "nothing to upgrade from".
    """
    body = "CAURA_VERSION=\nMEMCLAW_VERSION=v2.8.4\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _current_version(tmp_path, body) == "v2.8.4", BLANK_NEW_BEATEN_BY_OLD


def test_upgrade_version_is_empty_rather_than_fatal_when_neither_key_is_there(tmp_path):
    """Absent under both spellings must return empty, not abort the script.

    Incidental to the dual-read but caused by it: the old body ran a bare grep
    pipeline, and under ``set -euo pipefail`` a no-match exits 1, which
    propagates out of ``FROM_VERSION=$(current_version)`` and kills upgrade.sh
    with no message at all — never reaching the ``die`` one line below that
    exists to explain it. The rewrite carries the ``|| true`` that _GET already
    documents for exactly this, so the intended error is reachable again.

    ``_run_bash`` asserts a zero exit, so this fails loudly if the guard is lost.
    """
    assert _current_version(tmp_path, "FOO=bar\n") == ""


def test_upgrade_version_prefers_a_filled_new_key(tmp_path):
    body = "CAURA_VERSION=v3.0.0\nMEMCLAW_VERSION=v2.8.4\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _current_version(tmp_path, body) == "v3.0.0"


def test_upgrade_tls_overlay_survives_a_blank_new_key():
    """The sharpest instance: blank here silently drops the customer's TLS.

    An empty _TLS_MODE is not an error — it means "add no overlay". So reading a
    blank new key instead of the filled old one removes the Caddy sidecar on the
    next upgrade, every service still reports healthy, and TLS quietly stops
    being served.
    """
    text = _read("upgrade.sh")
    assert "_TLS_MODE_NEW=$(_GET CAURA_TLS_MODE)" in text, (
        "upgrade.sh no longer reads the new spelling of the TLS mode key"
    )
    assert '[ -n "$_TLS_MODE_NEW" ] && _TLS_MODE="$_TLS_MODE_NEW"' in text, (
        "the new TLS key is applied unconditionally — a blank value would "
        "overwrite the old key's letsencrypt and drop the sidecar"
    )
    script = (
        "set -euo pipefail\n"
        'cat > .env <<EOF\nCAURA_TLS_MODE=\nMEMCLAW_TLS_MODE=letsencrypt\nEOF\n'  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        "_GET() {\n"
        "  grep -E \"^$1=\" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\"' | tr -d \"'\" || true\n"
        "}\n"
        "_TLS_MODE=$(_GET MEMCLAW_TLS_MODE)\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        "_TLS_MODE_NEW=$(_GET CAURA_TLS_MODE)\n"
        '[ -n "$_TLS_MODE_NEW" ] && _TLS_MODE="$_TLS_MODE_NEW"\n'
        'printf "%s" "$_TLS_MODE"'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        cwd=REPO_ROOT / "tests",
        check=False,
    )
    try:
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "letsencrypt", BLANK_NEW_BEATEN_BY_OLD
    finally:
        (REPO_ROOT / "tests" / ".env").unlink(missing_ok=True)


# -- upgrade.sh: the .env write-back must match the read order ----------------
#
# The half this lane originally missed, and the review caught. Reading the new
# spelling first while writing only the old one is not a smaller version of the
# dual-read — it is a silent no-op: compose keeps resolving every image from the
# key that was left alone, nothing restarts, every health check passes because
# nothing changed, and upgrade.sh prints "Upgrade complete" over an upgrade that
# did not happen.

_REWRITE_FN = _extract_function("upgrade.sh", "_rewrite_env_key")

_BUMP = _extract_block(
    "upgrade.sh",
    "# Mutate the version in .env (in-place). Keep the file otherwise.",
    "# ── Rollback helpers (defined before any call site) ────────────────────────",
)


def _apply_version_write(tmp_path: Path, env_body: str, block: str, **vars_) -> str:
    """Run one of upgrade.sh's real .env-mutating blocks over ``env_body``."""
    home = tmp_path / "install"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_body, encoding="utf-8")
    assign = "\n".join(f'{k}="{v}"' for k, v in vars_.items())
    _run_bash(
        "set -euo pipefail\n"
        f'cd "{home}"\n'
        "warn() { :; }\n"
        f"{assign}\n{_REWRITE_FN}\n{block}",
        {"PATH": os.environ["PATH"]},
    )
    return (home / ".env").read_text(encoding="utf-8")


def _keys(body: str) -> dict:
    out = {}
    for line in body.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_the_bump_moves_both_version_keys(tmp_path):
    """A .env that carries both must come out with both at the new version."""
    body = "CAURA_VERSION=v2.8.4\nMEMCLAW_VERSION=v2.8.4\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    got = _keys(_apply_version_write(tmp_path, body, _BUMP, TO_VERSION="v2.9.0"))
    assert got["CAURA_VERSION"] == "v2.9.0", (
        "the new spelling was left behind, so compose keeps resolving the old tag "
        "while the upgrade reports success"
    )
    assert got["MEMCLAW_VERSION"] == "v2.9.0"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


def test_the_bump_does_not_introduce_the_new_key(tmp_path):
    """Every .env on a customer's disk today has only the old key; keep it that way.

    Writing the new spelling into a file that does not have it is a writer
    change, and it belongs to item 5.4 rather than to an upgrade run.
    """
    got = _keys(_apply_version_write(
        tmp_path, "MEMCLAW_VERSION=v2.8.4\n", _BUMP, TO_VERSION="v2.9.0"))  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert got["MEMCLAW_VERSION"] == "v2.9.0"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert "CAURA_VERSION" not in got, "an upgrade must not add the new spelling"


def test_the_bump_still_appends_the_old_key_when_absent(tmp_path):
    """Unchanged behaviour: a .env predating the key gets it appended."""
    got = _keys(_apply_version_write(tmp_path, "FOO=bar\n", _BUMP, TO_VERSION="v2.9.0"))
    assert got["MEMCLAW_VERSION"] == "v2.9.0"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


@pytest.mark.parametrize("fn", ["_rollback_pre_up", "_rollback"])
def test_rollback_moves_both_version_keys(tmp_path, fn):
    """The sharper direction: a half-rollback strands the stack on the bad version.

    The real function is defined and called rather than having its body spliced
    out, so what runs is what ships. ``docker`` is stubbed because neither
    rollback path should need a daemon to put .env back, and ``_rollback`` is
    called in a subshell because it ends in ``exit 4`` by design.
    """
    home = tmp_path / "install"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(
        "CAURA_VERSION=v2.9.0\nMEMCLAW_VERSION=v2.9.0\n", encoding="utf-8"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    )
    script = (
        "set -uo pipefail\n"
        f'cd "{home}"\n'
        "warn() { :; }\n"
        "docker() { return 0; }\n"
        'BACKUP_PATH=""\n'
        "COMPOSE_FILES=(-f docker-compose.yml)\n"
        'FROM_VERSION="v2.8.4"\n'
        f"{_REWRITE_FN}\n{_extract_function('upgrade.sh', fn)}\n"
        f'( {fn} "test-cause" ) || true'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"]}, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = _keys((home / ".env").read_text(encoding="utf-8"))
    assert got["CAURA_VERSION"] == "v2.8.4", f"{fn} left the new spelling on the bad version"
    assert got["MEMCLAW_VERSION"] == "v2.8.4", f"{fn} did not roll the old spelling back"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


def test_the_read_order_and_the_write_set_cannot_drift(tmp_path):
    """Round-trip: what current_version() reads back is what the bump wrote.

    The two sides are asserted against each other rather than against a literal,
    so adding a third spelling to one of them without the other fails here.
    """
    body = "CAURA_VERSION=v2.8.4\nMEMCLAW_VERSION=v2.8.4\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    home = tmp_path / "install"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(body, encoding="utf-8")
    _run_bash(
        "set -euo pipefail\n"
        f'cd "{home}"\nTO_VERSION="v2.9.0"\n{_REWRITE_FN}\n{_BUMP}',
        {"PATH": os.environ["PATH"]},
    )
    assert _current_version(tmp_path, (home / ".env").read_text()) == "v2.9.0"


# -- install.conf parsing ------------------------------------------------------


def _parse_conf(tmp_path: Path, body: str, var: str) -> str:
    """Drive install.sh's real config-file block over ``body``."""
    conf = tmp_path / "install.conf"
    conf.write_text(body, encoding="utf-8")
    block = _extract_block(
        "install.sh",
        "# ── Apply config file (lower precedence than CLI/env) ──────────────────────",
        "# Re-export in MEMCLAW_* form so sudo -E preserves them into the child.",  # legacy-name-ok: test anchors on the old spelling, which rule 3 keeps working
    )
    script = (
        "set -euo pipefail\n"
        "log() { :; }\ndie() { echo \"$1\" >&2; exit 1; }\n"
        f'CONFIG_FILE="{conf}"\n'
        f"{_DEFAULTS}\n"
        f'CONFIG_FILE="{conf}"\n'
        f"{block}\n"
        f'printf "%s" "${var}"'
    )
    return _run_bash(script, {"PATH": os.environ["PATH"]})


def test_install_conf_accepts_the_new_key_names(tmp_path):
    """The whitelist `case` must name the new keys, or they are dropped silently."""
    body = 'caura_home = "/srv/new"\ncaura_version = "v9.9.9"\n'
    assert _parse_conf(tmp_path, body, "MEMCLAW_HOME") == "/srv/new"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _parse_conf(tmp_path, body, "MEMCLAW_VERSION") == "v9.9.9"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


def test_install_conf_old_keys_keep_working(tmp_path):
    body = 'memclaw_home = "/srv/old"\nmemclaw_version = "v1.0.0"\n'  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _parse_conf(tmp_path, body, "MEMCLAW_HOME") == "/srv/old"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _parse_conf(tmp_path, body, "MEMCLAW_VERSION") == "v1.0.0"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working


@pytest.mark.parametrize("order", ["new_first", "old_first"])
def test_install_conf_blank_new_key_never_clobbers_a_filled_old_one(tmp_path, order):
    """Order-independence is the point of resolving after the loop.

    A blank key assigned as it is seen makes the answer depend on which spelling
    sits lower in the file — so the same two lines swapped would blank the value.
    """
    old = 'memclaw_home = "/srv/old"'  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    new = "caura_home ="
    body = f"{new}\n{old}\n" if order == "new_first" else f"{old}\n{new}\n"
    assert _parse_conf(tmp_path, body, "MEMCLAW_HOME") == "/srv/old", (  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        f"{BLANK_NEW_BEATEN_BY_OLD} ({order})"
    )


def test_install_conf_blank_old_key_does_not_blank_the_install_root(tmp_path):
    """A half-filled template must not install into "".

    The shipped install.conf.example ships three keys with empty values, so
    "present and blank" is not hypothetical here — it is what the template does.
    """
    body = "memclaw_home =\n"  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert _parse_conf(tmp_path, body, "MEMCLAW_HOME") == "/opt/memclaw"  # legacy-name-ok: test pins the floor install path


def test_both_config_spellings_have_identical_precedence(tmp_path):
    """The two spellings of one key must be interchangeable in every respect.

    Not just "which value wins between them" — which the order-independence test
    above covers — but how each behaves against the layers around it. If the new
    key were given a different precedence from the old one, an operator moving a
    line from one spelling to the other would silently change when it applies.

    NOTE ON WHAT THIS DOES **NOT** BLESS. install.sh documents "CLI flags > env
    vars > --config file > defaults", and for these two keys the config file
    actually wins over both. That inversion is PRE-EXISTING — the old key
    assigned unconditionally before this change and still resolves the same way
    after it — and it is shared with seven other unguarded keys in the same
    block. Fixing it is a real change: the emptiness guard the other keys use
    cannot work here, because these two have NON-EMPTY defaults,
    so that guard disables the key outright instead of deferring to the flag.
    A correct fix needs provenance tracking through the flag parser, and it
    should move all nine keys together rather than two of them.

    So this asserts the property this lane owes — the spellings match — and
    deliberately does not pin the inversion as correct. Whoever fixes precedence
    changes both branches here together, which is exactly the coupling worth
    keeping.
    """
    for old_key, new_key, var in (
        ("memclaw_home", "caura_home", "MEMCLAW_HOME"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ("memclaw_version", "caura_version", "MEMCLAW_VERSION"),  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        ):
        with_old = _parse_conf(tmp_path, f'{old_key} = "/from-conf"\n', var)
        with_new = _parse_conf(tmp_path, f'{new_key} = "/from-conf"\n', var)
        assert with_old == with_new == "/from-conf", (
            f"{old_key} and {new_key} do not resolve alike: "
            f"{with_old!r} vs {with_new!r}"
        )
        # And neither spelling may be silently inert — the failure mode of the
        # naive precedence "fix", which leaves the default in place.
        assert with_new != "/opt/memclaw", (  # legacy-name-ok: test pins the floor install path
            f"{new_key} was dropped and the default survived"
        )


# -- the operator CLI (python) -------------------------------------------------------


def _import_cli_defaults(env: dict[str, str]) -> dict[str, str]:
    """Re-import the CLI module under ``env`` and report its resolved defaults.

    A subprocess rather than monkeypatch + importlib.reload: these are
    module-level constants, so the value under test is decided at import time
    and a reload inside this process would leave a half-initialised module
    behind for every later test.
    """
    src = REPO_ROOT / "tools" / "memclawctl" / "src"  # legacy-name-ok: the path of the shipped CLI, which is floor
    code = (
        "import json\n"
        "from memclawctl import cli, support\n"  # legacy-name-ok: the path of the shipped CLI, which is floor
        "print(json.dumps({'url': cli.DEFAULT_URL, 'admin_key': cli.DEFAULT_ADMIN_KEY,"
        " 'home': str(cli.DEFAULT_HOME), 'support_home': str(support.DEFAULT_HOME),"
        " 'endpoint': support.DEFAULT_SUPPORT_ENDPOINT}))"
    )
    proc = subprocess.run(
        ["python3", "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(src), **env},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_operator_cli_defaults_ignore_blank_new_names():
    """``or`` semantics, asserted through a real import rather than by reading it."""
    got = _import_cli_defaults(
        {
            "CAURA_URL": "",
            "MEMCLAW_URL": "https://old.example",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
            "CAURA_ADMIN_KEY": "",
            "MEMCLAW_ADMIN_KEY": "jwt-old",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
            "CAURA_HOME": "",
            "MEMCLAW_HOME": "/srv/old",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
            "CAURA_SUPPORT_URL": "",
            "MEMCLAW_SUPPORT_URL": "https://old.support",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        }
    )
    assert got["url"] == "https://old.example", BLANK_NEW_BEATEN_BY_OLD
    # The one with teeth: _client sends no Authorization header when this is "".
    assert got["admin_key"] == "jwt-old", BLANK_NEW_BEATEN_BY_OLD
    assert got["home"] == "/srv/old", BLANK_NEW_BEATEN_BY_OLD
    assert got["support_home"] == "/srv/old", BLANK_NEW_BEATEN_BY_OLD
    assert got["endpoint"] == "https://old.support", BLANK_NEW_BEATEN_BY_OLD


def test_operator_cli_defaults_prefer_a_filled_new_name():
    got = _import_cli_defaults(
        {
            "CAURA_URL": "https://new.example",
            "MEMCLAW_URL": "https://old.example",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
            "CAURA_HOME": "/srv/new",
            "MEMCLAW_HOME": "/srv/old",  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        }
    )
    assert got["url"] == "https://new.example"
    assert got["home"] == "/srv/new"
    assert got["support_home"] == "/srv/new"


def test_operator_cli_defaults_unchanged_when_neither_name_is_set():
    got = _import_cli_defaults({})
    assert got["url"] == "http://localhost"
    assert got["admin_key"] == ""
    assert got["home"] == "/opt/memclaw"  # legacy-name-ok: test pins the floor install path
    assert got["endpoint"] == "https://support.caura.ai/api/onprem/support"


def test_click_envvar_list_resolves_to_the_first_non_empty_name():
    """Pins a click behaviour this repo depends on but does not implement.

    ``--api-key`` is declared with a LIST of envvars, and the whole dual-read
    there rests on click skipping an empty one rather than taking the first that
    exists. pyproject allows any click 8.x, so this is the check that a minor
    upgrade cannot quietly turn the option into first-defined.

    The probe reproduces the REAL declaration, ``required=True`` included,
    because that is the part a simplified stand-in would not exercise: with
    both names blank the option must raise the missing-option error rather than
    hand an empty string to the API as a credential.

    On the recurring claim that click checks ``rv is not None`` here: it does,
    but in ``Option.resolve_envvar_value``, applied to the RESULT of
    ``Parameter.resolve_envvar_value``'s search — and that search is
    ``for envvar in self.envvar: rv = os.environ.get(envvar); if rv: return rv``,
    which skips a blank and returns None if every name is blank. The
    definedness test never sees an individual variable. Verified by source and
    by behaviour on 8.1.0, 8.1.3, 8.1.8, 8.2.1 and 8.3.1.
    """
    import click
    from click.testing import CliRunner

    @click.command()
    @click.option(
        "--api-key",
        envvar=["CAURA_API_KEY", "MEMCLAW_API_KEY"],  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        required=True,
    )
    def probe(api_key):
        click.echo(api_key)

    runner = CliRunner()

    def run(**env):
        return runner.invoke(
            probe, [], env={"CAURA_API_KEY": None, "MEMCLAW_API_KEY": None, **env}  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
        )

    blank_new = run(CAURA_API_KEY="", MEMCLAW_API_KEY="mc_old")  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert blank_new.output.strip() == "mc_old", BLANK_NEW_BEATEN_BY_OLD

    filled = run(CAURA_API_KEY="mc_new", MEMCLAW_API_KEY="mc_old")  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert filled.output.strip() == "mc_new"

    old_only = run(MEMCLAW_API_KEY="mc_old")  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert old_only.output.strip() == "mc_old", "an existing environment stopped working"

    assert run(CAURA_API_KEY="mc_new").output.strip() == "mc_new"

    # Both blank must REFUSE, not resolve to "". An empty per-tenant key would
    # otherwise be sent as a credential instead of the option erroring.
    both_blank = run(CAURA_API_KEY="", MEMCLAW_API_KEY="")  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert both_blank.exit_code != 0 and "Missing option" in both_blank.output, (
        f"a blank pair resolved instead of erroring: {both_blank.output!r}"
    )


def test_the_api_key_option_actually_uses_a_list():
    """The pin above is worth nothing if the option went back to a bare string.

    Introspected out of a subprocess for the same reason the defaults are: the
    package lives under tools/ and is not importable from this directory, and
    putting it on sys.path here would leak into the two gate test files that
    share this runner.
    """
    src = REPO_ROOT / "tools" / "memclawctl" / "src"  # legacy-name-ok: the path of the shipped CLI, which is floor
    code = (
        "import json\n"
        "from memclawctl import cli\n"  # legacy-name-ok: the path of the shipped CLI, which is floor
        "out = {}\n"
        "for name, cmd in cli.memory_group.commands.items():\n"
        "    for p in cmd.params:\n"
        "        if getattr(p, 'name', None) == 'api_key':\n"
        "            out[name] = p.envvar\n"
        "print(json.dumps(out))"
    )
    proc = subprocess.run(
        ["python3", "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(src)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    found = json.loads(proc.stdout)
    assert set(found) == {"export", "import"}, f"unexpected commands: {sorted(found)}"
    for cmd, envvar in found.items():
        assert isinstance(envvar, list), f"{cmd}: envvar is {envvar!r}, not a list"
        assert len(envvar) == 2, f"{cmd}: expected exactly the pair, got {envvar}"
        assert envvar[0].startswith(NEW_PREFIX), f"{cmd}: new name is not read first"
        assert envvar[1].startswith(OLD_PREFIX), f"{cmd}: old name is no longer read"
        assert envvar[0][len(NEW_PREFIX) :] == envvar[1][len(OLD_PREFIX) :], (
            f"{cmd}: the pair does not share a suffix: {envvar}"
        )


# -- docker compose ------------------------------------------------------------

_COMPOSE_FILES = [f for f in SCANNED if f.startswith("docker-compose.")]


def test_every_compose_version_interpolation_is_first_non_empty():
    """Compose's ``:-`` treats blank as absent; ``-`` does not.

    Structural rather than behavioural so it runs everywhere, and paired with
    the compose-resolved test below, which needs a docker binary.
    """
    bad = []
    checked = 0
    for rel in _COMPOSE_FILES:
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            # Interpolations only. A comment mentioning a name is prose, and
            # flagging it would make this test fail on documentation rather than
            # on a resolution rule — which is item 5.4's job, not this one's.
            if "${" not in line:
                continue
            for m in re.finditer(r"\$\{(CAURA_[A-Z0-9_]+)(:?-)", line):
                checked += 1
                if m.group(2) != ":-":
                    bad.append(
                        f"{rel}:{lineno}: {m.group(1)}{m.group(2)} takes the first "
                        "DEFINED name; blank must fall through, so it needs ':-'"
                    )
            for m in re.finditer(r"\$\{(MEMCLAW_[A-Z0-9_]+)", line):  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
                twin = NEW_PREFIX + m.group(1)[len(OLD_PREFIX) :]
                if twin not in line:
                    bad.append(f"{rel}:{lineno}: {m.group(1)} interpolated without {twin}")
    assert not bad, "\n".join(bad)
    assert checked >= 20, f"expected the whole image-tag family, checked {checked}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_compose_resolves_image_tags_from_either_spelling():
    """Resolved by compose itself — the interpolation is its semantics, not ours."""
    base = {
        "PATH": os.environ["PATH"],
        "POSTGRES_PASSWORD": "x",
        "JWT_SECRET": "y" * 40,
        "CORE_ADMIN_API_KEY": "z",
        "PLATFORM_OPERATIONS_INTERNAL_TOKEN": "t",
        "SETTINGS_ENCRYPTION_KEY": "k",
        "PUBLIC_HOSTNAME": "h.example",
    }

    def tag_of(service: str, env: dict[str, str]) -> str:
        proc = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
            capture_output=True,
            text=True,
            env={**base, **env},
            cwd=REPO_ROOT,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"docker compose unavailable here: {proc.stderr.strip()[:200]}")
        return json.loads(proc.stdout)["services"][service]["image"].rsplit(":", 1)[1]

    old_only = {"MEMCLAW_VERSION": "v2.8.4"}  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert tag_of("core-api", old_only) == "v2.8.4", "an existing .env stopped working"

    blank_new = {"CAURA_VERSION": "", **old_only}
    assert tag_of("core-api", blank_new) == "v2.8.4", BLANK_NEW_BEATEN_BY_OLD

    assert tag_of("core-api", {"CAURA_VERSION": "v3.0.0"}) == "v3.0.0"

    # The ops tag falls back through four names; the blank ones must all lose.
    ops = {"CAURA_OPS_VERSION": "", "MEMCLAW_OPS_VERSION": "v2.9.0", **old_only}  # legacy-name-ok: test pins the old spelling, which rule 3 keeps working
    assert tag_of("platform-operations", ops) == "v2.9.0", BLANK_NEW_BEATEN_BY_OLD
