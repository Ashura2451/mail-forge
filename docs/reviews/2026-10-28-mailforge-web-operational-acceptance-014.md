# Review, Mailforge web operational acceptance 014, 2026-10-28

**Task:** `MAILFORGE-WEB-OPERATIONAL-ACCEPTANCE-014`
**Branch:** `feat/mailforge-web-operational-acceptance-014`

## Result

The local browser operational slice is implemented and locally verified. The
scope is limited to the existing local-first architecture: FastAPI/Uvicorn,
React/Vite compiled assets, canonical Python services, SQLite persistence, and
bounded in-process synthetic work.

## Acceptance matrix

| Criterion | Status | Evidence |
| --- | --- | --- |
| Existing web application remains functional | PASS | `pytest` 159 passed; existing browser acceptance passed |
| Inbound review through browser controls | BROWSER VERIFIED | EML upload, classification, exact correlation, approve/reject, and eligibility update in `test_browser_closure_and_inbound_review_survive_service_runtime` |
| Browser closure does not stop authorized work | BROWSER VERIFIED | Page closed after start; fresh page observed completed campaign against live service |
| Fresh-session reopening restores state | BROWSER VERIFIED | New browser context reloaded the reviewed inbound state |
| Service restart recovers durable work | PASS | `test_web_runtime_reconciles_running_campaign_after_restart`; valid `running → pausing → paused` transition |
| Duplicate local processes cannot independently dispatch | PASS | `DatabaseProcessLock` regression and two actual `mailforge serve` processes; second exited with ownership error |
| Ambiguous delivery remains protected | LOCALLY PROTOCOL TESTED | Connected TLS SMTP fixture produced ambiguous post-DATA state; retry made no second submission |
| Unified SMTP/IMAP lifecycle | LOCALLY PROTOCOL TESTED | `test_unified_local_smtp_imap_recovery_and_policy_lifecycle` passed with canonical services and SQLite |
| Suppression and eligibility remain enforced | PASS | Approved DSN/unsubscribe review changed future eligibility to excluded |
| Activity/reporting reflect persisted facts | PASS | Existing API lifecycle plus operational event assertions |
| Responsive UI | BROWSER VERIFIED | Existing 390px acceptance and closure test at desktop/mobile contexts |
| Automated accessibility | PASS | axe WCAG 2A/2AA route audit: no critical or serious findings |
| Keyboard/focus behavior | BROWSER VERIFIED | Focused navigation and form control, activated navigation with Enter, and asserted visible focus |
| Local web security invariants | PASS | Host/origin/session/CSRF tests, bounded uploads, temporary-file cleanup, CSP, loopback bind |
| Existing CLI/backend capabilities | PASS | Complete Python suite and existing provider/legacy tests passed |
| Real provider or recipient data accessed | PASS (none accessed) | All protocol fixtures, credentials, lists, and databases were synthetic/disposable |

## Review limitations

- This is local synthetic evidence, not real-provider qualification, inbox
  delivery evidence, consent evidence, or hosted-service approval.
- The browser remains simulation-only for outbound work. The provider screen's
  local diagnostic result must not be read as external readiness.
- 011-S remains qualified as locally corrected but not independently approved;
  the prior reviewer did not issue a fresh approval after the fixes.
- Independent final review for 014 was unavailable in this execution
  environment. No independent approval is claimed.

## Validation commands

- `.venv/bin/pytest -q` — 159 passed, one existing Starlette/httpx deprecation warning.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run lint` — passed.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build` — passed and refreshed packaged assets.
- `uv run ruff check src tests SORTER` and `uv run ruff format --check src tests SORTER` — passed after formatting.
- `uv lock --check` — passed.
- `uv build` — passed for sdist and wheel.
- `bash scripts/check-secrets.sh` — passed.
- `git diff --check` — passed.

No publication or push was performed.
