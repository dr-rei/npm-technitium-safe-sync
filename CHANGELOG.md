# Changelog

## 1.0.7

- Add `FORWARDER_HTTPS_RECORDS` (default `true`) to turn the Forwarder-zone HTTPS companion records on or off. `false` creates none and removes the companions this tool created (after a fresh reread proves they are unchanged); re-enabling adds them back. Manual HTTPS records are never touched.

## 1.0.6

- An explicit Technitium rejection of one write (status `error`, nothing applied) no longer stops the other names. A name inside a separate, unlisted zone is a conflict with a specific reason; any other rejection (for example missing permission) is skipped too, but the cycle then reports failed so the healthcheck shows it. Transport, TLS and token failures still abort immediately. Remote error text is classified, never logged.
- Allow nested zones in `DNS_ZONES` (for example `abhatigroup.com,internal.abhatigroup.com`). Names route to the most specific zone; snapshots ignore parent records shadowed by a listed nested zone; a listed zone's apex is never managed, and earlier ownership of such a name is released with a warning.
- Fix `ERR_QUIC_PROTOCOL_ERROR` in Forwarder zones: each managed name also gets a local HTTPS (SVCB type 65) record advertising only `h2,http/1.1`, replacing forwarded HTTP/3 and public address hints. It is owned, updated and deleted with the A record; existing managed names receive it after upgrade. Primary zones are unchanged.
- Add `--list-owned`, `--forget-missing`, `--forget NAME_OR_PATTERN...` and `--forget-all` to inspect and release ownership after manual DNS changes; they edit only the state file and require the stopped service (state lock).
- An owned name now inside a listed nested zone is released when its old parent-zone record is detected as shadowed, so it is recreated in the nested zone instead of conflicting forever.
- Startup errors show the locally authored reason (for example `Missing required setting: NPM_URL`) instead of only the error class.
- Real-app CI covers an unlisted nested zone, a listed nested Primary zone and authoritative HTTPS answers without HTTP/3, and recovery of a hand-deleted record with `--forget-missing` against Technitium 15.6.2.

## 1.0.5

- Support Technitium Forwarder (conditional-forwarder) zones in addition to Primary zones, for split DNS: NPM hostnames become local A overrides and all other names and record types are still forwarded.
- Treat a `FWD` record below the zone apex like an NS delegation on both the initial snapshot and the pre-write reread; apex forwarding stays allowed and untouched.
- A delegation, forwarding rule or alias added above an owned name now skips that name's deletion as a conflict instead of failing the whole cycle (and the healthcheck) every interval.
- Keep refusing replica zone types (Secondary, SecondaryForwarder, Stub, Catalog) with an updated message.
- Real-app CI adds a Forwarder-zone lifecycle against Technitium 15.6.2; document zone-type choice, the forwarded-AAAA caveat and that upstream-only names are not visible to the no-adoption check.

## 1.0.4

- CI now upgrades from the published release pinned in the quickstart template to each candidate image on the same named volume and requires the previous release's state to survive.
- Document the update, rollback, backup and restore steps, and which Docker commands keep or destroy the state volume. Restore writes state as the service user so it stays writable.
- README adds everyday-change, credential-rotation, private-CA and uninstall instructions, aligns the version-pinning section with the update procedure and fixes the file-secret preset download step.

## 1.0.3

- Fix updates and deletions of records that clients query: Technitium rewrites `lastUsedOn` on every answer, so the pre-write reread aborted the cycle. The reread now ignores only that field; real 15.6.2 CI queries records between snapshot and reread.
- Ignore NPM hostnames that cannot be DNS names (for example `nas`, `my_app.lan`) when they lie outside `DNS_ZONES`, instead of halting all synchronization. Invalid names claiming a permitted zone still fail closed.
- Reread only the target zone before each write instead of every configured zone.
- Replace the health heartbeat atomically so the healthcheck never reads a partial timestamp.

## 1.0.2

- Preflight durable state before DNS writes and fsync the directory after atomic replacement.
- Exclude retained ownership outside current zones from the deletion cap.
- Stop between API stages and mutations while preserving successful write state.
- Reject empty NPM host domain lists, overflowing polling intervals and out-of-range TTL values.
- Report sanitized API failure reasons and close HTTP error responses.
- Gate publication on pinned real NPM 2.16.0 / Technitium 15.6.2 tests covering DNS, backend forwarding, both authentication modes and restricted permissions.
- Expand multi-zone, duplicate source, partial-write recovery and orphan regressions; document coverage in INTEGRATION_REVIEW.md.

## 1.0.1

- Reset and persist deletion timers on validated source reappearance before any DNS conflict or mutation, including later mutation failures.
- Retain zone-apex aliases and apply one ancestry guard to initial and fresh snapshots, blocking apex DNAME/CNAME while preserving normal SOA/NS behavior.
- Add five integration regression methods covering all three ancestor blocker types, fresh apex aliases, normal apex records and durable timer reset on failure.
- Document conservative upgrade handling for pending timers created by v1.0.0. Existing state format, marker and conservative defaults remain compatible.

## 1.0.0

Initial project-maintained GHCR release with conservative ownership, dry-run, opt-in delayed deletion and amd64/arm64 distribution.
