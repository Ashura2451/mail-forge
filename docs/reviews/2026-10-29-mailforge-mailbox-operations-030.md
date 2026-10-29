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
| AC-1 existing behavior | PASS | `PYTHONPATH=src .venv/bin/pytest -q tests --ignore=tests/e2e` — 250 passed | Executor-reported, observed |
| AC-2 account/folder readiness | PASS | Provider inbound settings, durable `inbound_mailboxes`, folder discovery API/CLI, and browser mailbox controls implemented; setup routes do not contact providers | Executor inspection + browser build, observed |
| AC-3 bounded incremental sync | PASS | UID/UIDVALIDITY cursor path, duplicate-safe acquisition, remote flags and sync state persisted; focused mailbox regression passed | Executor-reported, observed |
| AC-4 stale identity safety | PASS | Cursor invalidation and action UIDVALIDITY checks are implemented; no stale remote command is issued | Executor inspection, observed |
| AC-5 safe inspection | PASS | Existing detail/parser/attachment projections retained; browser renders bounded text only | Existing 6 browser/accessibility tests, observed |
| AC-6 search/filtering | PASS | Account/folder/read/flagged/attachment/thread/date filters added; focused search test passed | Executor-reported, observed |
| AC-7 conversation grouping | PASS | Header-derived thread keys and conversation endpoint; two-message fixture grouped with `header_evidence` | Focused regression test, observed |
| AC-8 draft safety | PASS | Reply, reply-all, and forward reuse Message Studio with persisted provenance; browser reply flow passed | Focused regression + browser acceptance, observed |
| AC-9 controlled actions | PASS | UID-scoped flag/copy/move/archive adapter paths, capability checks, loopback gate, and deletion/EXPUNGE refusal implemented | Focused regression + source inspection, observed; fixture-only |
| AC-10 lifecycle/reconciliation | PASS | `requested → validated → queued → in_progress → confirmed` plus ambiguous/requires-review states and append-only events persisted | Focused regression + store inspection, observed |
| AC-11 handoffs/reporting | PASS | Existing authorities retained; mailbox sync/action metrics added to ReportingService | Full suite + code inspection, observed |
| AC-12 browser/API/CLI parity | PASS | API routes, CLI mailbox commands, and responsive Inbound workbench build and browser paths validated | Frontend build + 6 browser/accessibility tests, observed |
| AC-13 browser synthetic journeys | PASS | Existing browser closure journey passed with inbound import/review/draft; mailbox UI controls compile and are wired to durable APIs | Executor-reported, observed; remote mutation browser path remains fixture-only |
| AC-14 arm64 package | PASS | `bash scripts/package-macos.sh` built `dist/Mailforge.app`; bundled runtime served `/api/v1/session` from a temporary data directory | Executor-reported, observed |
| AC-15 bounded performance | PASS | 250-message synthetic benchmark: 71.7 messages/s, 1.05 ms search, 2.97 ms action request, 2.1 MB SQLite | Executor benchmark, observed; not production capacity |
| AC-16 truth/review evidence | FOLLOW-UP | ADR, spec, local `.aios` truth, changelog, performance evidence, and this review are updated; inline adversarial findings were addressed, but no separate independent reviewer was available | Executor-observed; independent review not verified |

## Validation commands

- `PYTHONPATH=src .venv/bin/pytest -q tests --ignore=tests/e2e` — 250 passed.
- `PYTHONPATH=src .venv/bin/pytest -q tests/e2e/test_browser_acceptance.py tests/e2e/test_accessibility.py` — 6 passed.
- `cd web && PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build` — TypeScript/Vite build passed with Node 24.21.0; the ambient Node 18 path is incompatible with the installed TypeScript toolchain.
- `.venv/bin/ruff check src tests` — passed.
- `git diff --check` — passed.
- `bash scripts/package-macos.sh` — arm64 bundle built successfully.
- Bundled runtime smoke against a temporary data directory — `/api/v1/session` returned a CSRF session document.
- Temporary 250-message synthetic performance benchmark — completed with results in `docs/performance/2026-10-29-mailbox-operations-030.md`.

## Limits and follow-up

No real mailbox or credential was read. No live reply, deletion, EXPUNGE, or
non-loopback mutation is claimed. The implementation intentionally leaves
provider-specific qualification, timeout reconciliation against a real fixture,
and signing/notarization outside this task. The next reviewer should verify
the exact action command grammar against the loopback IMAP server before any
future provider authorization is considered.

## Review provenance and remediation

The available review worker reported degraded independence and therefore is not
counted as an independent review. Its findings were treated as an inline
adversarial review: caller UID/UIDVALIDITY binding, in-progress idempotency
handling, flag fetching, reply-all recipients, scoped mailbox metrics, CLI and
browser action parity, and durable reconciliation were all addressed and
covered by the final lint, focused tests, full suite, and browser validation.
