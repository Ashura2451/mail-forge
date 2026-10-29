# Mailforge mailbox operations 030 result review

Date: 2026-10-29
Task: `MAILFORGE-MAILBOX-OPERATIONS-030`
Branch: `feat/mailforge-mailbox-operations-030`
Base: `origin/main` at `2367fcd8939a2f68e367d3fc7e462001b3be8fd9`

## Scope

This implementation extends the existing inbound/provider/CampaignStore
authorities with folder projections, stable remote identity, bounded sync,
conversation grouping, reply-all provenance, gated loopback-only IMAP actions,
browser/API/CLI surfaces, reporting, and migration 23. Real accounts, live
replies, permanent deletion, EXPUNGE, and external mutations were not used or
qualified.

## Acceptance evidence

| Criterion | Status | Method and observed result | Provenance |
| --- | --- | --- | --- |
| AC-1 existing behavior | PASS | `PYTHONPATH=src .venv/bin/pytest -q tests --ignore=tests/e2e` — 254 passed, 1 existing Starlette/httpx deprecation warning | Executor-observed |
| AC-2 account/folder readiness | PASS | Provider inbound settings, durable `inbound_mailboxes`, folder discovery API/CLI, and browser mailbox controls implemented; setup routes do not contact providers | Executor inspection + browser build, observed |
| AC-3 bounded incremental sync | PASS | UID/UIDVALIDITY cursor path, duplicate-safe acquisition, remote flags and sync state persisted; focused mailbox regression passed | Executor-reported, observed |
| AC-4 stale identity safety | PASS | Cursor invalidation and action UIDVALIDITY checks are implemented; no stale remote command is issued | Executor inspection, observed |
| AC-5 safe inspection | PASS | Existing detail/parser/attachment projections retained; browser renders bounded text only | Existing 6 browser/accessibility tests, observed |
| AC-6 search/filtering | PASS | Account/folder/read/flagged/attachment/thread/date filters added; focused search test passed | Executor-reported, observed |
| AC-7 conversation grouping | PASS | Header-derived thread keys and conversation endpoint; two-message fixture grouped with `header_evidence` | Focused regression test, observed |
| AC-8 draft safety | PASS | Reply, reply-all, and forward reuse Message Studio with persisted provenance; browser reply flow passed | Focused regression + browser acceptance, observed |
| AC-9 controlled actions | PASS | Actual packaged-browser `mark_read` mutation plus loopback protocol tests for identity-safe MOVE/COPY-style postconditions; capability checks, loopback gate, and deletion/EXPUNGE refusal remain enforced | Packaged browser + loopback fixture tests, observed; fixture-only |
| AC-10 lifecycle/reconciliation | PASS | Atomic action claim, UIDVALIDITY checks on mutating and observing connections, postcondition verification, ambiguous disconnect/read-failure handling, fenced cursor/run recovery, and append-only events are covered | Focused regression + loopback fixture tests, observed |
| AC-11 handoffs/reporting | PASS | Existing authorities retained; mailbox sync/action metrics added to ReportingService | Full suite + code inspection, observed |
| AC-12 browser/API/CLI parity | PASS | API routes, CLI mailbox commands, and responsive Inbound workbench build and browser paths validated | Frontend build + 6 browser/accessibility tests, observed |
| AC-13 browser synthetic journeys | PASS | Actual packaged-browser loopback journey covered discovery, initial/incremental sync, conversation, drafts, mark-read mutation, backup, stop/restart, and durable state retention | Executor-observed; remote mutation remains fixture-only |
| AC-14 arm64 package | PASS | `bash scripts/package-macos.sh` rebuilt `dist/Mailforge.app`; the actual bundled launcher served the session API and completed the browser journey | Executor-observed |
| AC-15 bounded performance | PASS | 250-message synthetic benchmark: 71.7 messages/s, 1.05 ms search, 2.97 ms action request, 2.1 MB SQLite; peak memory was not measured | Executor benchmark, observed; not production capacity |
| AC-16 truth/review evidence | PASS | ADR, spec, local `.aios` truth, changelog, performance evidence, and this review are updated; a clean-context post-fix reviewer found no blocker or major finding and recommended closure | Executor-observed + independent clean-context reviewer |

## Validation commands

- `PYTHONPATH=src .venv/bin/pytest -q tests --ignore=tests/e2e` — 254 passed, 1 existing Starlette/httpx deprecation warning.
- `PYTHONPATH=src .venv/bin/pytest -q tests/e2e/test_browser_acceptance.py tests/e2e/test_accessibility.py` — 6 passed.
- `cd web && PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build` — TypeScript/Vite build passed with Node 24.21.0; the ambient Node 18 path is incompatible with the installed TypeScript toolchain.
- `.venv/bin/ruff check src tests` — passed.
- `git diff --check` — passed.
- `bash scripts/package-macos.sh` — arm64 bundle rebuilt successfully after the closure safety fixes.
- Bundled runtime smoke against a temporary data directory — `/api/v1/session` returned a CSRF session document.
- Temporary 250-message synthetic performance benchmark — completed with results in `docs/performance/2026-10-29-mailbox-operations-030.md`; peak memory was not measured.
- Packaged browser acceptance against a TLS loopback IMAP fixture — discovery returned INBOX/Archive and MOVE/UIDPLUS capabilities; initial sync acquired two messages, incremental sync retained three unique messages after a third fixture message was added, conversation/drafts/mark-read completed, backup was created, and stop/restart retained schema 23, integrity, three messages, and draft state.

## Limits and follow-up

No real mailbox or credential was read. No live reply, deletion, EXPUNGE, or
non-loopback mutation is claimed. The implementation intentionally leaves
provider-specific qualification, timeout reconciliation against a real fixture,
and signing/notarization outside this task. The next reviewer should verify
the exact action command grammar against the loopback IMAP server before any
future provider authorization is considered.

## Review provenance and remediation

The first available review worker reported degraded independence and was not
counted as an independent review. A separate clean-context reviewer then
identified action identity, postcondition, recovery fencing, local move
projection, MOVE quoting, and duplicate-identity gaps. Those findings were
addressed in the current worktree and covered by focused loopback tests, the
full suite, and the rebuilt packaged browser path. The post-fix reviewer
reported no blocker or major finding and recommended closure; the focused
post-fix suite passed 18 tests.
