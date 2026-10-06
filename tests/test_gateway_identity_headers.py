"""Every gateway location that vouches for the caller's identity sets all of it
from /_auth.

A location that sends X-Gateway-Secret tells core-api and admin-api that the
identity headers on the request came from the gateway, and they trust them on
that basis. nginx forwards any client header that a location doesn't set itself,
so such a location must set every identity header from the /_auth subrequest:
one it leaves out reaches the backend with the client's own value. These are
structural assertions over the template, not a running nginx.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

NGINX = Path(__file__).resolve().parents[1] / "nginx"
# The file keeps the old brand because nginx/Dockerfile copies it by that name.
TEMPLATE = NGINX / "memclaw-locations.template"  # legacy-name-ok: see above

VOUCHES = 'proxy_set_header X-Gateway-Secret "${GATEWAY_SHARED_SECRET}";'
_BACKEND = re.compile(r"proxy_pass\s+https?://\$(admin|core)_upstream\b")

# Request header -> the /_auth response header it must be copied from: what
# core-api and admin-api read on the gateway path, as the hosted gateway sets it.
IDENTITY = {
    "X-Tenant-ID": "x_tenant_id",
    "X-User-ID": "x_user_id",
    "X-Agent-ID": "x_agent_id",
    "X-Org-Role": "x_org_role",
    "X-Org-Read-Only": "x_org_read_only",
    "X-Readable-Tenant-IDs": "x_readable_tenant_ids",
    "X-Capabilities": "x_capabilities",
    "X-Key-Scopes": "x_key_scopes",
    "X-Auth-Mode": "x_auth_mode",
    "X-Caura-Credential-Kind": "x_caura_credential_kind",
    "X-Install-UUID": "x_install_uuid",
}

# Authenticated backend locations that vouch for no identity, because the
# backend authorizes them on the Authorization header alone.
NO_IDENTITY = {"location /api/license/"}

# Requests that must land on a location the first test checks: every spelling
# of the agent-key routes, and core-api's data and MCP routes.
CHECKED_PATHS = (
    "/api/agent-keys",
    "/api/agent-keys/provision",
    "/api/admin/agent-keys",
    "/api/v1/admin/agent-keys/key-1/rotate",
    "/api/memories",
    "/api/v1/memories/m-1",
    "/mcp",
    "/mcp/session",
)


def _locations() -> dict[str, str]:
    """``{"location [modifier] path": block}`` for every location, comments removed."""
    lines = [
        re.sub(r"(^|\s)#.*", "", line) for line in TEMPLATE.read_text().splitlines()
    ]
    blocks: dict[str, str] = {}
    for i, line in enumerate(lines):
        if not line.startswith("location "):
            continue
        depth = 0
        for j in range(i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth == 0:
                break
        blocks[line.split("{")[0].strip()] = "\n".join(lines[i : j + 1])
    return blocks


def _matches(name: str, path: str) -> bool:
    """Whether location ``name`` matches request ``path``, ignoring precedence."""
    parts = name.split()
    if len(parts) == 2:
        return path.startswith(parts[1])
    modifier, pattern = parts[1], parts[2]
    if modifier == "=":
        return path == pattern
    if modifier == "^~":
        return path.startswith(pattern)
    return (
        re.search(pattern, path, re.IGNORECASE if modifier == "~*" else 0) is not None
    )


def test_every_location_that_vouches_for_identity_sets_all_of_it_from_auth() -> None:
    gaps = []
    for name, block in _locations().items():
        if VOUCHES not in block:
            continue
        if "auth_request /_auth;" not in block:
            gaps.append(f"{name}: vouches for an identity it never resolved")
            continue
        for header, source in IDENTITY.items():
            sent = re.search(
                rf"proxy_set_header\s+{re.escape(header)}\s+\$(\w+);",
                block,
                re.IGNORECASE,
            )
            if sent is None:
                gaps.append(
                    f"{name}: does not set {header}, so the client's own reaches the backend"
                )
            elif (
                f"auth_request_set ${sent.group(1)} $upstream_http_{source};"
                not in block
            ):
                gaps.append(f"{name}: {header} is not copied from /_auth's {source}")
    assert not gaps, "\n".join(gaps)


def test_authenticated_backend_locations_vouch_or_are_listed() -> None:
    """A location that authenticates the caller and proxies to core-api or
    admin-api vouches for the identity (so the test above covers it), or is a
    listed exception that stays off the admin routes."""
    locations = _locations()
    unlisted = []
    for name, block in locations.items():
        if "auth_request /_auth;" not in block or not _BACKEND.search(block):
            continue
        if VOUCHES in block:
            continue
        if name not in NO_IDENTITY:
            unlisted.append(name)
            continue
        rewrite = re.search(r"rewrite\s+\S+\s+(\S+)\s+break;", block)
        lands = rewrite.group(1) if rewrite else name.split()[-1]
        assert not lands.startswith("/api/v1/admin"), (
            f"{name} reaches {lands} with no identity"
        )
    assert not unlisted, "authenticated backend locations that send no identity: " + (
        "; ".join(unlisted)
    )
    for name in NO_IDENTITY:
        assert name in locations, f"{name} is gone; drop it from NO_IDENTITY"


@pytest.mark.parametrize("path", CHECKED_PATHS)
def test_the_routes_that_matter_land_on_a_vouching_location(path: str) -> None:
    """Keeps the first test from passing on nothing."""
    vouching = [
        name
        for name, block in _locations().items()
        if VOUCHES in block and _matches(name, path)
    ]
    assert vouching, f"no location that vouches for identity serves {path}"
