# Changelog

## 1.0.1

- Reset and persist deletion timers on validated source reappearance before any DNS conflict or mutation, including later mutation failures.
- Retain zone-apex aliases and apply one ancestry guard to initial and fresh snapshots, blocking apex DNAME/CNAME while preserving normal SOA/NS behavior.
- Add five integration regression methods covering all three ancestor blocker types, fresh apex aliases, normal apex records and durable timer reset on failure.
- Document conservative upgrade handling for pending timers created by v1.0.0. Existing state format, marker and conservative defaults remain compatible.

## 1.0.0

Initial project-maintained GHCR release with conservative ownership, dry-run, opt-in delayed deletion and amd64/arm64 distribution.
