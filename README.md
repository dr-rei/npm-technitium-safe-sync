# NPM â†’ Technitium Safe Sync

Poll enabled Nginx Proxy Manager proxy hosts and reconcile their hostnames to IPv4 `A` records in dedicated Technitium Primary zones. Every managed name points to **the NPM reverse proxy IP**, not its upstream service IP. Other record types, wildcard hosts, apex names, redirection hosts, and 404 hosts are excluded.

Project-maintained container image: `ghcr.io/dr-rei/npm-technitium-safe-sync`. This is not affiliated with NPM or Technitium and does not carry Docker's Official Image badge.

## Publication and validation

Images are published from main and version tags. Use the selected GitHub release and its attached `RELEASE_EVIDENCE.md` to confirm the exact digest, workflow result and anonymous access checks. A prepared workflow or template alone is not evidence that a version was published.

Tests include local HTTP fixtures and disposable real NPM **2.16.0** / Technitium **15.6.2** containers pinned by digest. Both authentication modes, restricted accounts, authoritative DNS and NPM-to-backend forwarding are exercised. See [INTEGRATION_REVIEW.md](INTEGRATION_REVIEW.md) for coverage, repairs and limits. Complete staging before production writes. The synchronizer never needs your real credentials in GitHub Actions.

## Safety contract

- Runtime/file-secret defaults: `DRY_RUN=true`, `ENABLE_DELETIONS=false`, `ALLOW_EMPTY_NPM=false`. The fresh-install quickstart explicitly sets `DRY_RUN=false` and keeps deletion disabled.
- Read and validate all NPM hosts and permitted zones before writing. Malformed, unavailable, oversized, or empty source responses abort the cycle. Booleans and zone configuration are validated strictly.
- Never adopt an existing DNS name, even if it has the same IP or a managed comment. Ownership requires persisted state **and** the exact managed comment, one enabled A record, and the previous IP. A changed comment, disabled record, multiple records, or mixed types causes a conflict.
- Before each mutation, read the DNS snapshot again and refuse the write if the target changed. Refuse names below NS delegations, DNAMEs or CNAMEs. Use dedicated zones without separately hosted child zones; separate child zones are not discoverable through the permitted parent-zone API.
- Deletions require opt-in, ownership checks, a grace period (24 hours default) and a cap (five stale names default). Exceeding the cap blocks **all** deletions for that cycle. `ALLOW_EMPTY_NPM=true` is separately required to remove the final host.
- State storage is tested before remote mutations; writes use flush/fsync, atomic rename and directory fsync. A local lock prevents two processes using the same state directory. Back up state and zones together.
- API timeouts, response-size limits, certificate verification, disabled redirects, and disabled environment HTTP proxies protect management credentials. HTTPS is required unless plaintext HTTP is explicitly opted into on an isolated trusted network. API bodies, tokens, passwords and raw remote errors are not logged.

**Limits:** Technitium provides no conditional-write/transaction contract for these endpoints. A change between the final read and write remains possible. Run exactly one synchronizer per zone, use one persistent local state volume, and coordinate manual edits. Multiple independent state volumes are not safe HA. A write followed by a crash before state persistence can leave an orphan; inspect it manually, as the service refuses automatic adoption. A failed cycle can contain earlier successful writes; it is not a transaction. Missing owned records are conflicts and are not silently recreated. DNSSEC-generated records can cause mixed-type conflicts; use a dedicated unsigned staging/production zone for this implementation. Large zones above the 4 MB API response limit fail closed.

## Production quickstart: two files, then up

For a **fresh installation**, you need only `compose.yaml` and `.env`. The preset
pulls the verified public **1.0.2** image by digest, creates persistent storage
with the correct nonroot ownership, and starts polling automatically. No source
checkout, local build, secret files, host data directory or manual `chown` is
required. Existing installations should use the upgrade instructions below and
preserve their existing state mount.

### 1. Prepare the existing apps

