# Operational assurance 032 cumulative final review

**Verdict: BLOCKED.** Reviewed revision `e0af46925a0f865912e6b586d52615b4d6e2d4d4` against `74d264b79901abda8b43ae613e5d5232c181e516`. The only working-tree change at review and test time was this untracked review artifact, updated for the new revision. Reviewer and implementer used separate execution contexts, but distinct model identity cannot be authenticated. This is not business or merge approval.

## Major finding: hard-linked database aliases bypass runtime ownership locks

`src/mailforge/interfaces/process_lock.py:17-19` now resolves symbolic links before deriving the lock filename; the new test covers that fix. `Path.resolve()` does not unify hard links, however. A disposable probe created two hard links with the same device and inode and acquired both `DatabaseProcessLock`s simultaneously. With distinct data directories, two server invocations can consequently bypass the intended single-database owner boundary and potentially run competing schedulers or recovery. Reject multiply linked databases or otherwise lock the underlying authority, and exercise hard-link cases. Actual concurrent server effects were not executed.

## Packet criteria A–N

| ID | Disposition | Evidence and limits |
| --- | --- | --- |
| A: changes since r6 | PASS | Inspected two-lock `ExitStack` and symlink canonicalization. Identical and symlinked custom database paths are serialized; hard-link issue above remains. |
| B: source-health UNKNOWN | PASS | `6ed81a5` preserves an existing incident for a present unknown check; focused regression passes. |
| C: incident resolution / acknowledgment | PASS | Source evidence disappearance resolves the earlier incident; unchanged acknowledged signature remains acknowledged. Focused tests and code inspection; broader occurrence-history guarantees not independently established. |
| D: startup cleanup / backup publication | PASS | Directory lock serializes standard CLI backup and startup cleanup; atomic hard-link publication prevents same-name replacement. No independent live process race rerun for final revision. |
| E: custom database / data-directory ownership | FAIL | Hard-linked paths produce distinct locks for the same database, as probed above. |
| F: races / stale ownership | BLOCKED | Alias bypass above; executor reports stale-owner lease test passed. No full process-level concurrency qualification by this reviewer. |
| G: backup atomicity / interruption | NOT VERIFIED | Focused interruption and same-destination tests pass. Executor reports interrupted process cleanup and staged restore; crash/power-loss durability and final-revision process exercise not independently verified. |
| H: process recovery / ambiguous actions | NOT VERIFIED | Executor reports terminated Campaign recovered ambiguous without retry and Data Studio resumed; exact commands, source revision and retained artifacts are not recorded. No independent reproduction here. |
| I: Keychain identity / secret safety | NOT VERIFIED | Native Security.framework avoids secret-bearing child arguments; CFRelease added. Executor reports unsigned packaged synthetic store/resolve/rotate/revoke. Access-denial, lock, identity continuity and packaged behavior were not independently rerun. |
| J: health / incident accuracy | BLOCKED | Source errors are surfaced and known UNKNOWN case fixed. Health derives stale work from `updated_at` rather than a proven heartbeat; source-specific progress, overdue work and full incident occurrence semantics remain unqualified. |
| K: canonical operator controls | NOT VERIFIED | API delegates exposed controls to existing services by inspection; only a subset of subsystems and actions is exposed, and full authorization / recovery journeys were not independently exercised. |
| L: Maintenance UI | NOT VERIFIED | Executor reports desktop, short desktop, tablet and mobile checks; screenshots and detailed interactions are not retained. Prior reviewer saw an earlier revision, not final UI. |
| M: cross-service regression | BLOCKED | Ownership alias issue above. Focused 29 tests pass, but no final full suite or broad live regression rerun here. |
| N: prior-round findings | BLOCKED | Diagnostic error classification, source timestamps, pathname links, outcomes, liveness and incident fixes reviewed in r1–r7; alias ownership issue remains. Prior findings beyond the specific fixes need full packet reconciliation, not assumed closure. |

## Evidence and reconciliation

Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py`: **30 passed**, one Starlette/httpx deprecation warning, 3.82 seconds. Ran the disposable hard-link probe described above. Prior r1–r7 records and the current spec were inspected. No application code or tests were edited.

`docs/reviews/2026-10-30-mailforge-operational-assurance-032-qualification-evidence.md` and `docs/performance/operational-assurance-032-2026-10-30.md` are executor-reported local synthetic observations, not independent reruns. They do not establish real provider quota, production credentials, signing/notarization or production scale. Their recovery and UI observations lack retained artifacts and exact per-observation revision; the full non-browser result predates final regression additions and lock changes. Spec 0021 remains In Progress. The criteria table separates actual observation from missing evidence; PR integration gates cannot be declared satisfied with the ownership finding and remaining unverified material criteria.
