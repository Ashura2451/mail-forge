# Review, feat/mailforge-unified-workspace-029, 2026-10-29, r3

**Reviewed by**: GPT-5/Codex (inline independent re-review; author model not established)
**Scope**: Targeted current working-tree fixes in `web/src/App.tsx`, compared with `origin/main` and r2 findings; read-only review.
**Verdict**: Approve

## Summary

The two r2 corrections are resolved in the current implementation. Maintenance now gates editing on loaded runtime preferences, hydrates only clean fields, and preserves newer dirty values across save races and failed refreshes. The closed mobile rail is inert and removed from tab navigation; no remaining major or minor finding was reproduced.

## Verification

| Criterion | Status | Evidence |
| --- | --- | --- |
| Maintenance initial hydration | PASS | Held `/runtime` in Chromium at 390×844; all preference controls and Save were disabled, then hydrated to the persisted `America/New_York` / simulation-on / sample-off values after release. |
| Maintenance field-level dirty hydration | PASS | Edited only sample-data, returned a runtime response changing timezone to `Europe/Paris`; timezone updated while the dirty sample edit remained checked. |
| Maintenance in-flight save reconciliation | PASS | Held the preference POST, changed the timezone from submitted `Europe/London` to newer `Asia/Tokyo`, then released; the UI retained `Asia/Tokyo` while the backend correctly retained the submitted `Europe/London`. |
| Maintenance failed refresh | PASS | Forced a 503 on refresh after the race; the dirty `Asia/Tokyo` value remained visible and intact. |
| Closed mobile drawer focus | PASS | At 390×844, after Escape and reverse-tab, the aside reported `inert=true`, `tabIndex=-1`, zero child tab stops, and focus did not enter the off-screen rail. |
| Browser acceptance | PASS | `tests/e2e/test_browser_acceptance.py`: 5 passed. |
| Frontend validation | PASS | TypeScript build and Vite build passed with Node 24; Oxlint exited 0 with existing React hook/ref warnings. |

## Remaining findings

None at major or minor severity.

## Limits

The full e2e directory was not cleanly runnable in the current environment because the accessibility subprocess could not import the unavailable `anyio` dependency when pytest plugin autoload was disabled; the five browser acceptance tests and the targeted Chromium probe passed. No application code or tests were modified; this report is the only review write.