- Ubuntu/Linux with Docker Engine and **Docker Compose v2**, on amd64 or arm64.
  If needed, follow [Docker's Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/), including the Compose plugin.
- Back up Technitium, then create an enabled **Primary** zone such as
  `apps.example.com`. This application does not create zones or accounts.
- Create a dedicated NPM account with **view access to all intended proxy hosts**.
  Use that account's password, not a registry token. Incomplete visibility can
  make valid hosts appear absent; keep deletion off initially.
- Create a Technitium account/API token with **View + Modify** on that zone only.
  Zone Delete permission is unnecessary while deletions are disabled.
- The container must reach both management URLs, and DNS clients must reach the
  NPM proxy IP. Existing NPM/Technitium services do not need restarting.

### 2. Create the two files

Use a **new, empty deployment directory**. Do not download over an existing
installation's `.env` or replace its state configuration.

```sh
mkdir -m 700 ~/npm-technitium-safe-sync
cd ~/npm-technitium-safe-sync
umask 077
curl --fail --silent --show-error --location https://raw.githubusercontent.com/dr-rei/npm-technitium-safe-sync/main/compose.quickstart.yaml --output compose.yaml
curl --fail --silent --show-error --location https://raw.githubusercontent.com/dr-rei/npm-technitium-safe-sync/main/.env.quickstart.example --output .env
chmod 600 .env
nano .env
```

Alternatively, create the files manually using the exact contents below.
[Compose preset](compose.quickstart.yaml) · [Environment template](.env.quickstart.example)

<details>
<summary>compose.yaml</summary>

```yaml
# Fresh installs: save as compose.yaml next to .env.quickstart.example renamed .env.
name: npm-technitium-safe-sync
services:
  npm-technitium-safe-sync:
    image: ${SYNC_IMAGE:?Set SYNC_IMAGE to the verified release image in .env}
    restart: unless-stopped
    init: true
    env_file: .env
    environment:
      NPM_URL: ${NPM_URL:?Set NPM_URL in .env}
      NPM_IDENTITY: ${NPM_IDENTITY:?Set NPM_IDENTITY in .env}
      NPM_SECRET: ${NPM_SECRET:?Set NPM_SECRET in .env}
      TECHNITIUM_URL: ${TECHNITIUM_URL:?Set TECHNITIUM_URL in .env}
      TECHNITIUM_TOKEN: ${TECHNITIUM_TOKEN:?Set TECHNITIUM_TOKEN in .env}
      NPM_LAN_IP: ${NPM_LAN_IP:?Set NPM_LAN_IP in .env}
      DNS_ZONES: ${DNS_ZONES:?Set DNS_ZONES in .env}
    volumes:
      - sync_data:/data
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=16m
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    user: "10001:10001"
    cpus: 0.25
    mem_limit: 128m
    pids_limit: 32
    stop_grace_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
volumes:
  sync_data:
```

</details>

<details>
<summary>.env</summary>

```dotenv
# Fresh installs only. Save as .env next to compose.quickstart.yaml saved as compose.yaml.
# Verified public 1.0.2 image; no registry login or local build is needed.
SYNC_IMAGE=docker.io/drreilab/npm-technitium-safe-sync@sha256:867fdd742b8b1db614f45aed775702cac2e6bfa3d6537b5c176f0b3d62230dab
NPM_URL=https://npm.internal.example.com
NPM_IDENTITY=dns-sync@example.com
# Use single quotes so dollar signs, hashes and spaces remain literal.
# An apostrophe inside single quotes is escaped as \'. Never paste real secrets into chat.
NPM_SECRET='REPLACE_WITH_NPM_ACCOUNT_PASSWORD'
TECHNITIUM_URL=https://dns.internal.example.com
TECHNITIUM_TOKEN='REPLACE_WITH_ZONE_SCOPED_API_TOKEN'
TECHNITIUM_AUTH_MODE=bearer
# Reachable IPv4 of NPM itself, not its backend; localhost is not the Docker host.
NPM_LAN_IP=192.0.2.10
DNS_ZONES=apps.example.com
DNS_TTL_SECONDS=120
SYNC_INTERVAL_SECONDS=300
# This preset starts creating/updating owned A records immediately.
# Set true for an initial preview with no DNS writes.
DRY_RUN=false
ENABLE_DELETIONS=false
DELETE_GRACE_SECONDS=86400
MAX_DELETES_PER_RUN=5
ALLOW_EMPTY_NPM=false
# Change to true only for HTTP management endpoints on a trusted isolated network.
ALLOW_INSECURE_HTTP=false
```

</details>

Replace these values in `.env`:

| Setting | What to enter |
| --- | --- |
| `NPM_URL` | NPM **management/API** URL; commonly port 81 for HTTP, or its existing trusted HTTPS URL |
| `NPM_IDENTITY` / `NPM_SECRET` | Dedicated NPM account email and password |
| `TECHNITIUM_URL` | DNS server management/API URL; commonly port 5380 for HTTP, or its trusted HTTPS URL |
| `TECHNITIUM_TOKEN` | Dedicated zone-scoped API token |
| `NPM_LAN_IP` | IPv4 clients use to reach NPM on ports 80/443; **not** the upstream backend IP |
| `DNS_ZONES` | Existing dedicated zone, for example `apps.example.com`; comma-separate nonoverlapping zones |

Keep credentials single-quoted so `$`, `#`, spaces and backslashes stay literal;
an apostrophe is written `\'` inside the quotes. Protect `.env` with mode 600 and
keep it out of Git. Do not paste it into logs or chat. This simple preset puts
credentials into the container environment, which Docker administrators can
inspect. For file-mounted credentials, use the existing preset described below.
Do not combine inline credentials with their `_FILE` equivalents.

Use reachable LAN IPs/hostnames rather than `localhost`, which means the new
container itself. Management hostnames must already resolve independently of
this service. For HTTP endpoints such as `http://192.168.1.10:81` and
`http://192.168.1.11:5380`, explicitly set `ALLOW_INSECURE_HTTP=true` only on a
trusted isolated network. HTTPS remains the default. Private CAs need a mounted
CA bundle as described under API compatibility; verification is never disabled.

### 3. Start

```sh
docker compose up -d
```

Docker pulls the pinned image, creates the named state volume and starts the
service. **This preset sets `DRY_RUN=false`: it starts creating/updating owned A
records immediately once both APIs validate.** Existing manual DNS is preserved;
deletion remains disabled. To preview first, set `DRY_RUN=true` before starting.
The Python runtime default and the older file-secret template remain dry-run by
default.

For example, an enabled NPM proxy host `app.apps.example.com` is published as an
A record pointing to `NPM_LAN_IP`. The zone apex and wildcard hosts are excluded.
Polling defaults to 300 seconds; the first cycle begins on startup.

### 4. Confirm operation

```sh
docker compose ps
docker compose logs --tail=100 npm-technitium-safe-sync
nslookup app.apps.example.com YOUR_TECHNITIUM_IP
```

Look for `Cycle completed`, review `added`, `updated` and `conflicts`, and test the
application through NPM. Health becomes healthy after a completed cycle and the
next health probe; it can initially show `starting`. Health includes dry-run and
preserved conflicts, so it does not replace DNS/application checks. If there is
an API, certificate, permission or storage error, the service logs a sanitized
failure and retries. Correct `.env`, then apply changes with:

```sh
docker compose up -d --force-recreate
```

`docker compose restart` alone does not reload changed environment values.
For optional configuration validation without printing credentials, use
`docker compose config --quiet` and
`docker compose run --rm npm-technitium-safe-sync --check`.

### State, updates and backups

The named volume is `npm-technitium-safe-sync_sync_data` under the preset's
fixed project name. This preset represents one deployment per Docker host: a
second directory using the same project name manages the same service and volume.
Docker creates the volume automatically; ordinary container recreation and
`docker compose down` preserve it. **Do not run `docker compose down -v`, remove
or prune this volume, change the project name, or launch a second deployment
against the same zones.** Losing ownership state causes existing records to be
preserved as conflicts rather than automatically adopted.

Before updating, stop the synchronizer and back up both state and Technitium
zones. One state-backup method, while the service container still exists, is:

```sh
docker compose stop
backup_dir="state-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
docker compose cp npm-technitium-safe-sync:/data/. "$backup_dir/"
docker compose start
```

Update only `SYNC_IMAGE` to the next verified release digest, then use
`docker compose pull` and `docker compose up -d --force-recreate`. Preserve `.env`
and state; keep deletions off during upgrade validation. Rolling back the image
does not undo DNS changes. Review coordinated state/zone backups before restoring.

**Already deployed with `./data` and file secrets?** Keep the existing
`compose.yaml`, `.env`, `data/` and `secrets/` setup. It remains supported. Update
its image pin only; do not switch to the quickstart named volume without a
planned, stopped-service state migration. The two presets are alternatives,
not Compose files to merge.

## Existing deployment / file-secret preset

Requirements: Linux Docker Engine, Docker Compose v2, amd64 or arm64, and connectivity from the container to the management endpoints. Existing NPM and Technitium containers do not need restarting. The original `compose.yaml` uses file secrets and the existing `./data` bind mount; it pulls an image and contains no build instruction.

1. Download `compose.yaml` and `.env.example` from the selected release, or clone this repository and check out its version tag.
2. Back up your Technitium zone. Create an enabled **Primary** zone such as `apps.example.com`. Do not configure overlapping `DNS_ZONES`, zone apex hostnames, or separately hosted subzones.
3. Create a dedicated NPM account with permission to view **all intended proxy hosts**. A restricted account that sees only some hosts is an incomplete source and could make others look stale. Verify `GET /api/nginx/proxy-hosts` with this account. Recheck access whenever ownership or permissions change. Keep deletion disabled until source visibility is established.
4. Create a dedicated Technitium user/token with **View + Modify** on the permitted zones only. Grant zone **Delete** only if enabling deletion. No global administrator token is required. Rotate these credentials using the same scoped accounts.
5. Prepare the deployment directory on the Linux Docker host:

```sh
mkdir -p data secrets
sudo chown 10001:10001 data
sudo chmod 700 data
cp .env.example .env
chmod 600 .env
# Enter credentials through an editor; never commit or paste them into GitHub.
${EDITOR:-vi} secrets/npm_secret
${EDITOR:-vi} secrets/technitium_token
sudo chown 10001:10001 secrets/npm_secret secrets/technitium_token
sudo chmod 400 secrets/npm_secret secrets/technitium_token
```

Compose file secrets are local file mounts, not an encrypted vault. The container UID 10001 must be able to read them. Leading/trailing password spaces are preserved; trailing CR/LF is stripped. Docker administrators and host root can access secrets. Never put real secrets in the Dockerfile, image, repository, workflow, or shell history. `.gitignore` and `.dockerignore` exclude local secrets and state. Do not define inline `NPM_SECRET` or `TECHNITIUM_TOKEN` with this Compose file; duplicate inline/file settings are rejected.

6. Edit `.env` with real endpoints, proxy IPv4, zones and a verified image digest. Keep `DRY_RUN=true` and deletion off.

```sh
${EDITOR:-vi} .env
docker compose config --quiet
docker compose pull
docker compose run --rm npm-technitium-safe-sync --check
docker compose run --rm npm-technitium-safe-sync --once
docker compose up -d
docker compose logs --tail=100 -f
```

`--check` validates settings without API access. `--once` contacts the APIs and honors `DRY_RUN`; in write mode it changes DNS. A zero exit code means the cycle completed; inspect reported conflicts as well. Avoid running it alongside the long-running service (the shared lock rejects overlap).

7. Verify planned hostnames, IPs, existing-name conflicts, source visibility and zone backups. Set `DRY_RUN=false`, recreate this service with `docker compose up -d --force-recreate`, and confirm DNS with `nslookup app.apps.example.com <Technitium-IP>`. Test HTTPS through NPM with `curl -I --resolve app.apps.example.com:443:<NPM-IP> https://app.apps.example.com/`.
8. Keep deletions disabled for initial rollout. In a disposable staging zone, add a host, change proxy IP, disable/remove a host, wait for the grace period, test the cap, change a comment manually, and confirm conflicts are preserved. Exercise API outage/recovery and a container restart with its state volume. Only then opt into deletions and, if intended, empty-source deletion.

### API compatibility and transport

NPM uses `POST /api/tokens` then `GET /api/nginx/proxy-hosts` with a bearer token. Enabled values may be JSON booleans or integer 0/1. The project uses the upstream proxy-host list shape; forks with incompatible schemas fail closed.

`TECHNITIUM_AUTH_MODE=bearer` targets Technitium **15+**, whose API documents bearer authentication. For older Technitium versions use `parameter`; the token is sent in a POST form body, never a query URL. All Technitium calls use POST forms. Both modes are tested against actual Technitium 15.6.2; older releases and your installed configuration still need staging validation.

Use trusted HTTPS certificates, final URLs without redirects, and firewalls limiting management-port access. HTTP requires `ALLOW_INSECURE_HTTP=true` and sends credentials in plaintext. For a private CA, mount a CA bundle read-only and set `SSL_CERT_FILE` to its container path; do not disable TLS verification. Ambient HTTP proxy variables are intentionally ignored. When using host management endpoints from Docker, `localhost` refers to this new container; use reachable host/IP addresses or appropriate Docker networks.

## Operational repairs in 1.0.2

Version 1.0.2 repairs state-storage preflight, deletion accounting after zone removal, in-cycle shutdown, malformed empty host lists and oversized settings. It also adds actionable safe API diagnostics and requires real-app tests before image publication. See [INTEGRATION_REVIEW.md](INTEGRATION_REVIEW.md). Back up zones and state, stage the new digest with deletion off, and review conflicts before production writes.

## Audit repairs in 1.0.1

Version 1.0.1 repairs the two operational defects identified in the full v1.0.0 audit: source reappearance now durably clears an old deletion timer before DNS conflicts/writes, and both initial and fresh-read guards now detect zone-apex DNAME/CNAME aliases while allowing ordinary apex NS/SOA records. See [AUDIT_REMEDIATION.md](AUDIT_REMEDIATION.md) for the regression cases and remaining production acceptance gates.

When upgrading from 1.0.0, first disable deletions and back up zone + state. The fix cannot reconstruct presence history missed by the old version. Treat old pending removals conservatively: observe successful upgraded polls throughout at least DELETE_GRACE_SECONDS with deletion disabled before considering re-enabling it. Review pending removals and staging results first. State format and record ownership marker remain compatible.

## Version pinning, updates and rollback

Production should set `SYNC_IMAGE=ghcr.io/dr-rei/npm-technitium-safe-sync@sha256:<verified-index-digest>` in `.env`. The release evidence includes an actual digest after publication. Version tag `:1.0.2` is readable but tags can be moved; a digest pins exact image content. `:main` tracks development and is not a production pin. GHCR has no floating `latest` tag; Docker Hub `latest` follows the GitHub Latest stable release.

The workflow builds `linux/amd64` and `linux/arm64`. Main pushes publish `:main` to GHCR only. Version tags publish `:MAJOR.MINOR.PATCH` to both enabled registries; Docker Hub `latest` follows GitHub Latest. No new commit-hash tags are created. Docker Hub shows only `latest` and numbered releases. Use a digest for immutability. Releases must use new version numbers; never retag an existing release.

```sh
# Verify the selected version, copy its digest into .env, then pull it.
docker buildx imagetools inspect ghcr.io/dr-rei/npm-technitium-safe-sync:1.0.2
docker compose pull
docker compose up -d --force-recreate
```

Back up zone + state before an update. Roll back by setting the previous verified digest and recreating the container. Rolling back an image does not undo DNS writes; restore DNS/state together only after stopping the service and reviewing the backup. Stop writes quickly with `docker compose stop`.

## Docker Hub distribution

The workflow also supports the configured Docker Hub destination `docker.io/drreilab/npm-technitium-safe-sync`. Activation requires a public repository, GitHub publishing secret and explicit enable variable. Published stable GitHub releases map to matching numeric Docker Hub tags; `latest` follows GitHub Latest; development `main` images are kept in GHCR only. A separate release-sync workflow preserves existing released digests when copying older versions. See [DOCKERHUB.md](DOCKERHUB.md) for setup, verification and deployment. A configured destination does not establish that an image has been published.

## GHCR permissions and release workflow

Public source does **not** automatically make the GHCR package public. The package owner must set its package visibility to **Public**. Anonymous manifest/pull checks are required before advertising public access. A public image can be pulled without `docker login`.

For a private copy, authenticate with a GitHub classic PAT limited to `read:packages`, and grant the account package read access (authorize organization SSO where applicable). Send the PAT through `docker login ghcr.io -u YOUR_LOGIN --password-stdin`, never a command-line password argument. Do not use registry credentials as NPM/Technitium credentials.

GitHub Actions uses its short-lived `GITHUB_TOKEN` with `contents:read` and, only for the publish job, `packages:write`. No personal package-publishing token or management secrets are needed. The OCI source label links the package to the repository; the repository must have Actions package access. Pull requests build/test without publishing. Actions are pinned to verified commit SHAs and the base image to its registry digest; Dependabot proposes updates weekly. Merge dependency updates only after checks pass.

On main/tag pushes, the workflow runs unit and local-HTTP integration tests inside a nonroot container without external network, validates Compose, and runs disposable real-app checks. Both test jobs must pass before it builds/pushes and pulls the published digest to test image configuration and Compose pull. Provenance and SBOM are attached. The base-image pin needs regular reviewed updates to receive security fixes; an SBOM alone is not a vulnerability scan.

## Operations

- Persist `/data/state.json` on a local filesystem with working advisory locks. Protect and back up it and zone data. Do not delete state while running or copy it between different DNS servers.
- Health reflects a successful **read/reconcile cycle**, including dry-run. A heartbeat older than `max(120, 3 Ã— interval)` seconds is unhealthy. It is not a DNS resolution probe. Docker marks unhealthy containers but does not automatically restart them solely for health failure. Monitor health, cycle errors and conflict counts externally.
- Configuration errors exit nonzero. API failures log locally authored safe reasons, or an error class for unexpected exceptions, then retry at the interval. Config, credentials and CA files are loaded at startup; recreate after rotation/change. SIGTERM/SIGINT interrupts the polling wait and stops further writes at cooperative checkpoints; an in-flight request can complete first.
- The filesystem is read-only except the state mount and temporary directory; UID 10001, all capabilities dropped, no-new-privileges, bounded memory/processes and rotated logs. No ports or Docker socket are exposed.
- To release, push a new main commit, wait for checks, then push a new `vMAJOR.MINOR.PATCH` tag at that tested commit. The workflow builds and verifies its own published digest. Tag publishing is authorized only for trusted maintainers; protect main and release tags in repository settings according to your policy.

## Development and source review

Python standard library only; no runtime pip dependencies. Linux is required for `fcntl` locking.

```sh
python -m unittest discover -s tests -v
docker build -t sync:test .
sh tests/container-smoke.sh sync:test
```

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for source findings, repairs, coverage and remaining limits. This project is intended for staged operational deployment, not a certification that any unknown live environment is safe.

Upstream references:
- [Technitium HTTP API](https://github.com/TechnitiumSoftware/DnsServer/blob/master/APIDOCS.md)
- [NPM proxy host API schema](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/develop/backend/schema/paths/nginx/proxy-hosts/get.json)
- [GitHub GHCR authentication and image permissions](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub Actions image publishing](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
