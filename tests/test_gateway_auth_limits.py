"""Auth bursts are bounded in the real rendered HTTP and TLS gateways.

Docker tests run in the existing CI suite with synthetic loopback upstreams.
They may skip on developer machines without Docker; missing Docker fails CI.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

NGINX = Path(__file__).resolve().parents[1] / "nginx"
PREFIXES = ["/api/auth/", "/api/v1/auth/"]
SECRET = "synthetic-auth-hop-proof"
IDENTITY_HEADERS = [
    "X-Tenant-ID",
    "X-User-ID",
    "X-Agent-ID",
    "X-Org-Role",
    "X-Org-Read-Only",
    "X-Org-Read-Only-Reason",
    "X-Readable-Tenant-IDs",
    "X-Capabilities",
    "X-Key-Scopes",
    "X-Auth-Mode",
    "X-Caura-Credential-Kind",
    "X-Install-UUID"
]

LOCATION_TEMPLATE = NGINX / "memclaw-locations.template"  # legacy-name-ok: shipped filename
OUTER_TEMPLATES = ["memclaw.conf.template", "memclaw-tls.conf.template"]  # legacy-name-ok: shipped filenames


def _auth_block(prefix: str) -> str:
    source = LOCATION_TEMPLATE.read_text()
    return source.split(f"location {prefix} {{\n", 1)[1].split("\n}", 1)[0]


@pytest.mark.parametrize("name", OUTER_TEMPLATES)
def test_both_modes_define_the_same_peer_ip_budget(name: str) -> None:
    source = (NGINX / name).read_text()
    zones = re.findall(r"^limit_req_zone\s+([^;]+);", source, re.MULTILINE)
    assert zones == ["$binary_remote_addr zone=auth_zone:10m rate=${AUTH_RATE_LIMIT_RPS}r/s"]
    assert "map $limit_req_status $auth_retry_after" in source
    assert "REJECTED 1;" in source
    assert "add_header Retry-After $auth_retry_after always;" in source
    assert "real_ip_header" not in source
    assert "set_real_ip_from" not in source


@pytest.mark.parametrize("prefix", PREFIXES)
def test_auth_aliases_share_budget_and_replace_proof(prefix: str) -> None:
    block = _auth_block(prefix)
    assert "limit_req zone=auth_zone burst=${AUTH_RATE_LIMIT_BURST} nodelay;" in block
    assert "limit_req_status 429;" in block
    assert "error_page 429 = @auth_rate_limited;" in block
    assert "proxy_intercept_errors off;" in block
    assert 'proxy_set_header X-Gateway-Secret "${GATEWAY_SHARED_SECRET}";' in block
    assert 'proxy_set_header X-Auth-Subrequest "";' in block
    assert "$http_x_gateway_secret" not in block
    assert "proxy_pass http://$auth_upstream;" in block


def test_rejection_has_the_api_error_contract() -> None:
    source = LOCATION_TEMPLATE.read_text()
    block = source.split("location @auth_rate_limited {", 1)[1].split("\n}", 1)[0]
    match = re.search(r"return 429 '([^']+)';", block)
    assert match is not None
    body = json.loads(match.group(1))
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["detail"] == body["error"]["message"]
    assert "default_type application/json;" in block
    # No child add_header may shadow the TLS server's HSTS/retry headers.
    assert "add_header" not in block


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, ("100", "200")),
        ({"AUTH_RATE_LIMIT_RPS": "75", "AUTH_RATE_LIMIT_BURST": "90"}, ("75", "90")),
        ({"AUTH_RATE_LIMIT_RPS": "", "AUTH_RATE_LIMIT_BURST": ""}, ("100", "200")),
    ],
)
def test_compose_passes_operator_limits(compose_services, env, expected) -> None:
    gateway = compose_services(env)["gateway"]["environment"]
    assert (gateway["AUTH_RATE_LIMIT_RPS"], gateway["AUTH_RATE_LIMIT_BURST"]) == expected


def _docker(*args: str, timeout: int = 120, check: bool = True):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
    )


@pytest.fixture(scope="module")
def gateway_image() -> Iterator[str]:
    if shutil.which("docker") is None:
        if os.environ.get("CI"):
            pytest.fail("Docker is required to exercise auth limiting in CI")
        pytest.skip("needs Docker")
    probe = _docker("info", check=False)
    if probe.returncode:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon is required in CI: " + probe.stderr)
        pytest.skip("needs a running Docker daemon")
    image = "caura-auth-limits-" + uuid.uuid4().hex[:12]
    try:
        _docker("build", "-t", image, str(NGINX), timeout=180)
        yield image
    finally:
        _docker("image", "rm", "-f", image, check=False)


@pytest.mark.parametrize("name", ["AUTH_RATE_LIMIT_RPS", "AUTH_RATE_LIMIT_BURST"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "01", "100000", "1; return 200;"])
def test_entrypoint_refuses_invalid_limits(gateway_image: str, name: str, value: str):
    result = _docker(
        "run", "--rm", "-e", f"GATEWAY_SHARED_SECRET={SECRET}",
        "-e", f"{name}={value}", gateway_image, "nginx", "-t", check=False,
    )
    assert result.returncode != 0
    assert f"FATAL: {name} must be an integer from 1 to 99999" in result.stderr


def _request(
    endpoint: tuple[bool, int, int], path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict, dict[str, str]]:
    tls, port, _ = endpoint
    connection: http.client.HTTPConnection
    if tls:
        # Fresh, test-generated certificate, trusted only for this loopback test.
        connection = http.client.HTTPSConnection(
            "127.0.0.1", port, context=ssl._create_unverified_context(), timeout=5
        )
    else:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        body = json.loads(raw) if raw.startswith(b"{") else {}
        return response.status, body, dict(response.getheaders())
    finally:
        connection.close()


@pytest.fixture(
    params=[(False, 100, 200), (True, 100, 200), (False, 2, 3), (True, 2, 3)],
    ids=["http-default", "tls-default", "http-custom", "tls-custom"],
)
def gateway(
    gateway_image: str, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[bool, int, int]]:
    tls, rate, burst = request.param
    # Echoing synthetic upstream only. No deployed service or real credentials.
    backend = tmp_path / "backend.conf"
    echo = json.dumps({
        "proof": "$http_x_gateway_secret", "marker": "$http_x_auth_subrequest",
        **{header: "$http_" + header.lower().replace("-", "_") for header in IDENTITY_HEADERS},
    })
    backend.write_text("""
