# Review, feat/mailforge-operational-assurance-032, 2026-10-30

**Reviewed by**: Codex in a fresh read-only review session (commit author is
Ashura2451; a separate reviewer model was not available in this session, so
cross-model independence is not claimed)
**Scope**: 84 cumulative changed files, including 80 non-generated files,
branch vs base `e584c6b73cdb9ff1e55c631a4c294f75c0f273ee`
**Reviewed revision**: `8a8cc41044f73ffb3cf5c8f74fd8aa274fca1203`
**Verdict**: APPROVE WITH NITS

## Summary

The r8 ownership and health findings are corrected. The stable
application-scoped directory lock covers canonical, relative, symlink, and
hard-link database aliases, including creation and replacement; active durable
work without authoritative heartbeat evidence is `unknown`; unknown required
evidence participates in overall-state precedence; and campaign restore now
uses the same ownership boundary. No material blocker remains in the reviewed
scope.

## Resolved blocker from prior review

### ✅ Campaign restore now respects runtime ownership, `src/mailforge/interfaces/cli.py:1363-1367`

The final correction wraps `CampaignStore.restore_from(args.source,
args.destination)` in `DatabaseProcessLock(args.destination)`. Application
backup/restore and campaign backup already used the same stable lock.

The new process-level regression
`tests/test_web.py::test_campaign_restore_cli_respects_runtime_ownership`
holds the default lock in a child process and verifies that the restore
contender exits nonzero with the ownership error. The test passed, and a
source-level read confirms the lock surrounds the restore call.

The previously reproduced successful restore during an active lock is no
longer possible through this CLI path.

## Review disposition of the requested priority findings

| Priority | Disposition | Evidence |
| --- | --- | --- |
| Canonical/relative/symlink/hard-link ownership | PASS | `process_lock.py:15-100` uses a stable application-scoped directory lock rather than database path/inode identity. The focused alias test uses a hard link in a different directory; the cross-process lock test also passed. |
| Creation/replacement and lock lifetime | PASS | The lock primitive remains stable across file creation, `os.replace`, and marker replacement; the campaign restore CLI now holds the same lock through replacement, with a process-level contention regression. |
| No generic `updated_at` liveness inference | PASS | `operational_assurance.py:245-283` never marks recent active work healthy; active work without authoritative heartbeat/progress evidence is `unknown`, while stale `updated_at` is explicitly described as unverified. The service and actual web-path tests passed. |
| Missing authoritative evidence not healthy | PASS | `operational_assurance.py:392-400` gives `unknown` explicit precedence before `healthy`; the unknown-storage and unknown-overall tests passed. |
| Incident resolution from recovery evidence | PASS | `operational_assurance.py:455-507` retains unknown incidents and resolves observed failures only after a healthy/current or recovered evidence state. Source-failure recovery and unknown-storage preservation tests passed. |
| Ambiguous outcomes and unsafe replay | PASS (local synthetic scope) | Existing campaign crash/recovery and provider outcome tests passed; ambiguous outcomes remain durable and campaign retry reports no transport calls. No real provider was contacted. |
| Backup/restore authority and integrity | PASS | Atomic backup publication, app restore, campaign restore, integrity, staged restore, and browser backup/restore paths are covered by source inspection and focused tests. |
| Credentials and unsigned Keychain claims | PASS with qualification limits | References remain opaque/runtime-only; native Security.framework code avoids secret-bearing child arguments, and package/docs explicitly state unsigned/unnotarized limits. Native access control, signed identity continuity, and production credentials were not independently verified. |

## Source independently inspected

- `src/mailforge/interfaces/process_lock.py`, including stable lock-path
  selection, symlink rejection, directory advisory lock, marker lifetime, and
  release behavior.
- `src/mailforge/interfaces/cli.py` and the web runtime backup/restore paths,
  including all identified `DatabaseProcessLock` call sites.
- `src/mailforge/application/operational_assurance.py`, incident persistence,
  worker classification, source-failure handling, and overall aggregation.
- `src/mailforge/application/local_application.py` and
  `src/mailforge/infrastructure/campaign_store.py` for atomic publication,
  staged restore, integrity verification, and campaign restore behavior.
- `src/mailforge/infrastructure/credentials.py`, the Maintenance UI changes,
  focused regression tests, `docs/specs/0021-operational-assurance.md`,
  `docs/runtime-ownership.md`, the r8 review, qualification manifest, and
  performance record.

## Tests independently executed for the final correction

The final correction test ran against `8a8cc41` without modifying application source or
tests:

- Focused restore/ownership and local restore tests, including the new
  process-level campaign restore contention regression: **8 passed**, one existing
  Starlette/httpx deprecation warning.

The prior review at `fea478a` independently recorded:

- Focused ownership, assurance, local backup/restore, campaign recovery, and
  Data Studio recovery tests: **49 passed**.
- Full non-E2E Python suite: **280 passed**, one existing
  Starlette/httpx deprecation warning.
- Browser/accessibility suite: **8 passed**.
- `npm run build --prefix web`: passed.
- `npm run lint --prefix web`: passed with existing React hook/ref warnings.
- `git diff --check`: passed.
- `bash scripts/check-secrets.sh`: passed.
- Disposable process-level campaign-restore contention probe: **failed the
  ownership expectation** as documented in the blocker above.

The repository-local Ruff executable was not present in `.venv/bin`, and
`uv run --offline ruff check src tests` could not spawn Ruff; the committed
executor claim that Ruff passed is therefore not independently verified here.

## Executor evidence assessed, not independently verified

`docs/reviews/2026-10-30-mailforge-operational-assurance-032-qualification-evidence.md`
reports package, native/packaged Keychain, process termination, interrupted
backup publication, staged restore, viewport, and performance exercises. Those
claims are executor evidence only. The manifest identifies its latest source
qualification as `327a98b`, not the reviewed `fea478a`, and its package is
explicitly unsigned and unnotarized. The performance values are small local
synthetic workloads and do not establish production scale, provider quotas,
RPO/RTO, or SLOs.

## Explicit limitations

No production credentials, real provider or mailbox access, live email, signed
or notarized release, production-scale dataset, or production deployment was
required or used. Native Keychain ABI/access-denial behavior, signed-package
identity continuity, and the packaged runtime were not independently rerun.
The local browser suite passed, but this review did not independently retain
new screenshots or claim full visual/package qualification. The milestone
spec remains `In Progress`, and separate simultaneous databases remain
intentionally unsupported by the documented single-runtime contract.

No scope row matching this milestone was found; scope review status is not
ticked. The remaining nits are evidence provenance only: cross-model reviewer
identity, Ruff execution, packaged runtime, native Keychain access behavior,
and signed/notarized continuity were not independently verified here. These do
not block the local synthetic milestone correction, and the prior blocker is
closed.
