# License directory

Drop your Caura-issued `license.key` here (the file is gitignored).
The docker-compose stack mounts this directory read-only into
`platform-admin-api` and `platform-auth-api` at `/etc/caura/`.

If the file is missing at startup the services will refuse to boot with
a clear error. Use the web wizard at `/setup` or `cauractl license load`
to drop a new file without editing this directory by hand.

The license is a signed RS256 JWT — the public key that verifies it is
baked into the service images. To rotate or obtain a license, contact
Caura support (support@caura.ai).
