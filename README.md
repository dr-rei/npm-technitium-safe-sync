# NPM → Technitium Safe Sync

Poll enabled Nginx Proxy Manager proxy hosts and reconcile their hostnames to IPv4 `A` records in Technitium Primary or Forwarder (split-DNS) zones. Every managed name points to **the NPM reverse proxy IP**, not its upstream service IP. Other record types, wildcard hosts, apex names, redirection hosts, and 404 hosts are excluded.

Project-maintained container image, identical in both registries: `docker.io/drreilab/npm-technitium-safe-sync` and `ghcr.io/dr-rei/npm-technitium-safe-sync`. To deploy, follow the [production quickstart](#production-quickstart-two-files-then-up); to update, see [Update to a new version](#update-to-a-new-version). This is not affiliated with NPM or Technitium and does not carry Docker's Official Image badge.

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

For a **fresh installation** you need only two files, `compose.yaml` and `.env`.
The preset pulls the verified public **1.0.6** image by digest, creates its own
persistent storage with the correct nonroot ownership, and starts polling. No
source checkout, build, secret files, host data directory or `chown` is needed.
Already running an older version? Skip to [State, updates and backups](#state-updates-and-backups).

**What you end up with:** every *enabled* NPM proxy host whose name is inside one
of your `DNS_ZONES` gets an `A` record in Technitium that points to the NPM server.
For example, with `DNS_ZONES=abhatigroup.com` and `NPM_LAN_IP=192.168.1.10`, the
NPM hosts `erp.abhatigroup.com` and `hr.dept.abhatigroup.com` both resolve to
`192.168.1.10`. New NPM hosts appear in DNS within one polling interval (5 minutes
by default).

### 1. Check the Docker host

- Linux (for example Ubuntu) on amd64 or arm64 with Docker Engine and **Docker
  Compose v2**. Check with `docker compose version`; if it is missing, follow
  [Docker's Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/),
  including the Compose plugin.
- This can be the Technitium server, the NPM server or any other host, as long as
  it can reach both management URLs (NPM, usually port 81; Technitium, usually
  port 5380). Existing NPM and Technitium services do not need restarting.

### 2. Prepare Technitium

1. **Back up** Technitium first (Settings page, backup option).
2. **Create the zone(s).** Zones → **Add Zone**, name for example
   `apps.example.com`, and choose the type (see the table below). Repeat for each
   zone you want synchronized. The tool never creates zones; each one must exist,
   be enabled and be **Primary** or **Forwarder**.
3. **Create a dedicated user.** Administration → Users → Add User, for example
   `dns-sync`, with a strong password.
4. **Grant zone permissions.** For each zone: Zones → the zone's menu →
   **Permissions** → add `dns-sync` with **View** and **Modify**. Add **Delete**
   only if you later enable deletions. No global administrator rights are needed.
5. **Create an API token for that user.** Administration → Sessions →
   **Create Token**, choose `dns-sync`, and copy the token. Store it safely; never
   paste it into chat, tickets or Git.

**Which zone type?**

| Your domain | Zone type | What happens |
| --- | --- | --- |
| Also used on the internet (for example your company domain `abhatigroup.com`) | **Forwarder** | NPM hostnames get local A records pointing to NPM. Every other name (`www`, mail, external services) is still answered by the DNS server you forward to. Nothing has to be copied |
| Internal only (for example `apps.example.com`, `corp.lan`) | **Primary** | Technitium answers every name in the zone itself; names without a record do not exist |

To create a **Forwarder** zone: Zones → Add Zone → type **Forwarder**, and as
the forwarder enter the DNS server that should answer everything else in that
domain (your public DNS provider, or your internal/AD DNS). Technitium checks its
local records first and forwards only what it does not have. An existing
Forwarder zone works as it is.

> **HTTP/3 in Forwarder zones (fixes `ERR_QUIC_PROTOCOL_ERROR`):** browsers also
> ask DNS for an `HTTPS` record (type 65). Many public DNS providers (Cloudflare,
> for example) publish one advertising HTTP/3 and the public IP. Forwarded to
> internal clients, it makes browsers try HTTP/3 (QUIC) against NPM, which fails
> with `ERR_QUIC_PROTOCOL_ERROR`. From 1.0.6 the tool therefore also creates, for
> every name it manages in a Forwarder zone, a local `HTTPS` record advertising only
> `h2,http/1.1` with the same managed comment. It replaces the forwarded one and is
> updated and deleted together with the A record. Primary zones never forward, so
> they get none. Set `FORWARDER_HTTPS_RECORDS=false` to turn this off (the tool
> then removes the HTTPS records it created). After upgrading, flush Technitium's cache (**Cache → Flush**) and
> restart the browser, or wait for cached answers to expire.

> **IPv6 in Forwarder zones:** the tool creates only `A` (IPv4) records. Queries
> for other record types are still forwarded, so if the public DNS has an `AAAA`
> (IPv6) record for an NPM hostname, IPv6-capable clients receive that public
> address and may bypass NPM. Check each hostname with
> `nslookup -type=AAAA erp.abhatigroup.com <TECHNITIUM-IP>`; it should return no
> address. If it returns one, remove the public AAAA record or disable IPv6 for
> that name. Primary zones are not affected.

> **Upstream names are not checked in Forwarder zones:** the tool sees only the
> records stored in Technitium, not names that exist on the server you forward
> to. Every enabled NPM hostname in the zone therefore gets a local A record
> pointing to NPM, even if the upstream DNS already has that name with another
> address. For split DNS that is the goal (the public name points to your public
> IP, the internal one to NPM), but it also means an NPM host accidentally named
> like an existing server (for example `mail` or `vpn`) would redirect that name
> to NPM for internal clients. Run the first start with `DRY_RUN=true` and check
> each `PLAN ADD` name. In Primary zones, every existing name is visible and
> preserved.

The tool never changes records it did not create, including the forwarding rule.
A `FWD` record on a sub-name (forwarding a whole subtree elsewhere) is treated
like a delegation: names beneath it are skipped, and records the tool already
owns beneath it are no longer changed or deleted (logged as conflicts).

### 3. Prepare Nginx Proxy Manager

1. Users → **Add User**, for example `dns-sync@example.com`. Do not tick
   Administrator.
2. Open the new user's menu → **Permissions**: set **Item Visibility: All Items**
   and **Proxy Hosts: View Only**; everything else can be Hidden.
3. Open the user's menu → **Change Password** and set its password.

**All Items matters:** with "Created Items Only" the account sees only its own
hosts, so other users' hosts get no DNS records (and, with deletions enabled,
would look removed). NPM proxy hosts must use names inside your zones, for
example `app.apps.example.com`.

### 4. Create the two files

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
Or extract the [two-file 1.0.6 quickstart ZIP](https://github.com/dr-rei/npm-technitium-safe-sync/releases/download/v1.0.6/npm-technitium-safe-sync-1.0.6-quickstart.zip)
into a new directory; it contains only `compose.yaml` and `.env`. Edit `.env`
before starting.

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
# Verified public 1.0.6 image; no registry login or local build is needed.
SYNC_IMAGE=docker.io/drreilab/npm-technitium-safe-sync@sha256:71b1860f3e049aba51e79d78a94d774ff76b8ffeccc107fb944417c6b07019bf
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
# Forwarder zones only: also create a local HTTPS record per name that disables
# HTTP/3 (prevents ERR_QUIC_PROTOCOL_ERROR). false removes the tool's HTTPS records.
FORWARDER_HTTPS_RECORDS=true
# Change to true only for HTTP management endpoints on a trusted isolated network.
ALLOW_INSECURE_HTTP=false
```

</details>

### 5. Configure `.env`

**Required: replace these placeholders**

| Setting | Example | What to enter |
| --- | --- | --- |
| `NPM_URL` | `http://192.168.1.10:81` | NPM **admin/API** URL (the web UI address), not a proxied site |
| `NPM_IDENTITY` | `dns-sync@example.com` | Email of the NPM user from step 3 |
| `NPM_SECRET` | `'Pa$$ word#1'` | That user's password, **in single quotes** |
| `TECHNITIUM_URL` | `http://192.168.1.11:5380` | Technitium web console URL |
| `TECHNITIUM_TOKEN` | `'0123abcd…'` | API token from step 2, **in single quotes** |
| `NPM_LAN_IP` | `192.168.1.10` | IPv4 address clients use to reach **NPM** on ports 80/443. Not a backend app's IP |
| `DNS_ZONES` | `apps.example.com` | Your zone; several zones are comma-separated (see below) |

**Optional: the defaults are safe**

| Setting | Template value | Meaning |
| --- | --- | --- |
| `SYNC_IMAGE` | verified 1.0.6 digest | Image to run. Change it only when upgrading |
| `TECHNITIUM_AUTH_MODE` | `bearer` | `bearer` for Technitium 15+; `parameter` for older servers (token sent in the POST body) |
| `DNS_TTL_SECONDS` | `120` | TTL of created records, 30 or more |
| `SYNC_INTERVAL_SECONDS` | `300` | Seconds between polls, 30 or more |
| `DRY_RUN` | `false` | `true` = only log what would change. **Use `true` for your first start** |
| `ENABLE_DELETIONS` | `false` | `true` = remove records this tool created after their NPM host is gone. Keep `false` at first |
| `DELETE_GRACE_SECONDS` | `86400` | How long a host must stay absent before its record is deleted (minimum 300) |
| `MAX_DELETES_PER_RUN` | `5` | If more names than this are waiting, **no** deletions happen that cycle |
| `FORWARDER_HTTPS_RECORDS` | `true` | Forwarder zones only: also create a local `HTTPS` record per managed name that disables HTTP/3, preventing `ERR_QUIC_PROTOCOL_ERROR` (see [Which zone type?](#2-prepare-technitium)). `false` creates none and removes the ones this tool created. No effect on Primary zones |
| `ALLOW_EMPTY_NPM` | `false` | `true` only if NPM may legitimately have no matching hosts; otherwise an empty list is treated as an error |
| `ALLOW_INSECURE_HTTP` | `false` | Must be `true` when either URL starts with `http://`. Use only on a trusted LAN |

**Rules that prevent most mistakes**

- **Never use `localhost` or `127.0.0.1`** in the URLs: inside the container that
  means the container itself. Use the host's LAN IP, even when NPM or Technitium
  run on the same machine.
- **Quote credentials with single quotes** so `$`, `#`, spaces and backslashes
  stay literal. An apostrophe inside the quotes is written `\'`.
- `http://` URLs are refused unless `ALLOW_INSECURE_HTTP=true`. With HTTPS, the
  certificate must be trusted; for a private CA see
  [API compatibility and transport](#api-compatibility-and-transport).
- Use the final URL: redirects (for example HTTP → HTTPS) are refused.
- Protect `.env` (`chmod 600`), keep it out of Git and never paste it anywhere.
  This preset passes credentials as container environment variables, which Docker
  administrators can inspect; for file-mounted credentials use the
  [file-secret preset](#existing-deployment--file-secret-preset). Do not combine
  inline credentials with their `_FILE` equivalents.

**Multiple zones:** list them comma-separated, for example
`DNS_ZONES=abhatigroup.com,abhati.local,example.org`. Each NPM host goes into the
zone its name falls under, and all records point to the same `NPM_LAN_IP`.

- Every listed zone must exist as an enabled Primary or Forwarder zone (types may
  be mixed), and the token needs
  View + Modify on **each** one. If any listed zone is missing or unreadable, that
  cycle writes nothing in any zone.
- A zone may not be listed twice. Names several levels deep, such as
  `db.internal.abhatigroup.com`, are covered by the parent zone `abhatigroup.com`
  automatically, as long as Technitium has no separate zone for `internal`.
- **Nested zones:** if Technitium *does* host a separate zone for part of a domain
  (for example `internal.abhatigroup.com` next to `abhatigroup.com`), list both:
  `DNS_ZONES=abhatigroup.com,internal.abhatigroup.com`. Each name goes to the most
  specific listed zone, exactly as Technitium answers it: `db.internal.abhatigroup.com`
  is written to `internal.abhatigroup.com`, `portal.abhatigroup.com` to the parent.
  The nested zone's own name (`internal.abhatigroup.com`) is never managed. Grant
  the token View + Modify on the nested zone too. If a separate zone exists but is
  not listed, names inside it are skipped with `SKIP …: it belongs to a separate
  zone` and everything else still synchronizes.
- Zones served by different NPM servers need one deployment per NPM server, each
  on its own Docker host with its own state.

**What is never synchronized:** the zone name itself (`apps.example.com`),
wildcard hosts (`*.apps.example.com`), disabled NPM hosts, names outside
`DNS_ZONES`, redirection/404/stream hosts, and IPv6 (`AAAA`). NPM names that can
never be DNS names, such as `nas` or `my_app.lan`, are ignored with a warning.

### 6. Start in preview mode

With `DRY_RUN=true` in `.env`:

```sh
docker compose config --quiet
docker compose up -d
docker compose logs -f npm-technitium-safe-sync
```

`config --quiet` reports Compose errors without printing your credentials.
Docker pulls the pinned image, creates the named state volume and starts the first
cycle immediately. Healthy output looks like this (timestamps omitted):

```text
Read 12 NPM hosts, 11 enabled, 9 permitted unique names
PLAN ADD erp.apps.example.com A 192.168.1.10
SKIP unmanaged existing DNS name wiki.apps.example.com (manual DNS preserved)
Cycle completed: {"added": 8, "conflicts": 1, "deleted": 0, "pending": 0, "updated": 0}
```

Check that every `PLAN ADD` name and IP is what you expect. Press Ctrl+C to stop
following the log; the service keeps running.

### 7. Go live

Set `DRY_RUN=false` in `.env`, then recreate the container (a plain
`docker compose restart` does **not** reload `.env`):

```sh
docker compose up -d --force-recreate
docker compose logs --tail=50 npm-technitium-safe-sync
nslookup erp.apps.example.com 192.168.1.11
curl -I --resolve erp.apps.example.com:443:192.168.1.10 https://erp.apps.example.com/
```

The log now shows `APPLY ADD …`. `nslookup` against the Technitium IP must return
`NPM_LAN_IP`, and `curl` confirms NPM serves the site. Finally, make sure your
clients use Technitium as their DNS server (router DHCP or device settings);
otherwise they never see the new names.

`docker compose ps` shows `healthy` after the first completed cycle and the next
health probe (it can show `starting` before that). Healthy means cycles complete;
it does not prove every name resolves, so also watch `conflicts` in the log.

### 8. Troubleshooting

The service never logs passwords, tokens or raw API responses. After fixing
`.env`, always run `docker compose up -d --force-recreate`.

| Log message | Cause and fix |
| --- | --- |
| `Startup failed (<reason>)` | A setting is missing or invalid; the reason names it, for example `Missing required setting: NPM_URL`, `HTTP sends credentials in plaintext…` (set `ALLOW_INSECURE_HTTP=true` or use HTTPS), `DNS_ZONES must not repeat a zone`, or `Missing or invalid credential: NPM_SECRET`. Compare `.env` with the tables above |
| `Startup failed (Another instance holds the state lock…)` | The service is still running while you ran an admin command, or a second copy uses the same state volume. Run `docker compose stop` first, then the command, then `docker compose start` |
| `API transport, TLS, or JSON failure` | URL unreachable, wrong port, `localhost` used, firewall, or untrusted HTTPS certificate |
| `API HTTP status 4xx` (for example 400, 401, 403) | Wrong NPM email or password, wrong URL path, or insufficient permissions |
| `NPM authentication did not return a token` | The NPM login did not complete, for example because the account requires 2FA. Use a dedicated account without 2FA |
| `Technitium API rejected request: invalid-token` | Wrong, expired or deleted Technitium token |
| `API redirects are refused` | The URL redirects (for example HTTP → HTTPS). Use the final URL |
| `Technitium API rejected request: error` | A zone in `DNS_ZONES` does not exist (check spelling), or the token's user lacks View/Modify permission on it |
| `DNS zone '…' must be an enabled Primary or Forwarder zone` | The zone exists but is disabled, or is a Secondary, Secondary Forwarder, Stub or Catalog zone (read-only copies). Use the zone on the server that owns it, or convert it |
| `NPM returned no permitted active hostnames` | No enabled NPM host is inside `DNS_ZONES`, or the NPM user cannot see them (check **All Items**) |
| `SKIP unmanaged existing DNS name …` | A record you created already uses that name. It is left alone; delete it in Technitium if this tool should manage the name |
| `SKIP managed …: DNS changed or disappeared outside this service` | A record this tool created was edited or deleted by hand. It is never silently recreated or overwritten. **If you deleted it by mistake**, release it with [`--forget-missing`](#repair-ownership-after-manual-dns-changes) and the next cycle recreates it. If you edited it, restore it or release the name with `--forget` |
| `SKIP name below delegation, forwarding or alias` | A parent name has an NS, FWD, CNAME or DNAME record, so a record here would be ignored or forwarded elsewhere |
| `SKIP …: it belongs to a separate zone on this Technitium server that is not in DNS_ZONES` | Technitium hosts a separate zone for part of that name (for example `internal.abhatigroup.com`). Add it to `DNS_ZONES` and give the token View + Modify on it, then recreate. Other names are unaffected |
| `SKIP …: Technitium rejected the write`, then `Cycle FAILED (Technitium rejected N write(s)…)` | Technitium refused those records, most often because the token lacks Modify (or Delete) permission on the zone. Nothing was written for them; all other names were still processed, and the cycle reports failed so the healthcheck shows the problem |
| `Stopped managing …: it is now the apex of a zone in DNS_ZONES` | A name the tool created is now the name of a listed nested zone. The tool no longer touches it; the old parent-zone record is shadowed and can be deleted by hand |
| Browser shows `ERR_QUIC_PROTOCOL_ERROR` | HTTP/3 hints forwarded from the public DNS (Forwarder zones). Upgrade to 1.0.6 or newer, which adds the local `HTTPS` record, then flush Technitium's cache and restart the browser |
| `Cycle FAILED (…)` | A temporary API failure. The service retries after the polling interval; earlier writes in that cycle are kept |

### 9. Optional: automatic deletions

After the service has run correctly for a while and NPM visibility is confirmed:

1. Give `dns-sync` **Delete** permission on each zone in Technitium.
2. Set `ENABLE_DELETIONS=true` and recreate the container.

Only records this tool created are ever deleted, only after their host has been
absent from NPM for `DELETE_GRACE_SECONDS` (24 hours by default), and at most
`MAX_DELETES_PER_RUN` per cycle. Disabling an NPM host counts as removal. Records
in zones you remove from `DNS_ZONES` are kept and must be cleaned up by hand.

### 10. Everyday changes

Settings are read only when the container starts. After **any** `.env` change run
this from the deployment folder (a plain `docker compose restart` keeps the old
values):

```sh
docker compose up -d --force-recreate
```

| Task | What to do |
| --- | --- |
| Add a site | Create the proxy host in NPM with a name inside `DNS_ZONES`. Its record appears within one polling interval; nothing to change here |
| Change the NPM server IP | Set the new `NPM_LAN_IP` and recreate. Every record this tool created is updated to the new IP |
| Change TTL or polling interval | Edit `DNS_TTL_SECONDS` / `SYNC_INTERVAL_SECONDS` and recreate. Existing records get the new TTL |
| Add a zone | Create it in Technitium, grant `dns-sync` permission on it, append it to `DNS_ZONES`, recreate |
| Remove a zone | Remove it from `DNS_ZONES` and recreate. Records already created there are kept; delete them by hand if unwanted |
| Change the NPM password | Change it in NPM, put the new value in `NPM_SECRET`, recreate |
| Replace the Technitium token | Create a new token (Administration → Sessions), put it in `TECHNITIUM_TOKEN`, recreate, confirm a `Cycle completed` line, then delete the old token in Sessions |
| Preview changes again | Set `DRY_RUN=true` and recreate. Nothing is written until you set it back to `false` |
| Pause syncing | `docker compose stop`; resume with `docker compose start`. DNS records stay as they are |
| See what the tool manages | In Technitium, records it created (A records, plus `HTTPS` records in Forwarder zones) carry the comment `managed-by:npm-technitium-safe-sync/v1`. Do not add that comment to your own records: the tool still will not treat them as its own |
| Turn the HTTP/3 fix on or off | Set `FORWARDER_HTTPS_RECORDS=true` or `false` and recreate. `false` removes the tool's HTTPS records within one cycle; `true` adds them back. Flush Technitium's cache afterwards |
| Recreate records deleted by mistake | Stop the service and run `--forget-missing`; see [Repair ownership](#repair-ownership-after-manual-dns-changes) |
| Update the tool | See [Update to a new version](#update-to-a-new-version) |

#### Repair ownership after manual DNS changes

The tool remembers which names it created (see `--list-owned`). If you delete or
edit one of its records by hand, it logs `SKIP managed …` every cycle and never
recreates or overwrites it by itself. To hand a name back to the tool, release it.
These commands change only the tool's state file, never DNS, and need the service
stopped:

```sh
cd ~/npm-technitium-safe-sync
docker compose stop
docker compose run --rm npm-technitium-safe-sync --list-owned
docker compose run --rm npm-technitium-safe-sync --forget-missing
docker compose start
docker compose logs --tail=50 npm-technitium-safe-sync
```

| Command | Releases |
| --- | --- |
| `--list-owned` | Nothing; lists every owned name and its IP |
| `--forget-missing` | Owned names that have **no DNS record left** (deleted by hand). Safest choice; the next cycle recreates them |
| `--forget NAME_OR_PATTERN…` | Specific names or wildcards, for example `--forget api-sac.abhatigroup.com` or `--forget '*.internal.abhatigroup.com'` (quote patterns) |
| `--forget-all` | Every owned name |

After a release, the next cycle creates a released name **only if it has no DNS
record**. A record that still exists (for example one you edited) is then treated
like any manual record and left alone; delete it in Technitium first if the tool
should recreate it. Back up the state before `--forget-all` (see below).

**HTTPS with a private certificate authority.** If NPM or Technitium uses a
certificate signed by your own CA, copy the CA certificate (PEM format) into the
deployment folder as `ca.pem`, make it readable (`chmod 644 ca.pem`), and add one
line under `volumes:` in `compose.yaml`:

```yaml
    volumes:
      - sync_data:/data
      - ./ca.pem:/certs/ca.pem:ro
```

Then add `SSL_CERT_FILE=/certs/ca.pem` to `.env` and recreate. This file replaces
the built-in public CA list, so if one endpoint uses a public certificate and the
other a private one, put both CA certificates in `ca.pem`. Certificate checking is
never switched off. Keep the added line when you re-download `compose.yaml`.

**Uninstall.** Removing the tool does not remove the DNS records it created; they
keep resolving until you delete them.

1. Optionally delete the records first: in Technitium, remove the records (A, and
   HTTPS in Forwarder zones) with the comment `managed-by:npm-technitium-safe-sync/v1`.
2. Stop and remove the container: `docker compose down`. The state volume is kept,
   so reinstalling later picks up where it left off.
3. To remove everything, including the state: `docker compose down -v`, then
   delete the deployment folder. Do this only after step 1 or if you will manage
   those records by hand: a fresh install will not recognize them as its own.
4. Remove the `dns-sync` user and token in Technitium and the `dns-sync` user in
   NPM.

### State, updates and backups

#### Where your data lives

Everything the service must remember is in one Docker volume,
`npm-technitium-safe-sync_sync_data`, mounted at `/data`. The most important file
is `state.json`: the list of DNS records **this tool created**. Without it, the
tool treats its own earlier records as manual DNS and leaves them alone (logged
as `SKIP unmanaged existing DNS name`) instead of updating or deleting them.
Your settings live in `.env` next to `compose.yaml`.

The volume name comes from the fixed `name:` in `compose.yaml`, not the folder
name, so it stays the same when you update or re-download the files. Run one
deployment per Docker host.

| Safe: data is kept | Destroys the data |
| --- | --- |
| `docker compose up -d --force-recreate`, `pull`, `restart`, `stop`/`start` | `docker compose down -v` (or `--volumes`) |
| `docker compose down` followed by `up -d` | `docker volume rm` of this volume |
| Updating or rolling back the image | `docker volume prune` or `docker system prune --volumes` **while the service is down** |
| Editing `.env`, re-downloading `compose.yaml` | Changing `name:` in `compose.yaml` |
| Host reboot (the service restarts by itself) | Moving to a new host without restoring a backup |

Recreation, `down`/`up`, an `.env` change, an upgrade from 1.0.2 to 1.0.3 and a
rollback were tested against real NPM and Technitium: no records were re-added,
lost or turned into conflicts. CI repeats the upgrade check for every release.

#### Update to a new version

1. Open the new [GitHub release](https://github.com/dr-rei/npm-technitium-safe-sync/releases)
   and copy its **Immutable production pin** (`docker.io/drreilab/npm-technitium-safe-sync@sha256:…`).
   Read its notes for any special upgrade steps.
2. Optionally back up first (see below).
3. Update, replacing `NEW_IMAGE` with the copied value:

```sh
cd ~/npm-technitium-safe-sync
sed -i 's|^SYNC_IMAGE=.*|SYNC_IMAGE=NEW_IMAGE|' .env
docker compose pull
docker compose up -d --force-recreate
docker compose logs --tail=50 npm-technitium-safe-sync
```

4. Confirm a new `Cycle completed` line. If nothing changed in NPM, expect
   `"added": 0` and `"conflicts": 0`, which shows the existing records are still
   owned.

Only `SYNC_IMAGE` changes; `.env` settings and the state volume carry over. A
version tag such as `docker.io/drreilab/npm-technitium-safe-sync:1.0.6` also
works and is easier to read; the digest guarantees the exact verified image.
Avoid `latest` in production, because it changes without you choosing to update.

**Roll back** the same way using the previous release's image. All 1.0.x
releases use the same state format (version 1); rolling back from 1.0.3 to 1.0.2
was tested. A rollback does not undo DNS changes already made.

#### Back up and restore

Back up the state together with a Technitium backup, so both describe the same
moment:

```sh
cd ~/npm-technitium-safe-sync
docker compose stop
backup_dir="state-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
docker compose cp npm-technitium-safe-sync:/data/. "$backup_dir/"
docker compose start
```

Restore (for example on a new host, after creating `compose.yaml` and `.env` and
running `docker compose up -d` once):

```sh
cd ~/npm-technitium-safe-sync
docker compose stop
docker compose run --rm -T --no-deps --entrypoint sh npm-technitium-safe-sync -c 'cat > /data/state.json' < state-backup-YYYYMMDD-HHMMSS/state.json
docker compose start
```

This writes the file as the service's own user, so it stays writable. Do not
restore with `docker compose cp` into the container: copied files belong to root
and the service could no longer update them. Restore DNS and state from the same
backup; a state file that does not match the zone shows up as conflicts.

**Already deployed with `./data` and file secrets?** Keep the existing
`compose.yaml`, `.env`, `data/` and `secrets/` setup. It remains supported. Its
state is the `data/` folder: update with the same `sed`, `pull` and `up -d
--force-recreate` steps, and back up with `sudo cp -a data <backup-folder>` while the
service is stopped (`-a` keeps the UID 10001 ownership). Do not switch to the quickstart named volume without a planned,
stopped-service state migration. The two presets are alternatives, not Compose
files to merge.

## Existing deployment / file-secret preset

Requirements: Linux Docker Engine, Docker Compose v2, amd64 or arm64, and connectivity from the container to the management endpoints. Existing NPM and Technitium containers do not need restarting. The original `compose.yaml` uses file secrets and the existing `./data` bind mount; it pulls an image and contains no build instruction.

1. Download and extract the selected release's deployment ZIP (it contains `compose.yaml` and `deployment.env.example`), or clone this repository and check out its version tag (`compose.yaml` and `.env.example`). For 1.0.6:

```sh
curl --fail --location --remote-name https://github.com/dr-rei/npm-technitium-safe-sync/releases/download/v1.0.6/npm-technitium-safe-sync-1.0.6-deployment.zip
unzip npm-technitium-safe-sync-1.0.6-deployment.zip
```

If `unzip` is missing on Ubuntu, install it with `sudo apt install unzip`.

2. Back up your Technitium zone. Create an enabled **Primary** or **Forwarder** zone such as `apps.example.com` (see [Which zone type?](#2-prepare-technitium)). Do not configure overlapping `DNS_ZONES`, zone apex hostnames, or separately hosted subzones.
3. Create a dedicated NPM account with permission to view **all intended proxy hosts**. A restricted account that sees only some hosts is an incomplete source and could make others look stale. Verify `GET /api/nginx/proxy-hosts` with this account. Recheck access whenever ownership or permissions change. Keep deletion disabled until source visibility is established.
4. Create a dedicated Technitium user/token with **View + Modify** on the permitted zones only. Grant zone **Delete** only if enabling deletion. No global administrator token is required. Rotate these credentials using the same scoped accounts.
5. Prepare the deployment directory on the Linux Docker host:

```sh
mkdir -p data secrets
sudo chown 10001:10001 data
sudo chmod 700 data
cp deployment.env.example .env   # or .env.example from a source checkout
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

## Nested zones, HTTP/3 and per-name failures in 1.0.6

Version 1.0.6 fixes three issues seen with a Forwarder zone in production:

- **One rejected name no longer stops the cycle.** When Technitium explicitly
  refuses a write (for example a name inside a separate zone, or missing
  permission), nothing was applied, so that name is skipped as a conflict with a
  specific reason and all other names continue. Uncertain failures (timeouts, TLS,
  invalid token) still stop the cycle.
- **Nested zones** such as `abhatigroup.com,internal.abhatigroup.com` can be listed
  together; each name goes to its most specific zone.
- **`ERR_QUIC_PROTOCOL_ERROR` fix:** in Forwarder zones each managed name also gets
  a local `HTTPS` record advertising only HTTP/2 and HTTP/1.1, replacing forwarded
  HTTP/3 hints. Existing managed names receive it on the first cycle after upgrade.
  Its TTL is set when it is created; a later `DNS_TTL_SECONDS` change updates only
  the A records.
- **Ownership repair commands** `--list-owned`, `--forget-missing`, `--forget` and
  `--forget-all` hand names back to the tool after manual DNS changes, so records
  deleted by mistake are recreated on the next cycle.
- **Clearer startup errors:** the reason (for example `Missing required setting:
  NPM_URL`) is shown instead of only an error class.

State format, ownership marker and defaults are unchanged. Upgrade by replacing
`SYNC_IMAGE` as described in [Update to a new version](#update-to-a-new-version).

## Forwarder zones in 1.0.5

Version 1.0.5 adds Technitium **Forwarder** (conditional-forwarder) zones next to
Primary zones, for split DNS on a public domain: NPM hostnames get local A
overrides and everything else is forwarded. A `FWD` record below the zone apex is
treated like a delegation. Replica zones (Secondary, Secondary Forwarder, Stub,
Catalog) remain refused. Primary-zone behavior, state format, ownership marker and
defaults are unchanged; upgrade by replacing `SYNC_IMAGE` as described in
[Update to a new version](#update-to-a-new-version).

## Upgrade assurance in 1.0.4

Version 1.0.4 has the same synchronization code as 1.0.3. CI now upgrades from the previously published release to every candidate image on the same state volume and requires the old state to survive. The README adds complete update, rollback, backup/restore, everyday-change, credential-rotation, private-CA and uninstall instructions. State format, ownership marker and defaults are unchanged; upgrade by replacing `SYNC_IMAGE` as described in [Update to a new version](#update-to-a-new-version).

## Reliability repairs in 1.0.3

Version 1.0.3 fixes updates and deletions of records that DNS clients query: Technitium rewrites each record's `lastUsedOn` on every answer, which the pre-write reread treated as a concurrent edit, aborting the cycle. NPM hostnames that cannot be DNS names (such as `nas` or `my_app.lan`) outside `DNS_ZONES` no longer halt synchronization. Each write now rereads only its target zone, and the health heartbeat is replaced atomically. State format, ownership marker and defaults are unchanged; upgrade by replacing the image digest and keeping the existing state mount.

## Operational repairs in 1.0.2

Version 1.0.2 repairs state-storage preflight, deletion accounting after zone removal, in-cycle shutdown, malformed empty host lists and oversized settings. It also adds actionable safe API diagnostics and requires real-app tests before image publication. See [INTEGRATION_REVIEW.md](INTEGRATION_REVIEW.md). Back up zones and state, stage the new digest with deletion off, and review conflicts before production writes.

## Audit repairs in 1.0.1

Version 1.0.1 repairs the two operational defects identified in the full v1.0.0 audit: source reappearance now durably clears an old deletion timer before DNS conflicts/writes, and both initial and fresh-read guards now detect zone-apex DNAME/CNAME aliases while allowing ordinary apex NS/SOA records. See [AUDIT_REMEDIATION.md](AUDIT_REMEDIATION.md) for the regression cases and remaining production acceptance gates.

When upgrading from 1.0.0, first disable deletions and back up zone + state. The fix cannot reconstruct presence history missed by the old version. Treat old pending removals conservatively: observe successful upgraded polls throughout at least DELETE_GRACE_SECONDS with deletion disabled before considering re-enabling it. Review pending removals and staging results first. State format and record ownership marker remain compatible.

## Version pinning, updates and rollback

For the step-by-step procedure see [Update to a new version](#update-to-a-new-version);
this section explains the tags.

Set `SYNC_IMAGE` in `.env` to a release digest from the GitHub release notes or
`RELEASE_EVIDENCE.md`. Docker Hub and GHCR hold the same verified index, so
`docker.io/drreilab/npm-technitium-safe-sync@sha256:<digest>` and
`ghcr.io/dr-rei/npm-technitium-safe-sync@sha256:<digest>` are interchangeable.
Version tags such as `:1.0.6` are readable and never intentionally re-pushed; a
digest pins the exact image content. `:main` tracks development and is not a
production pin. GHCR has no floating `latest`; Docker Hub `latest` follows the
GitHub Latest stable release and is not recommended for production.

To check what a tag currently points to and which platforms it contains:

```sh
docker buildx imagetools inspect docker.io/drreilab/npm-technitium-safe-sync:1.0.6
```

Every image supports `linux/amd64` and `linux/arm64`. Main pushes publish `:main`
to GHCR only. Version tags publish `:MAJOR.MINOR.PATCH` to both registries. No
commit-hash tags are created. Releases always use new version numbers; an existing
release is never retagged.

Back up zone + state before an update. Roll back by setting the previous verified
digest and recreating the container. Rolling back an image does not undo DNS
writes; restore DNS/state together only after stopping the service and reviewing
the backup. Stop writes quickly with `docker compose stop`.

## Docker Hub distribution

The workflow also supports the configured Docker Hub destination `docker.io/drreilab/npm-technitium-safe-sync`. Activation requires a public repository, GitHub publishing secret and explicit enable variable. Published stable GitHub releases map to matching numeric Docker Hub tags; `latest` follows GitHub Latest; development `main` images are kept in GHCR only. A separate release-sync workflow preserves existing released digests when copying older versions. See [DOCKERHUB.md](DOCKERHUB.md) for setup, verification and deployment. A configured destination does not establish that an image has been published.

## GHCR permissions and release workflow

Public source does **not** automatically make the GHCR package public. The package owner must set its package visibility to **Public**. Anonymous manifest/pull checks are required before advertising public access. A public image can be pulled without `docker login`.

For a private copy, authenticate with a GitHub classic PAT limited to `read:packages`, and grant the account package read access (authorize organization SSO where applicable). Send the PAT through `docker login ghcr.io -u YOUR_LOGIN --password-stdin`, never a command-line password argument. Do not use registry credentials as NPM/Technitium credentials.

GitHub Actions uses its short-lived `GITHUB_TOKEN` with `contents:read` and, only for the publish job, `packages:write`. No personal package-publishing token or management secrets are needed. The OCI source label links the package to the repository; the repository must have Actions package access. Pull requests build/test without publishing. Actions are pinned to verified commit SHAs and the base image to its registry digest; Dependabot proposes updates weekly. Merge dependency updates only after checks pass.

On main/tag pushes, the workflow runs unit and local-HTTP integration tests inside a nonroot container without external network, validates Compose, and runs disposable real-app checks. Both test jobs must pass before it builds/pushes and pulls the published digest to test image configuration and Compose pull. Provenance and SBOM are attached. The base-image pin needs regular reviewed updates to receive security fixes; an SBOM alone is not a vulnerability scan.

## Operations

- Persist `/data/state.json` on a local filesystem with working advisory locks. Protect and back up it and zone data. Do not delete state while running or copy it between different DNS servers.
- Health reflects a successful **read/reconcile cycle**, including dry-run. A heartbeat older than `max(120, 3 × interval)` seconds is unhealthy. It is not a DNS resolution probe. Docker marks unhealthy containers but does not automatically restart them solely for health failure. Monitor health, cycle errors and conflict counts externally.
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
