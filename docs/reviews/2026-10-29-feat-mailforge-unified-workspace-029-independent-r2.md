# Review, feat/mailforge-unified-workspace-029, 2026-10-29, r2

**Reviewed by**: GPT-6 / Codex (author attributed to Codex/GPT-5 in the original review request)
**Scope**: 9 maintained implementation/documentation files, current working tree against `origin/main` at `8c2b4d75b218e455a48060e65543936d17634e33`, plus replacement generated frontend assets and the r1 report as context. Implementation HEAD remains `a3e21714ff2ea458e109f360adf2a248a8b7c7b8`; the fixes are working-tree changes.
**Verdict**: Changes requested

## Summary

The drawer reachability, mutation replay, mobile campaign purpose, and shared mobile column fixes work in independently exercised browser scenarios. Normal Maintenance hydration and protection of an already edited form during refresh also work, but delayed initial loading can still cause an unrelated saved preference to be overwritten, and an in-flight save can discard newer edits. One major preference-state issue and one minor hidden-drawer focus issue remain. Green browser/static checks do not cover these reproduced cases.

## Major

### 🟠 Preference dirty-state handling still loses settings and newer edits, `web/src/App.tsx:213`

**Problem**: The single `preferencesDirty` flag prevents hydration of every field when any field changes, even before the initial preferences response has arrived. Independently seeded `timezone=America/New_York`, opened Maintenance from Reports while holding its `/runtime` response, checked only “Allow sample data during onboarding,” then released the response. The untouched timezone remained the initial UTC default. Saving persisted UTC, overwriting America/New_York despite changing only the checkbox.

The unconditional dirty reset after the save POST at `web/src/App.tsx:220` also discards newer edits: holding that response, changing the timezone again, and releasing it replaced the newer Asia/Tokyo input with the earlier submitted Europe/Paris value. Separately, a successful timezone save followed by a failed `/runtime` refresh reverted the form to its pre-save value while the backend retained the new value; the safe GET Retry repaired the display.

**Why it matters**: Ordinary slow or failing requests still cause an unrelated durable setting to be overwritten or an operator's newer input to disappear. The normal navigation/reload case is fixed, but the original preference-integrity concern is not fully closed.

**Suggested fix**: Establish a loaded preferences baseline before enabling edits/save, or merge incoming values into individually untouched fields. Only clear dirty state for the edit snapshot actually acknowledged by a save; preserve newer edits and use the acknowledged values instead of stale pre-save props. Add delayed-initial-load, edit-during-save, and successful-save/failed-refresh regression scenarios.

## Minor

### 🟡 The closed drawer itself remains a hidden keyboard stop, `web/src/App.tsx:145`

**Problem**: The buttons correctly receive `tabIndex=-1` and the rail receives `aria-hidden=true`, but the new scrollable aside itself remains keyboard focusable in the installed Chromium. After closing the drawer and waiting for the transition, focusing Open navigation and pressing Shift+Tab focused the ASIDE at x=-300 with `aria-hidden=true`. This was independently reproduced after navigation selection and after Escape dismissal.

**Why it matters**: Keyboard focus still enters an invisible element excluded from the accessibility tree. Checking the child buttons' tabindex, as the updated test does, misses the browser's focusable scroll container.

**Suggested fix**: Make the complete closed mobile drawer inert or otherwise remove the scroll container itself from sequential focus. Exercise actual forward/reverse tab navigation after dismissal while preserving the open drawer's scrolling and desktop navigation.

## Strengths

- Mobile Marketing purpose now propagates through create, workspace loading, and draft updates; the independent check observed the expected unsubscribe preflight blocker.
- Removing generic mutation replay prevents duplicate creation after a successful POST followed by a failed detail read, while failed shell reads retain a GET-only Retry.
- The lower drawer destinations can now be scrolled into view and activated at both 390×844 and 390×568; shared split panels correctly become one column.

## Test coverage

Independent checks used the repository Python environment, installed Chromium, FastAPI-served current assets, and disposable state outside the repository. No application code, tests, specs, AGENTS files, or prior reports were modified by this review.

| Requested verification | Status | Independent observation |
| --- | --- | --- |
| Drawer scroll/reachability | PASS | Actual wheel scrolling at 390×844 and 390×568 brought Maintenance within the viewport; activating it reached Maintenance and restored menu focus. |
| Closed-drawer focus | FAIL | Child buttons are removed from sequential focus; the hidden ASIDE itself receives Shift+Tab focus. Escape and scrim dismissal restore menu focus; desktop navigation remains accessible. |
| Successful mutation/follow-up read failure | PASS | Injected one 503 document-detail response during message creation. Exactly one document was created; no mutation Retry was offered. Refresh did not create another document. A separately failed shell GET offered a Retry that also left the count unchanged. |
| Maintenance normal hydration | PASS | Saved values loaded after navigation and reload. An edited timezone survived an ordinary runtime refresh without being reset. |
| Maintenance delayed loading/save dirty-state protection | FAIL | Delayed initial response plus a checkbox edit overwrote the unrelated stored timezone with UTC on save. A newer edit during a pending save was also discarded. Successful save plus failed refresh displayed the old value until GET Retry. |
| Mobile Campaigns Marketing persistence | PASS | At 390×844, creation persisted Marketing and produced one preflight blocker, including `endpoint_not_configured` and the `one_click_not_fully_verified` finding. Refresh/reopen and a subsequent draft save preserved Marketing. |
| Previously reported split/stat layout | PASS | Data Connections, Integration Hub, Email Standards, and Maintenance computed one 342px split column at 390px viewport width. Dashboard statistics computed two 165px columns; a fresh rendered capture was inspected. |
| Existing browser/accessibility suite | PASS | Independent run of `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider tests/e2e` with an external basetemp: 6 passed. |
| TypeScript, lint, frontend build | PASS | Both TypeScript projects passed no-emit checks. Oxlint completed with React effect/dependency warnings. Vite built into an external temporary directory using Node 24; JS/CSS SHA-256 values matched the current working-tree assets. |

The updated persistent tests cover drawer scrolling, child-button tabindex, normal preference reload, and mobile Marketing creation. They do not exercise the failing preference timing cases or actual reverse-tab focus into the scroll container. Those are specific remaining regressions, not a request for broader scaffolding.

### Provenance and limits

This is a follow-up to the r1 findings, using the actual current working-tree fixes and compiled assets, the governing repository/web conventions, DESIGN.md, and specification 0019. Application source hashes at review completion: App.tsx `b2a89063f69a61bf5f4425bbbbd21fa757595a0055e85afeecd44c761c5ebb3e`; App.css `6d963008545d4e09a71d37bdae73e0477a033ea1a2c75446f868f0591f6b141a`. Minor formatting changes to the browser tests appeared during the review and were inspected and preserved.

The prior 248 Python regression passes and packaged durability qualification belong to r1; this follow-up does not claim a new packaged-runtime or whole-suite Python qualification. External providers, real recipients, live sending, signing/notarization, and business acceptance remain outside scope. The only repository write authorized and performed by this review is this r2 report.
