# Integration and operational review — 2026-11-11

Scope: runtime reconciliation, ownership state, both API contracts, container
operation and publication checks. No production endpoint or credential was used.
This is verified behavior within a defined scope, not certification of every
possible deployment.

## Real application pins

| Application | Version | Image index digest |
| --- | --- | --- |
| Nginx Proxy Manager | 2.16.0 | `sha256:4393e642e233e5efd5adeb7f10918d4aaea71c9ed875b94190273fc8e87f9594` |
| Technitium DNS Server | 15.6.2 | `sha256:eb48ac5c135f4ab936b536f74f853d53e15ab39ef37fdbc9598a089b90d9f245` |

These were the current upstream releases resolved for this review. A server set
to `latest` may still run an older cached image; future releases need retesting.
CI starts fresh containers on an internal Docker network without published
ports. Administrators prepare synthetic fixtures; reconciliation uses a view-only
NPM account and a Technitium account limited to one zone. Test passwords are
disposable and public. Production secrets are never passed into the job.

## Repairs in 1.0.2

| Defect | Repair and regression evidence |
| --- | --- |
| DNS writes preceded detection of unwritable state | Persist state before mutations; fsync the directory after atomic replacement. Injected storage failure results in zero DNS mutations. |
| Removed zones consumed the current deletion cap | Retain/report out-of-scope ownership without charging it against permitted-zone deletion. Six foreign names no longer block one permitted removal. |
| Stop requests allowed further writes in a cycle | Check between API stages/items and after the final reread. Stop after the first mutation retains its ownership and prevents a second mutation. |
| Empty host domain lists were accepted with another nonempty host | Abort malformed source before reading DNS or changing state, preserving existing deletion timers. Both enabled and disabled entries are covered. |
| Oversized settings passed validation | Reject intervals above the runtime wait limit and TTL above 2,147,483,647 before API access. |
| Generic error classes concealed useful failure reasons | Log locally authored safe messages; suppress arbitrary remote exception text. Tests cover 401/403/429/500, invalid-token, error, invalid/oversized JSON. HTTP error responses are explicitly closed. |

State format version 1, ownership marker and conservative defaults are unchanged.

## Coverage matrix

| Case | Evidence |
| --- | --- |
| NPM authentication and visibility | Actual NPM view-only account sees all intended hosts and is denied proxy-host creation |
| Technitium authentication | Bearer and token-in-form against actual 15.6.2; older releases remain unverified |
| Least privilege | View/Modify permits create/update; denied deletion preserves the record; granting Delete permits removal; unrelated zone access is denied |
| Source hostnames | Actual enabled/disabled hosts, aliases, apex/wildcard/out-of-scope exclusion; local duplicate/normalization/malformed/empty-source regressions |
| Create and update | Actual API metadata and authoritative UDP A answers; IP and TTL changes |
| Proxy traffic | Resolve A to NPM, send HTTP with that hostname, verify backend response |
| Restart and idempotence | Reload persisted ownership each cycle; repeated cycle adds nothing |
| Manual records | Actual CNAME/TXT/mixed A+AAAA/changed comment; local disabled/multiple/changed-IP cases |
| Deletion | Actual disabled source, grace, explicit deletion and authoritative absence; local cap/returning-source timer/removed-zone cases |
| Multiple zones | Invalid second zone prevents all mutations; overlapping configuration is rejected |
| Alias/delegation | Local initial/final rereads for NS/FWD/CNAME/DNAME, apex aliases and normal SOA/NS/FWD |
| Forwarder zones (1.0.5) | Actual Forwarder zone: local override create, authoritative answer, IP update, grace deletion, subtree FWD refusal, apex forwarding rule preserved; local replica-type refusal |
| Failure recovery | Local HTTP/redirect/schema/size/corrupt-state/storage tests; successful first-write persistence, retry, orphan refusal |
| Container operation | CI nonroot/read-only/no-external-network local tests, startup smoke, lock, health and Compose validation |
| Distribution | Both test jobs gate publication; published image checks include anonymous pulls and amd64/arm64 startup |

Grace tests advance fixture timestamps instead of waiting five minutes. Restart
checks reload state; they do not simulate physical power loss. The real API suite
uses isolated HTTP and does not verify your TLS chain, firewall or reverse proxy.

## Limits and recovery

- Supported synchronization is IPv4 A records for enabled NPM proxy hosts beneath
  allowed enabled Primary or Forwarder zones. Wildcards, apex names, generated AAAA, redirects,
  404 hosts, streams and upstream-service IPs are outside scope. Missing owned
  records and existing unowned records are preserved as conflicts.
- A nonempty partial NPM source cannot reliably be distinguished from removals.
  Verify the account's visibility and keep deletion disabled for initial rollout.
- Technitium offers no conditional-write transaction here. Manual edits can race
  the final reread. Use one writer and dedicated unsigned zones. Separately hosted
  child zones cannot be discovered with a parent-only token.
- Preflight cannot prevent a lost response, later disk failure, or crash between
  remote success and state commit. Stop, back up DNS and state, compare the exact
  discrepancy with logs and resolve deliberately. Never delete all state or
  automatically adopt every matching marker. Orphaned or uncertain records fail
  closed. Earlier writes can survive a failed cycle; reconciliation is not atomic.
- Cancellation is cooperative. An in-flight request may complete first; a
  successful write is committed before stopping. The 12-second socket timeout
  limits inactivity, not total cycle duration. Force kill can interrupt a write.
- Responses above 4 MB fail closed. Every mutation rereads its target zone
  and commits full state. Large batches can be expensive; no large-fleet
  throughput or HA claim is made.
- Health means a completed cycle, including dry-run and conflicts. It is not proof
  that every requested name is managed or resolves. Monitor conflicts and errors.

Before production writes validate exact running versions, network/TLS access,
source visibility, persistent storage, backups and a staging lifecycle with the
published digest. Keep deletions off initially. See README.md for deployment.

## Evidence

[First successful real-app and publication run](https://github.com/dr-rei/npm-technitium-safe-sync/actions/runs/42973710739)
passed 38 local tests and 34 actual-app checks. The expanded suite adds permission
denials, malformed-source, setting bounds, multi-zone and recovery checks. Final
counts, commit and published digests are recorded in release evidence.

An independent read-only reviewer reproduced the empty-list deletion path and
configuration overflow and checked ownership/recovery paths. Final repairs and
integration were checked by the primary maintainer.