server {
    listen 18081;
    default_type application/json;
    location = /api/v1/auth/user/reset-password {
        add_header Retry-After 77 always;
        return 429 '{"detail":"account-lockout"}';
    }
    location / {
        return 200 '""" + echo + """';
    }
}
""")
    container = "caura-auth-limits-" + uuid.uuid4().hex[:12]
    port = "443" if tls else "80"
    args = [
        "run", "-d", "--name", container,
        "-p", f"127.0.0.1::{port}",
        "-v", f"{backend}:/etc/nginx/conf.d/test-backend.conf:ro",
        "-e", "SERVER_NAME=localhost",
        "-e", f"GATEWAY_SHARED_SECRET={SECRET}",
    ]
    for upstream in ["AUTH", "ADMIN", "CORE", "APP"]:
        args.extend(["-e", f"{upstream}_UPSTREAM=127.0.0.1:18081"])
    if (rate, burst) != (100, 200):
        args.extend([
            "-e", f"AUTH_RATE_LIMIT_RPS={rate}",
            "-e", f"AUTH_RATE_LIMIT_BURST={burst}",
        ])
    if tls:
        if shutil.which("openssl") is None:
            pytest.fail("openssl is required for the synthetic TLS certificate")
        certs = tmp_path / "tls"
        certs.mkdir()
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(certs / "key.pem"),
                "-out", str(certs / "cert.pem"),
                "-subj", "/CN=localhost", "-days", "1",
            ],
            check=True, capture_output=True, timeout=30,
        )
        args.extend(["-v", f"{certs}:/etc/nginx/tls:ro"])
    try:
        _docker(*args, gateway_image)
        mapping = _docker("port", container, port + "/tcp").stdout.strip()
        endpoint = (tls, int(mapping.rsplit(":", 1)[1]), burst)
        for _ in range(100):
            try:
                if _request(endpoint, "/healthz")[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        else:
            pytest.fail("Gateway did not become healthy: " + _docker("logs", container).stdout)
        _docker("exec", container, "nginx", "-t")
        rendered = _docker("exec", container, "nginx", "-T").stdout
        assert f"zone=auth_zone:10m rate={rate}r/s;" in rendered
        assert rendered.count(f"zone=auth_zone burst={burst} nodelay;") == 2
        yield endpoint
    finally:
        _docker("rm", "-f", container, check=False)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_real_nginx_proof_rate_limit_and_alias_budget(gateway, prefix: str) -> None:
    def check_transport(headers: dict[str, str]) -> None:
        if gateway[0]:
            assert headers["Strict-Transport-Security"] == (
                "max-age=2592000; includeSubDomains"
            )
        else:
            assert "Strict-Transport-Security" not in headers

    status, body, headers = _request(
        gateway, prefix + "user/login",
        {"X-Gateway-Secret": "caller-proof", "X-Auth-Subrequest": "1",
         **{header: "caller-forged" for header in IDENTITY_HEADERS}},
    )
    assert status == 200
    assert body == {"proof": SECRET, "marker": "", **{h: "" for h in IDENTITY_HEADERS}}
    check_transport(headers)
    # A real backend lockout is preserved, including its recovery hint.
    status, body, headers = _request(gateway, prefix + "user/reset-password")
    assert status == 429
    assert body == {"detail": "account-lockout"}
    assert headers["Retry-After"] == "77"
    check_transport(headers)

    def call(index: int, route: str = prefix):
        return _request(
            gateway, route + "user/login",
            {"X-Forwarded-For": f"198.51.100.{index % 250 + 1}",
             "X-Real-IP": f"203.0.113.{index % 250 + 1}"},
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        responses = list(pool.map(call, range(max(80, gateway[2] * 4))))
    rejected = [r for r in responses if r[0] == 429]
    assert rejected, "Changing client-supplied proxy headers bypassed the budget"
    assert {r[0] for r in responses} <= {200, 429}
    for _, body, headers in rejected:
        assert body["error"]["code"] == "RATE_LIMITED"
        assert headers["Retry-After"] == "1"
        check_transport(headers)
    # Switching spellings cannot obtain another full burst for the same peer.
    other = next(path for path in PREFIXES if path != prefix)
    with ThreadPoolExecutor(max_workers=16) as pool:
        alias = list(pool.map(lambda i: call(i, other), range(32)))
    assert any(r[0] == 429 for r in alias), "Auth aliases must share one budget"
    assert _request(gateway, "/healthz")[0] == 200
    assert _request(gateway, "/api/auth/validate")[0] == 404
    assert _request(gateway, "/api/v1/auth/validate")[0] == 404
