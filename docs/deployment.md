# Deployment

omron-data ships as a single non-root container image and publishes no port on its own:
run it behind a TLS-terminating reverse proxy. SQLite is the only datastore and lives in a
host directory mounted at `/app/data`, so a backup is a plain file and nothing else has to
be running. The same code runs without a container from a working copy, which is the path
the [README](../README.md) shows; the difference is where the database and the static
files live.

## Requirements

- Docker Engine 24 or newer, with a current Docker Compose release
- a TLS-terminating reverse proxy (Caddy, nginx, Traefik, or a NAS proxy app)
- an `amd64` or `arm64` host
- a host directory for the database that you can `chown`, on a local filesystem and not on
  NFS or SMB: SQLite locking is not safe on a network filesystem

## Images

Released images are published to GHCR:

```sh
docker pull ghcr.io/martinwa/omron-data:latest
```

Pin a version (`ghcr.io/martinwa/omron-data:1.0.0`) or, better, a digest
(`ghcr.io/martinwa/omron-data@sha256:...`) for a deployment you do not want to change
underneath you. To build from a working copy instead of deploying a published image, use
the overlay that always builds and tags `omron-data:local`:

```sh
docker compose -f compose.yaml -f compose.local-build.yaml up -d
```

It cannot quietly deploy a published image or overwrite a release tag with local source.
`tools/audit_image.sh IMAGE` audits an image by digest for HIGH and CRITICAL
vulnerabilities and optionally writes its SPDX SBOM.

A release is built from scratch rather than from the CI build cache, so a published image
always carries the Debian security updates that the base image's `apt-get upgrade` layer
picks up at build time. The CI checks job keeps reusing the cache to stay fast, which is why
only a tag rebuild is a guarantee: rebuild locally with `docker build --no-cache` when you
want the same freshness from a working copy.

## Configuration

`compose.yaml` reads its values from a private `.env` file next to it, or from the shell
when they are exported there. `.env.example` is the copyable list and starts with the
three values that have no usable default. The production Compose file passes application
variables through untouched and defaults only the image, the user and the network name,
so a value in `.env` is what the container sees.

