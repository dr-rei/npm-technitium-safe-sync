# Changelog

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
