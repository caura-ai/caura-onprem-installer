# Sigstore trusted root

`trusted_root.json` is the trusted root of Sigstore's public-good instance:
the Fulcio certificate authorities, the Rekor transparency-log keys, the CT
logs and the timestamp authority. `airgap-load.sh` verifies a release
tarball's signature against it with no network, so it ships in the installer
bundle and is never read from beside the tarball. It is exactly as trustworthy
as the installer bundle that carries it, which nothing verifies: the check
protects against a replaced release tarball, not against a replaced bundle.

Pinned 2026-10-28 from TUF targets metadata version 14:
sha256 `6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755fc3517c9e6bc0b66`,
the same file as `targets/trusted_root.json` in
[sigstore/root-signing](https://github.com/sigstore/root-signing).

Sigstore adds keys to this file when it rotates a service. A release signed
after a rotation the pinned copy does not know fails verification (closed).
Refresh it from the same TUF repository, check the hash against the signed
targets metadata, and publish the installer bundle again.