Configuration fails loudly rather than half-working: with `OMRON_DEBUG` off the
application refuses to start when `OMRON_SECRET_KEY` is missing or still one of the
published placeholders, and the container's entry point refuses to start when the data
directory is not writable by the UID it runs as.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMRON_SECRET_KEY` | none, required | Django secret. Generate one per installation: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. A published placeholder is rejected, so the value in `.env.example` cannot start a container |
| `OMRON_SECRET_KEY_FILE` | empty | Path to a file holding the secret, for Docker or Podman secrets. Read only when `OMRON_SECRET_KEY` is empty. Every setting accepts the same `<NAME>_FILE` form |
| `OMRON_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated hostnames the proxy forwards, for example `omron.example.com`. A request with any other `Host` header is refused, and `*` is refused at startup: every hostname has to be named |
| `OMRON_TRUSTED_PROXY` | empty | The address of the reverse proxy that may set `X-Forwarded-Proto` and `X-Forwarded-For`, or `*` for any peer. Required for HTTPS behind a proxy: waitress discards both headers unless the peer is named here, and without them every browser `POST` fails the CSRF origin check. See [Proxy headers](#proxy-headers) |
| `OMRON_CSRF_TRUSTED_ORIGINS` | empty | Comma-separated `https://` origins; needed only when the proxy rewrites the `Host` header. An entry that does not start with `https://` is refused at startup |
| `OMRON_TIME_ZONE` | `Europe/Stockholm` | IANA zone name. The export records wall-clock times with no offset, so imports are read in this zone, impossible timestamps are reported against it, and every date in the UI is rendered in it, while the API answers in UTC with a `Z` suffix. It applies at import time: changing it later does not rewrite stored rows |
| `OMRON_DEBUG` | `false` | Set by the development image only. With it on, a fixed development secret is accepted and the session and CSRF cookies are not `Secure`; never enable it on an instance that is reachable |
| `OMRON_DB_PATH` | `.data/db.sqlite3` in a working copy; `/app/data/omron.sqlite3` in the image | The SQLite file. The image sets it, and the production Compose file passes nothing for it, so the database is always the file behind the `data` mount |
| `OMRON_STATIC_ROOT` | `staticfiles/` in a working copy; `/app/staticfiles` in the image | Where `collectstatic` writes and where WhiteNoise reads. The image sets it |
| `OMRON_VERSION` | empty, which falls back to the version in `pyproject.toml`; the image sets it from the build argument, `development` by default | Shown in the footer |
| `OMRON_PORT` | `8000` | Listen port inside the container, used by the server and by the health check. Leave it alone: `compose.host-port.yaml` publishes container port 8000 |
| `OMRON_THREADS` | `8` | Worker threads the server starts |
| `OMRON_PENDING_DIR` | the system temp directory | Parent of the staging area. The app writes only inside its own `omron-data-pending` subdirectory of it, and cleanup deletes nothing outside that subdirectory: a value such as `/app/data` is safe. Inside the container the default is the `/tmp` tmpfs, so a staged file does not survive a restart |
| `OMRON_IMAGE` | `ghcr.io/martinwa/omron-data:latest` | Image tag or digest to deploy. Compose only |
| `OMRON_HOST_PORT` | `8000` | Loopback port, used only with `compose.host-port.yaml` |
| `OMRON_NETWORK` | `omron-data` | Name of the Docker network Compose creates for the app |
| `PROXY_NETWORK` | none, required by `compose.proxy-network.yaml` | Existing proxy network to join, used only with that overlay |
| `PUID` / `PGID` | `1000` / `1000` | UID and GID the container runs as, and that must own the `data` directory |

`OMRON_DEBUG`, `OMRON_DB_PATH`, `OMRON_STATIC_ROOT` and `OMRON_VERSION` come from the
image and are deliberately absent from `.env.example` and from the production Compose
file. `OMRON_PORT`, `OMRON_THREADS` and `OMRON_PENDING_DIR` are read from the container
environment but passed by nothing, so changing one means adding it yourself in a
`compose.override.yaml`. Compose merges that file next to `compose.yaml` automatically:

```yaml
services:
  web:
    environment:
      - OMRON_THREADS
```

`docker compose config` prints the effective configuration, including which variables are
set.

## First start

The `data` directory is a bind mount, so the host owns it. Create it and give it to the
UID and GID the container runs as, or the app cannot create the database:

```sh
mkdir -p data
sudo chown -R 1000:1000 data
cp .env.example .env
# edit .env: OMRON_SECRET_KEY, OMRON_ALLOWED_HOSTS, OMRON_TRUSTED_PROXY,
#            and OMRON_TIME_ZONE if you are not in Stockholm
docker compose up -d
docker compose logs -f web
```

`OMRON_TRUSTED_PROXY` is required behind the proxy, not optional: waitress discards
`X-Forwarded-Proto` and `X-Forwarded-For` unless the peer is named, the app then sees
`http`, and every browser login fails the CSRF origin check with no other symptom. With
`OMRON_DEBUG` off the server refuses to start while the variable is empty, so the
misconfiguration is loud at boot instead of silent at login. Set `*` for a proxy on the
host or on a private network, or the proxy container's address when it sits on the
`omron-data` network. See [Proxy headers](#proxy-headers).

If the data directory is not writable, startup stops with an error that names the
directory and the `chown` that fixes it, before anything else is attempted. On each start
the container verifies that directory, applies migrations and serves the app with
waitress; waitress on this image drains requests on `SIGTERM`, which `compose.yaml` bounds
with `stop_grace_period: 30s`.

The container runs read-only with every Linux capability dropped, `no-new-privileges`, an
`init` process and a 256 MB tmpfs at `/tmp` — enough scratch for concurrent uploads and their
staged previews — and its built-in health check calls `GET /api/v1/readyz` every 30 seconds
with a 15-second start period:

```sh
docker compose ps
curl -fsS https://omron.example.com/api/v1/healthz   # liveness
curl -fsS https://omron.example.com/api/v1/readyz    # readiness: database and migrations
```

Both probe endpoints are anonymous. `readyz` checks write access to the existing database
and verifies that migration history is complete and consistent. It answers `503` if either
check fails. `healthz` answers independently of the database.
`/admin/` exists for an administrator; the pages a person uses are in the header:
*Readings*, *Imports* and *Tokens*.

## Accounts and tokens

Signup is disabled and there is no self-service password reset, so every account is
created by an administrator. Create the first one once the stack is up:

```sh
docker compose exec web python manage.py createsuperuser
```

It prompts for a username, e-mail and password. For a scripted setup, pass the password in
the environment and give the e-mail explicitly, because `--noinput` needs a value for
every required field and an empty one is allowed:

```sh
docker compose exec -e DJANGO_SUPERUSER_PASSWORD='<password>' web \
  python manage.py createsuperuser --noinput --username <name> --email ''
```

Sign in at `/accounts/login/`. This account is an administrator: a superuser sees every
account in `/admin/` and can create the people who import their own readings under
**Authentication and Authorization → Users**, leaving **Staff status** and **Superuser
status** unchecked, because an ordinary account needs neither to log in and use the app.
Measurements, import runs and tokens in `/admin/` are read-only, for support rather than
editing.

Issue an API token for an account from the shell when the person cannot use the browser,
or let them create one on `/tokens/`:

```sh
docker compose exec web python manage.py make_token --owner <name> --name laptop
```

Add `--expires-days 90` to give that token an expiry, so a leaked secret stops working on
its own instead of waiting to be noticed and revoked.

Every account is separate: each one imports its own exports, keeps its own tokens, and
never sees another account's readings outside the admin.

## Login rate limiting

The application limits login attempts itself, so a limiter in front of it is defence in depth
rather than a requirement. The limit is per username **and** client address: five wrong
passwords for one username from one address lock that pair out for an hour, another account
at the same address is unaffected, and a successful login clears the count for the account.
The same limit covers `/accounts/login/` and `/admin/login/`, a locked-out attempt is
answered with `429` rather than the login form, and retrying while locked out does not extend
the hour.

The client address is the connection address: the one the server resolved, never a header the
caller wrote. Behind the proxy that is the visitor's own address, because waitress replaces
the peer address with the proxy's `X-Forwarded-For` entry for a peer declared in
`OMRON_TRUSTED_PROXY` and discards both forwarded headers from any other. The development
entry point is the exception: `runserver` performs no such filtering, so a forwarding header
sent to it is ignored for this purpose and every caller counts as `127.0.0.1`. A wildcard
`OMRON_TRUSTED_PROXY=*` trusts every peer, so anything that can reach the published port
directly can choose the address the limiter counts against: that evades the limit, it does
not reach another account. It is the same trust the scheme already gets, so keep the port off
the public network as described in [Proxy headers](#proxy-headers).

Failures and lockouts are logged on the `axes` logger, which reaches the container's log:
`docker compose logs -f web` shows them as they happen, and the container's own log rotation
bounds what is kept. The limiter keeps a counter per username and address in the SQLite file
(`axes_accessattempt`) so it survives a restart, and it writes no per-attempt record of the
attempted usernames, addresses or user agents. A lockout therefore ends on its own after the
hour, and a forgotten password is a wait rather than a restart:

```sh
docker compose exec web python manage.py axes_reset_username <name>
```

`axes_reset_username` clears that account's counter. The `axes_reset_logs` command clears
successful sign-in rows and `axes_reset_failure_logs` clears failure records, but neither is
needed routinely here: the failure record is switched off, and the sign-in rows are only
written for accounts that sign in successfully.

## Connecting a reverse proxy

Two patterns cover the usual home lab setups. Terminate TLS at the proxy, keep the app
unreachable from the public network except through it, and raise the proxy's body limit
only as far as needed for the 10 MB upload limit. The application refuses a request body
above 11 MB before reading it, and `tools/serve.py` caps what waitress will spool at
`MAX_REQUEST_BODY_BYTES` (11 MB), so a proxy that forgets its own limit cannot fill the
container's temporary storage.

### A proxy container on a shared Docker network

Compose creates a network named `omron-data`. Attach your proxy container to it and
forward to `web:8000`:

```sh
docker network connect omron-data caddy
```

If your proxy already owns a private network that all your services join, use that
network instead. Its name is the only value you need:

```sh
docker network ls
echo 'PROXY_NETWORK=proxy' >> .env
docker compose -f compose.yaml -f compose.proxy-network.yaml up -d
```

### A proxy on the host

For a proxy installed on the host itself, such as a NAS reverse-proxy package or a native
nginx or Caddy, publish the port on loopback. It is never reachable from outside the host:

```sh
docker compose -f compose.yaml -f compose.host-port.yaml up -d
```

Point the proxy at `http://127.0.0.1:8000` and set `OMRON_HOST_PORT` if 8000 is taken. Do
not bind the port to `0.0.0.0`.

### Proxy headers

Forward the original `Host` and the client's `X-Forwarded-Proto` on every request. The app
trusts that header only when `OMRON_TRUSTED_PROXY` names a proxy: with the variable empty it
falls back to the scheme of the request it received, and a forwarded scheme is ignored on top
of waitress clearing both headers from a peer it was not told to trust. With `OMRON_DEBUG`
off the session and CSRF cookies are `Secure`, so the app has to be served over HTTPS.

waitress strips `X-Forwarded-Proto` and `X-Forwarded-For` from any peer it has not been told
to trust, so declare the proxy or the browser's `POST` will fail the CSRF origin check and
you will see "CSRF verification failed" after every login attempt:

```sh
# any peer may set the header: right for a proxy on a private Docker network
OMRON_TRUSTED_PROXY=*

# or name the proxy's address, when the proxy is a container on the same network
OMRON_TRUSTED_PROXY=172.20.0.4
```

The address is compared as an exact peer address, with no wildcards and no CIDR ranges. A
proxy on the host that reaches a published port is seen as the Docker bridge gateway, not as
the proxy's own address, so that setup takes `*` rather than a named peer.

Only the last hop is read (`trusted_proxy_count` is 1), so a client cannot forge the scheme
by sending its own header past the proxy. `OMRON_CSRF_TRUSTED_ORIGINS` stays empty for the
usual single-host setup: once the scheme is right, the browser's own origin matches the
request and no extra origin list is needed. The header is only trusted by the server the
image starts: a working copy served with plain `waitress-serve` ignores the setting, so run
`uv run python -m tools.serve` there instead.

The upstream below is `web:8000` for a proxy in a shared Docker network; use
`http://127.0.0.1:8000` for a proxy on the host:

```nginx
location / {
    proxy_pass http://web:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    client_max_body_size 10m;
}
```

Caddy forwards `X-Forwarded-Proto` on its own, so this is enough for it:

```caddyfile
omron.example.com {
    reverse_proxy web:8000
}
```

### TLS, HSTS and cookies

TLS terminates at the reverse proxy. The image serves plain HTTP on its private port and
holds no certificate, so HSTS is a proxy decision as well: only the proxy knows the hostname
is reachable over HTTPS everywhere. Caddy sends the header when you ask it to, in the same
block:

```caddyfile
omron.example.com {
    reverse_proxy web:8000
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
}
```

With nginx it is an `add_header` in the `server` block, next to the `location` above:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

None of the cookie flags need a setting at the proxy. Django marks the session cookie
`HttpOnly` and gives both the session and the CSRF cookie `SameSite=Lax`, and the app sets
`Secure` on both whenever `OMRON_DEBUG` is off, so a browser refuses to send either one over
plain HTTP. `SameSite=Lax` is left alone: every form in the app posts to its own origin, so
another value would only loosen what the CSRF middleware already checks.

The app also sends its own security headers, and the proxy should pass them through
untouched rather than add caching: one `Content-Security-Policy` for every page, built around
a fresh nonce that each inline script carries, and `Cache-Control: no-store` on every
authenticated page and every API response, so a cache in front of the app can never hand one
account's readings to another. The generated API reference at `/api/v1/docs` needs no
exemption: its only inline script is a `type="application/json"` data block, which the nonce
policy does not restrict because nothing executes it. Styles are external only (`style-src
'self'`), so a template that grows a `style="..."` attribute or a `<style>` block would be
dropped by the policy; Django's admin carries neither on the pages this app serves. Do not
enable caching for this upstream.

Failed logins and lockouts are logged on the `axes` logger, which reaches the container's
console: `docker compose logs -f web` shows each attempt as the limiter records it. See
[Login rate limiting](#login-rate-limiting).

Keep `.env` private: it is gitignored, excluded from the image build context, and must
never be committed.

## Static files and the read-only root

The container root is read-only, so nothing can be collected while it runs: the
stylesheet and the scripts are collected into `/app/staticfiles` at build time and
WhiteNoise serves them from that layer. That is why changing an asset means rebuilding the
image, and why `OMRON_STATIC_ROOT` should only be overridden together with a writable
volume.

A working copy has no such layer. There, `collectstatic` is a step you run, and it writes
to `staticfiles/` unless `OMRON_STATIC_ROOT` says otherwise:

```sh
uv run python manage.py collectstatic --noinput
export OMRON_DEBUG=true
uv run python -m tools.serve
```

A working copy serves plain HTTP directly, so run it with `OMRON_DEBUG=true`; with debug off
the server refuses to start without `OMRON_TRUSTED_PROXY`, which only a reverse proxy can
provide. Without it the pages render unstyled, because the stylesheet is not where the server looks
for it. WhiteNoise reads the directory once at startup, so re-run `collectstatic` and
restart the server after changing an asset. `staticfiles/` is gitignored, so a collected
asset cannot end up in a commit.

## Upgrading

Take a backup before upgrading. See [Backup and recovery](backup-and-recovery.md).

Set `OMRON_TRUSTED_PROXY` in `.env` before replacing the running container. The
production entry point now requires a trusted proxy at startup. See [Proxy headers](#proxy-headers).

Update `OMRON_IMAGE` to the release tag and recreate the container.

```sh
docker compose up -d --pull always
```

`--pull always` matters: without it Compose does not refresh an unchanged tag such as
`latest`. Migrations run automatically on start. To roll back, point `OMRON_IMAGE` back to
the previous tag and restore the database backup together, because a newer schema may
already have been applied.

Staged uploads written before this version carry no `.omron-data-pending` marker file, and
cleanup only deletes folders it can prove it created, so the old ones survive an upgrade.
Where they are depends on how this instance was configured:

- With `OMRON_PENDING_DIR` set, the old code treated that directory as the staging root, so
  each import got a subdirectory directly inside it: `OMRON_PENDING_DIR/<id>/`, holding the
  export and its `summary.json` and carrying no marker file. Imports since the upgrade are
  one level deeper, in `OMRON_PENDING_DIR/omron-data-pending/<id>/`, and are still tracked,
  so remove only the marker-less subdirectories sitting directly in `OMRON_PENDING_DIR` and
  leave `omron-data-pending/` alone while an upload is waiting to be confirmed.
- With it unset, the old and the current code both use `omron-data-pending/` under the
  system temp directory, so the marker-less subdirectories to remove there are one level
  further down, inside `omron-data-pending/`.

## Ownership

The image runs as an unprivileged user with every capability dropped, so it can never
`chown` anything, inside the container or on the host. Ownership is therefore a host
concern: the `data` directory must belong to `PUID:PGID`, `1000:1000` by default, and must
never belong to root. A root-owned `data` directory produces a container that exits at
startup with the `chown` line that fixes it. On a NAS where those IDs differ, set them and
fix the directory once:

```sh
echo 'PUID=1026' >> .env
echo 'PGID=100' >> .env
docker compose down
sudo chown -R 1026:100 data
docker compose up -d
```

## Verifying an installation

The image carries two checks you can run against a deployed container:

```sh
docker compose exec -T web python tools/check_production.py
docker compose exec -T web python tools/healthcheck.py
```

`check_production.py` requires `OMRON_DEBUG` off, a declared trusted proxy, a private
secret, a writable database and directory, and a complete, consistent migration history.
It prints one line per problem. The container runs `healthcheck.py`, which probes
`127.0.0.1` on `OMRON_PORT` with the first hostname from
`OMRON_ALLOWED_HOSTS` as the `Host` header, so a wrong `OMRON_ALLOWED_HOSTS` shows up here
as an unhealthy container rather than as a silent 400 on every page.
