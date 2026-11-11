# Implementation and security review

Review date: 2026-11-11. Scope: original `npm_technitium_safe_sync.zip` from the earlier conversation; synchronizer, state, HTTP client, Docker/Compose packaging, tests and the new publishing workflow. This is a focused source/implementation review, not a penetration test or production certification. No live DNS or proxy-manager endpoints were accessed and no real management credentials were used.

## Findings and repairs

| Original issue | Consequence | Repair and evidence |
| --- | --- | --- |
| Ownership checked old IP but ignored managed comment and disabled status | A manually reclaimed record could still be updated or deleted | Require state + exact comment + enabled sole A + old IP; regression and HTTP lifecycle/manual-comment tests |
| NPM enabled flags used truthiness; malformed hostnames were skipped | Partial/malformed source could change desired-set membership and allow stale deletion | Strict boolean/0/1 and hostname validation; abort full cycle on malformed source; valid wildcards explicitly excluded |
| urllib followed redirects and ambient proxies | Management credentials could be sent to an unintended redirect endpoint or proxy | Refuse redirects, ignore proxy environment, validate endpoint URL; HTTP redirect regression verifies no second request |
| Remote errors were logged verbatim | A server error could echo credentials or control text into logs | Sanitized exception-class logging; no raw response bodies or URLs |
| State entries were not validated beyond top-level dictionaries | Corrupt state could influence ownership/deletion decisions | Validate normalized names, IPv4 addresses, finite nonnegative stale timestamps and stale-to-owned references |
| Parent and child zones could overlap in a name-only snapshot | DNS records could be attributed to the wrong configured zone | Reject overlapping/duplicate configured zones; validate zone and record response fields |
| Mutations used a single old cycle snapshot | Concurrent DNS edits could be overwritten/deleted | Fresh validated read before each write; target and delegation checks; residual API race documented |
| Configuration allowed typo booleans, empty credentials and unprotected HTTP | Unexpected operating modes and plaintext credential exposure | Strict flags, file secrets, duplicate-source rejection, HTTPS default and explicit HTTP opt-in |
| Heartbeat excluded dry-run and had no container healthcheck | A healthy dry-run could appear stale; failures lacked health feedback | Successful-cycle heartbeat in both modes, stale/missing health tests, Docker healthcheck |
| Local-only Compose and floating image/action inputs | No reproducible distribution or deployment pin | Image-only Compose, pinned base digest and action SHAs, least-privilege Actions, two-platform publishing with tested digest |

Additional compatibility repair: zone snapshot validation permits underscore DNS owners such as `_acme-challenge` TXT and `_sip._tcp` SRV while keeping strict hostname validation for NPM/owned A names. A local HTTP regression verifies unrelated service records are preserved.

## Verification

30 unit and local HTTP integration tests passed under Linux Python 3.14.4 before initial publication. Fixtures cover actual request serialization, both Technitium auth modes, add/update/delete, dry-run, restart/state reload, malformed APIs, manual comment changes, redirects, fresh-read conflict, process lock and health. CI repeats these tests on the actual Python 3.12 image, checks hardened startup, nonroot permissions, missing credentials, Compose validation and published-digest pull. Consult the release evidence and workflow for the actual CI result; prepared checks are not evidence that they ran.

## Residual limits and required staging

- No upstream compare-and-swap or cross-request transaction is available. Re-reads reduce but cannot eliminate concurrent-writer races. Use one writer and dedicated zones, and coordinate operator edits.
- Authentication cannot establish that an NPM account sees every intended host. Incomplete source visibility can look like removals. Verify access and keep deletion off until staged.
- A remote write can succeed before a crash/state failure. Such records are not automatically adopted. Review zone and state together; do not retry destructive recovery blindly.
- Separately hosted child zones are not enumerated by parent-zone record listing. Use zones without them. Signed DNSSEC records can create mixed-type conflicts; dedicated unsigned zones are the supported deployment pattern here.
- No live NPM/Technitium version compatibility, real DNS resolution, real certificates, restricted-user permissions or network reachability has been verified. Bearer authentication is documented for Technitium 15+; legacy parameter mode needs installed-version validation.
- Docker Compose file secrets rely on host permissions and are accessible to host/Docker administrators. Local state is a trust boundary and must not be writable by untrusted users.
- The healthcheck measures cycle freshness, not application reachability or successful DNS resolution. Conflicts require monitoring and operator action.
- Pinning does not deliver future base-image security updates automatically. Review Dependabot updates and perform appropriate vulnerability scanning as part of ongoing maintenance.

No prior independent audit record was supplied. Coverage is limited to this source package and its distribution workflow; unrelated GitHub account settings and existing live infrastructure were outside scope.
