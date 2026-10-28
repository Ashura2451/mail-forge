# Review, Mailforge operational workflows 017, 2026-10-28

**Task:** `MAILFORGE-OPERATIONAL-WORKFLOWS-017`
**Branch:** `feat/mailforge-operational-workflows-017`

## Result

The local operational workflow slice is implemented on top of the published
016 branch. It remains local-first and synthetic: FastAPI/Uvicorn, React/Vite,
canonical Python services, SQLite, private app-owned List Tools artifacts, and
bounded in-process supervisors. No live provider, mailbox, recipient, or
external delivery was used.

## Acceptance matrix

| Area | Status | Evidence |
| --- | --- | --- |
| Durable migration for event/schedule/List Tools state | PASS | Migration 9; schema/integrity exercised by full suite |
| Automatic contact-import enrollment | PASS | `test_operational_contact_event_is_automatic_and_idempotent` |
| Automatic list-membership enrollment | PASS | Transactional membership event emission and supervisor dispatch |
| Trusted inbound source protection | PASS | Event emitted only for trusted synthetic exact reconciliation |
| List Tools completion event | PASS | `test_list_tools_completes_without_contact_side_effect` |
| Duplicate/burst event protection | PASS | Durable cursor plus source/enrollment deduplication |
| Scheduled absolute/relative/local trigger support | PASS | Validation and durable schedule reconciliation tests |
| IANA timezone and DST conversion | LOCALLY TESTED | ZoneInfo conversion, nonexistent-time shift, ambiguous-time fold policy |
| Restart/downtime bounded reconciliation | PASS | Durable cursor, schedule rows, and due-run scan |
| Standalone List Tools browser route | PASS | `/api/v1/list-tools/*`, React List Tools navigation and controls |
| TXT/CSV/JSON/JSONL processing | PASS | Existing canonical InputReader plus 017 service integration |
| Provider TXT/CSV/master/summary/error artifacts | PASS | Private artifact manifest and path-safe download route |
| No silent Contacts/List/Campaign mutation | PASS | Focused test asserts zero contacts after completion |
| Provider evidence separation | PASS | Catalog/DNS status, source, version, and evaluation timestamp fields |
| Bounded streaming/checkpoint/recovery | PASS | Existing EmailSorter/Checkpoint delegated by ListProcessingService |
| Existing application regression safety | PASS | `178 passed` |
| Browser/accessibility regression safety | PASS | Existing 4 Playwright acceptance tests passed |
| Frontend quality | PASS WITH WARNINGS | lint/build passed; pre-existing React hook warnings remain |
| Security boundary | PASS | App-owned input/artifact paths, bounded upload, session/CSRF/Host middleware |
| Real provider/recipient access | PASS (none accessed) | Offline DNS and synthetic fixtures only |
| Independent final review | NOT VERIFIED | No independent reviewer was available in this execution |

## Validation commands

- `uv run pytest -q` — 178 passed, one existing Starlette/httpx deprecation warning.
- `uv run pytest -q tests/e2e` — 4 passed.
- `uv run ruff check src tests` — passed.
- `uv run ruff format --check src tests` — passed.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run lint` from `web/` — passed with existing React hook warnings.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build` from `web/` — passed and refreshed packaged assets.
- `git diff --check` — passed.

## Limitations

- Evidence is local synthetic evidence, not provider qualification, consent
  evidence, mailbox delivery, hosted deployment, or production scheduler SLOs.
- List Tools uses the canonical sorter with offline DNS by default. DNS/MX
  evidence remains deterministic test data unless an operator explicitly runs a
  permitted local diagnostic.
- The in-process supervisor is bounded local infrastructure, not a distributed
  queue. Crash recovery is idempotent replay rather than a hosted exactly-once
  guarantee.
- Independent approval was not obtained.

No publication or push was performed in this review step.
