# Full-audit remediation — 1.0.1

The full source/local audit of v1.0.0 (commit 5101e524cc71486873cc10283ac35440e74cd60d) reproduced two operational correctness defects, with no demonstrated unauthorized security principal. This patch repairs both. The historical audit remains an assessment of v1.0.0, not evidence that every enterprise deployment has passed acceptance.

## OP-01: source reappearance and deletion grace

All validated desired names now clear their stale timestamps before per-name conflict handling. When a reset is needed in write mode, it is persisted before DNS mutations. A later unrelated mutation failure therefore cannot preserve an obsolete timer on restart. Dry-run still makes no DNS mutations or ownership-state writes.

The regression exercises real run_once and source validation, with ALLOW_EMPTY_NPM=false and another permitted host retained: absent -> returns beneath NS/DNAME/CNAME ancestor -> absent again with blocker gone. The second absence gets a new grace timestamp; no deletion occurs one second before expiry, and only the owned hostname is deleted at expiry. A separate test verifies durable reset despite a later sorted-earlier add failure.

## OP-02: apex aliases and fresh reads

Zone snapshots retain apex records. A shared ancestry predicate is used by both the initial snapshot and the final pre-mutation read. Apex DNAME/CNAME blocks descendant mutation; ordinary apex SOA/NS remains permitted. Descendant NS delegations remain blockers. All add/update/delete operations retain their fresh-read guard.

Regressions cover both persistent apex alias types in dry-run/live mode, both alias types appearing only on the final read, and normal apex SOA/NS with a successful child addition.

## Verification

Five regression methods were added to the original 30-test suite. They exercise multiple alias/delegation cases. Running the new cases against the unchanged v1.0.0 implementation produced the expected failures on all four affected paths, while the ordinary SOA/NS case passed. The fixed implementation passed all 35 tests under a bounded offline Linux sandbox. Independent read-only review found no concrete blocker in the source patch. Consult the release's attached evidence for actual container, architecture and published-digest checks; source tests alone do not establish those results.

## Upgrade and residual acceptance

State version and ownership marker are unchanged. First disable deletion, back up zone + state, and deploy the new verified digest with dry-run/staging review. The new code cannot reconstruct past presence missed by v1.0.0; observe successful upgraded polls throughout at least a full configured deletion grace window with deletion disabled before considering re-enabling it. Review pending removals and backups.

These repairs resolve the two code correctness blockers. Enterprise approval still requires installed NPM/Technitium API compatibility, complete NPM account visibility, zone-scoped credentials, trusted network/certificates, matched backup/restore and single-writer recovery drills, conflict/DNS monitoring, capacity measurements, exact-image vulnerability assessment and release-governance evidence. The upstream API still lacks atomic compare-and-swap, so coordinate manual edits and run one writer per zone.
