"""Only Postgres and the services that connect to it are given its password.

core-api was given ``ALLOYDB_HOST``, ``ALLOYDB_PASSWORD`` and the rest, none of
which it reads. OSS commit 9e6d7f8f (2026-06-25, first released in
backend-v2.18.0) removed its database connection; everything it stores goes
through core-storage-api. Nothing read the variables, so nothing failed, and the
password sat in one more container's environment, where ``docker inspect``
shows it.

No core-api image this compose can run predates that commit. Checked on
2026-10-07:

- Connected installs pull ``ghcr.io/caura-ai/caura-onprem-core-api`` or
  ``-core-api-embedder``. Each has five version tags: v2.11.18, v2.11.19,
  v2.11.20, v2.13.0-rc1 and v2.13.0.
- Air-gapped installs load their images from a release tarball. The oldest
  published tarball is v2.11.16's. The release runs for v2.8.2 to v2.10.1
  failed before publishing one.
- Each of those releases built core-api from an OSS commit that contains
  9e6d7f8f, according to its release-onprem run:

  - v2.11.16 and v2.11.17: 78f86766
  - v2.11.18: 5e5f53fd
  - v2.11.19: OSS main on 2026-09-05
  - v2.11.20: 1e051396
  - v2.13.0-rc1 and v2.13.0: backend-v3.24.1

Resolved by compose itself, and the whole rendered service is searched, so the
password reaching a service by any route counts, not only a variable named for
it.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit]

# A value no default or other variable produces, so finding it in a service
# means the password reached that service.
_PASSWORD = "pw-for-the-database-and-its-clients-only"

# Postgres itself, and the two services that open connections to it.
_DATABASE_AND_CLIENTS = {"postgres", "core-storage-api", "platform-storage-api"}


def test_only_the_database_and_its_clients_get_the_password(compose_services):
    # Equality, not a subset: the clients must still get it too, which also
    # keeps this from passing on a render the password never reached at all.
    given = {
        name
        for name, svc in compose_services({"POSTGRES_PASSWORD": _PASSWORD}).items()
        if _PASSWORD in json.dumps(svc)
    }
    assert given == _DATABASE_AND_CLIENTS, (
        f"given the database password: {sorted(given)}; "
        f"only {sorted(_DATABASE_AND_CLIENTS)} use it"
    )
