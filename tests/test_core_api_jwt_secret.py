"""core-api still gets ``JWT_SECRET``: every image this compose can run needs it.

OSS caura-ai/caura#2161 (01c4e18e, 2026-10-22) removed core-api's ``jwt_secret``
setting. Nothing in core-api has signed or verified a JWT since
caura-ai/caura#2154, so a core-api built from 01c4e18e or later ignores the
variable.

No image this compose can run is that new. ``test_db_credentials.py`` lists
them: v2.11.16 to v2.13.0, built from OSS 78f86766 to backend-v3.24.1, and the
newest OSS release on 2026-10-22, backend-v3.35.8, predates the change too.
Each sets ``jwt_secret`` to ``change-me-in-production`` when the variable is
absent and, with ``ENVIRONMENT=production`` as this compose runs core-api,
refuses to start with that value. Dropping the variable here would stop
core-api on every install, and on every upgrade, which keeps ``.env`` but takes
the new compose file.

Remove the variable from core-api, and this test, once the oldest core-api
image the compose can run is built from an OSS commit that contains 01c4e18e.
platform-auth-api and platform-admin-api keep theirs: they sign and verify the
platform's JWTs.
"""

from __future__ import annotations

# A value no default or other variable produces, so finding it in core-api's
# environment means the .env value reached it.
_JWT_SECRET = "jwt-secret-every-runnable-core-api-still-needs"


def test_core_api_is_still_given_the_jwt_secret(compose_services):
    services = compose_services({"JWT_SECRET": _JWT_SECRET})
    environment = services["core-api"]["environment"]
    assert environment.get("JWT_SECRET") == _JWT_SECRET, (
        "core-api no longer receives JWT_SECRET, and every core-api image this "
        "compose can run refuses to start in production without it. See this "
        "module's docstring for when it can go."
    )
