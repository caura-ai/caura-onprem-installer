"""The silent install preserves credentials as data in its setup request.

Only the request serializer is evaluated. No Docker, networking, configuration
loading or installation is performed by these tests.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]
INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def _serializer() -> str:
    source = INSTALLER.read_text()
    start = source.index("json_string() {")
    end = source.index("# ── Parse CLI flags", start)
    return source[start:end]


@pytest.mark.parametrize(
    "password",
    [
        "Correct-password1",
        'Quotes"Backslash\\Digit1',
        "Upper1\nNewline\rTab\tControl\x01\x1f",
        "Unicode-Ä-密码-1",
        'Inject1\",\"license_key\":\"other-proof',
    ],
)
def test_setup_payload_round_trips_credentials_without_json_injection(password):
    values = ["admin@acme.example", password, 'Acme "Corp"', "signed-license-proof\n"]
    result = subprocess.run(
        ["bash", "-c", _serializer() + '\nsetup_admin_payload "$@"', "setup", *values],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == dict(
        zip(("email", "password", "org_name", "license_key"), values, strict=True)
    )


@pytest.mark.parametrize(
    "password,accepted",
    [
        ("Correct-password1", True),
        ("lowercase-with-digit1", False),
        ("Uppercase-without-digit", False),
        ("Short1", False),
        ("A1" + "a" * 255, False),
    ],
)
def test_silent_password_policy_fails_with_input_exit_code(password, accepted):
    result = subprocess.run(
        [
            "bash", "-c",
            'die() { printf "%s" "$1" >&2; exit "$2"; }\n'
            + _serializer() + '\nvalidate_setup_password "$1"',
            "setup", password,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if accepted else 2)
    if not accepted:
        assert "uppercase letter and a digit" in result.stderr
        assert password not in result.stderr


def test_silent_password_file_is_checked_before_install_mutations(tmp_path):
    source = INSTALLER.read_text()
    start = source.index("# ── Silent-mode input validation")
    end = source.index("# ── Resolve / generate secrets", start)
    validation = source[start:end]
    # Input validation must precede filesystem staging, image pulls and startup.
    assert end < source.index('mkdir -p "$CAURA_HOME"')
    password_file = tmp_path / "password"
    password_file.write_text("weak-password\n")
    program = (
        'set -eu\n'
        'die() { printf "%s" "$1" >&2; exit "$2"; }\n'
        'read_file() { [ -n "${1:-}" ] && [ -f "$1" ] && cat "$1"; }\n'
        + _serializer()
        + '\nNON_INTERACTIVE=true; SKIP_ADMIN="$1"; HOSTNAME=acme.example\n'
        + 'LICENSE_PATH=license.key; LICENSE_URL=""; ADMIN_EMAIL=admin@acme.example\n'
        + 'ADMIN_PASSWORD="$2"; ADMIN_PASSWORD_FILE="$3"\n'
        + validation
    )
    for skip_admin, direct_password, expected in [
        ("false", "", 2),
        ("false", "Correct-password1", 0),
        ("true", "", 0),
    ]:
        result = subprocess.run(
            ["bash", "-c", program, "setup", skip_admin, direct_password, str(password_file)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected, result.stderr


def test_error_details_omit_echoed_credential_inputs():
    response = {
        "detail": [{
            "loc": ["body", "password"],
            "msg": "String should have at least 12 characters",
            "input": "private-password",
        }],
        "license_key": "private-license-proof",
    }
    result = subprocess.run(
        ["bash", "-c", _serializer() + "\nsetup_admin_error_detail"],
        input=json.dumps(response),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "String should have at least 12 characters"
    assert "private-password" not in result.stdout
    assert "private-license-proof" not in result.stdout


def test_no_shipped_script_quotes_a_substitution_replacement():
    """bash 4.2 and older keep the quotes of ``${var//pat/"rep"}`` in the result.

    Amazon Linux 2 and CentOS 7 ship bash 4.2, and the quoted form broke the
    setup JSON there for any value holding a control byte. The round trip above
    passes on the bash 5 CI has either way, so the spelling is held here.
    """
    quoted = re.compile(r'\$\{[A-Za-z_][A-Za-z_0-9]*//?[^}]*/"')
    root = INSTALLER.parent
    found = [
        f"{path.relative_to(root)}:{n}: {line.strip()}"
        for path in sorted(root.rglob("*.sh"))
        if ".git" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if quoted.search(line)
    ]
    assert not found, "\n".join(found)
