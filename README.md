# NPM â†’ Technitium Safe Sync

Poll enabled Nginx Proxy Manager proxy hosts and reconcile their hostnames to IPv4 `A` records in dedicated Technitium Primary zones. Every managed name points to **the NPM reverse proxy IP**, not its upstream service IP. Other record types, wildcard hosts, apex names, redirection hosts, and 404 hosts are excluded.

Project-maintained container image: `ghcr.io/dr-rei/npm-technitium-safe-sync`. This is not affiliated with NPM or Technitium and does not carry Docker's Official Image badge.

## Publication and validation

Images are published from main and version tags. Use the selected GitHub release and its attached `RELEASE_EVIDENCE.md` to confirm the exact digest, workflow result and anonymous access checks. A prepared workflow or template alone is not evidence that a version was published.

The tests use simulated local HTTP APIs. They do not establish compatibility with your live servers. Complete the staging procedure below before production writes. The synchronizer never needs your real credentials in GitHub Actions.

## Safety contract

- `DRY_RUN=true`, `ENABLE_DELETIONS=false`, `ALLOW_EMPTY_NPM=false` by default.
- Read and validate all NPM hosts and permitted zones before writing. Malformed, unavailable, oversized, or empty source responses abort the cycle. Booleans and zone configuration are validated strictly.
- Never adopt an existing DNS name, even if it has the same IP or a managed comment. Ownership requires persisted state **and** the exact managed comment, one enabled A record, and the previous IP. A changed comment, disabled record, multiple records, or mixed types causes a conflict.
- Before each mutation, read the DNS snapshot again and refuse the write if the target changed. Refuse names below NS delegations, DNAMEs or CNAMEs. Use dedicated zones without separately hosted child zones; separate child zones are not discoverable through the permitted parent-zone API.
- Deletions require opt-in, ownership checks, a grace period (24 hours default) and a cap (five stale names default). Exceeding the cap blocks **all** deletions for that cycle. `ALLOW_EMPTY_NPM=true` is separately required to remove the final host.
- State writes use flush/fsync and atomic rename. A local lock prevents two processes using the same state directory. Back up state and zones together.
- API timeouts, response-size limits, certificate verification, disabled redirects, and disabled environment HTTP proxies protect management credentials. HTTPS is required unless plaintext HTTP is explicitly opted into on an isolated trusted network. API bodies, tokens, passwords and raw remote errors are not logged.

**Limits:** Technitium provides no conditional-write/transaction contract for these endpoints. A change between the final read and write remains possible. Run exactly one synchronizer per zone, use one persistent local state volume, and coordinate manual edits. Multiple independent state volumes are not safe HA. A write followed by a crash before state persistence can leave an orphan; inspect it manually, as the service refuses automatic adoption. A failed cycle can contain earlier successful writes; it is not a transaction. Missing owned records are conflicts and are not silently recreated. DNSSEC-generated records can cause mixed-type conflicts; use a dedicated unsigned staging/production zone for this implementation. Large zones above the 4 MB API response limit fail closed.

## Deploy the published image

Requirements: Linux Docker Engine, Docker Compose v2, amd64 or arm64, and connectivity from the container to the management endpoints. Existing NPM and Technitium containers do not need restarting. `compose.yaml` pulls an image and contains no build instruction.

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

`TECHNITIUM_AUTH_MODE=bearer` targets Technitium **15+**, whose API documents bearer authentication. For older Technitium versions use `parameter`; the token is sent in a POST form body, never a query URL. All Technitium calls use POST forms. Both authentication modes have local HTTP fixture coverage; compatibility with your installed release must be checked in staging.

Use trusted HTTPS certificates, final URLs without redirects, and firewalls limiting management-port access. HTTP requires `ALLOW_INSECURE_HTTP=true` and sends credentials in plaintext. For a private CA, mount a CA bundle read-only and set `SSL_CERT_FILE` to its container path; do not disable TLS verification. Ambient HTTP proxy variables are intentionally ignored. When using host management endpoints from Docker, `localhost` refers to this new container; use reachable host/IP addresses or appropriate Docker networks.

## Audit repairs in 1.0.1

Version 1.0.1 repairs the two operational defects identified in the full v1.0.0 audit: source reappearance now durably clears an old deletion timer before DNS conflicts/writes, and both initial and fresh-read guards now detect zone-apex DNAME/CNAME aliases while allowing ordinary apex NS/SOA records. See [AUDIT_REMEDIATION.md](AUDIT_REMEDIATION.md) for the regression cases and remaining production acceptance gates.

When upgrading from 1.0.0, first disable deletions and back up zone + state. The fix cannot reconstruct presence history missed by the old version. Treat old pending removals conservatively: observe successful upgraded polls throughout at least DELETE_GRACE_SECONDS with deletion disabled before considering re-enabling it. Review pending removals and staging results first. State format and record ownership marker remain compatible.

## Version pinning, updates and rollback

Production should set `SYNC_IMAGE=ghcr.io/dr-rei/npm-technitium-safe-sync@sha256:<verified-index-digest>` in `.env`. The release evidence includes an actual digest after publication. Version tag `:1.0.1` is readable but tags can be moved; a digest pins exact image content. `:main` tracks development and is not a production pin. No floating `latest` tag is published.

The workflow builds `linux/amd64` and `linux/arm64`, publishes `:main` for main pushes, `:MAJOR.MINOR.PATCH` for `vMAJOR.MINOR.PATCH` tags, plus full `:sha-<commit>` tags. SHA tags may refer to the most recent build for that commit (e.g. main versus a release); use a digest for immutability. Releases must use new version numbers; never retag an existing release.

```sh
# Verify the selected version, copy its digest into .env, then pull it.
docker buildx imagetools inspect ghcr.io/dr-rei/npm-technitium-safe-sync:1.0.1
docker compose pull
docker compose up -d --force-recreate
```

Back up zone + state before an update. Roll back by setting the previous verified digest and recreating the container. Rolling back an image does not undo DNS writes; restore DNS/state together only after stopping the service and reviewing the backup. Stop writes quickly with `docker compose stop`.

## Docker Hub distribution

The workflow also supports the configured Docker Hub destination `docker.io/drreilab/npm-technitium-safe-sync`. Activation requires a public repository, GitHub publishing secret and explicit enable variable. Published stable GitHub releases map to matching numeric Docker Hub tags; `latest` follows GitHub Latest and `main` remains the development channel. A separate release-sync workflow preserves existing released digests when copying older versions. See [DOCKERHUB.md](DOCKERHUB.md) for setup, verification and deployment. A configured destination does not establish that an image has been published.

## GHCR permissions and release workflow

Public source does **not** automatically make the GHCR package public. The package owner must set its package visibility to **Public**. Anonymous manifest/pull checks are required before advertising public access. A public image can be pulled without `docker login`.

For a private copy, authenticate with a GitHub classic PAT limited to `read:packages`, and grant the account package read access (authorize organization SSO where applicable). Send the PAT through `docker login ghcr.io -u YOUR_LOGIN --password-stdin`, never a command-line password argument. Do not use registry credentials as NPM/Technitium credentials.

GitHub Actions uses its short-lived `GITHUB_TOKEN` with `contents:read` and, only for the publish job, `packages:write`. No personal package-publishing token or management secrets are needed. The OCI source label links the package to the repository; the repository must have Actions package access. Pull requests build/test without publishing. Actions are pinned to verified commit SHAs and the base image to its registry digest; Dependabot proposes updates weekly. Merge dependency updates only after checks pass.

On main/tag pushes, the workflow runs unit and local-HTTP integration tests inside a nonroot container without external network, validates Compose, then builds/pushes and pulls the published digest to test image configuration and Compose pull. Provenance and SBOM are attached. The base-image pin needs regular reviewed updates to receive security fixes; an SBOM alone is not a vulnerability scan.

## Operations

- Persist `/data/state.json` on a local filesystem with working advisory locks. Protect and back up it and zone data. Do not delete state while running or copy it between different DNS servers.
- Health reflects a successful **read/reconcile cycle**, including dry-run. A heartbeat older than `max(120, 3 Ã— interval)` seconds is unhealthy. It is not a DNS resolution probe. Docker marks unhealthy containers but does not automatically restart them solely for health failure. Monitor health, cycle errors and conflict counts externally.
- Configuration errors exit nonzero. API failures log a sanitized error class and retry at the interval. Config, credentials and CA files are loaded at startup; recreate after rotation/change. Graceful SIGTERM/SIGINT interrupts the polling wait.
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
