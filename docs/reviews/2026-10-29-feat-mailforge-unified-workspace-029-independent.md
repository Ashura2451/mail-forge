# Review, feat/mailforge-unified-workspace-029, 2026-10-29

**Reviewed by**: GPT-6 / Codex (author appears to be Codex/GPT-5, per the supplied executor-session attribution)
**Scope**: 9 maintained files, branch vs origin/main; additionally inspected the 4 generated frontend diff entries. Base and merge base: `8c2b4d75b218e455a48060e65543936d17634e33`; reviewed implementation HEAD: `a3e21714ff2ea458e109f360adf2a248a8b7c7b8`, including current working-tree documentation changes.
**Verdict**: Changes requested

## Summary

The milestone connects existing workbenches through a shared browser shell and adds useful Integration Hub, Email Standards, and Maintenance controls without introducing another business-logic authority. Independent execution passed 248 Python tests, all 6 browser/accessibility tests, frontend validation, and packaged Message Studio/backup/restart operations. Four major defects remain: unreachable mobile navigation destinations, mutation replay through Retry, preferences initialized from stale data, and loss of campaign purpose through the sole mobile creation flow. No blocker-level defect or new unrestricted live-send path was demonstrated; local functional acceptance still requires the major fixes below.

## Major

### 🟠 Lower drawer destinations cannot be reached by scrolling, `web/src/App.css:226`

**Problem**: The narrow-screen rail is fixed to the viewport but has no vertical scrolling container. At 390×844, its content measures 1,259px high while its box remains 844px high. Integration Hub starts at y=942 and Maintenance at y=1,147. Independently opening the drawer and scrolling over it moved the underlying page by 1,100px while both destination coordinates remained unchanged.

**Why it matters**: Connectivity, Insights, and Application destinations fall below the usable drawer area. The milestone's labelled navigation does not make all major areas discoverable and reachable on a normal mobile viewport. Opening the drawer and pressing Escape, as the current test does, does not exercise this failure.

**Suggested fix**: Make the drawer's contents scroll within its available height, keeping navigation items from shrinking and handling background scrolling deliberately. Verify that the last destination can be scrolled into view and activated at 390×844 and shorter heights.

### 🟠 Retry repeats completed mutations after a follow-up read fails, `web/src/App.tsx:124`

**Problem**: Every action installs a retry closure that runs the entire action again, without an idempotency or completion distinction. Independently intercepting one document-detail GET with a 503 during “+ New message” left one successfully created document and displayed an error with Retry. Clicking Retry created a second document. The document count progressed from 0 to 1 to 2; the create POST itself had succeeded.

**Why it matters**: An error in a read after a write becomes a duplicate durable operation. This wrapper serves creation, imports, revisions, credentials, and operational controls, so it cannot safely assume that every rejected action promise means no mutation occurred. The new recovery affordance violates the requirement to preserve durable-operation boundaries.

**Suggested fix**: Separate mutation completion from subsequent refresh failures. Retry reads after a confirmed mutation; permit mutation replay only through an explicit service-supported idempotency contract or reconciliation of the original outcome. Add regression coverage for a successful write followed by a failed detail read and for an ambiguous write response.

### 🟠 Reopening Maintenance replaces saved preferences with defaults, `web/src/App.tsx:193`

**Problem**: The new preference controls initialize React state from `data` once. Maintenance mounts before its asynchronous `/runtime` response arrives, receiving the previous workbench's data or `{}`; the later response never hydrates those controls. Independently saving America/New_York, visiting Reports, and reopening Maintenance displayed UTC while `/runtime` still returned America/New_York. A direct page reload also displayed UTC.

**Why it matters**: The interface misrepresents durable settings. Saving another preference then submits the stale timezone and all default checkbox values, silently overwriting settings that the operator did not intend to change. The existing browser test checks the immediate save notification, not a reopened view or the resulting settings after another save.

**Suggested fix**: Initialize editable preferences only after the matching runtime data has loaded, or hydrate them from that response with explicit dirty-state handling. Verify navigation, refresh, and a subsequent single-preference edit preserve all saved values.

### 🟠 Hiding the legacy controls makes the mobile flow discard Marketing purpose, `web/src/App.css:258`

**Problem**: Below 560px this change hides the legacy creation controls, leaving `CampaignWorkspace.create` as the only visible creation flow. That handler at `web/src/App.tsx:758` omits `purpose` from the POST body, unlike `createLegacy` at line 739. Independently selecting Marketing at 390×844 and clicking Create draft persisted `configuration.communication_purpose = transactional`; the resulting workspace preflight was ready with zero blockers and no marketing unsubscribe findings.

**Why it matters**: The remaining mobile workflow records a different policy purpose from the operator's selection and evaluates the wrong standards requirements. The omission existed in the workspace handler before this branch, but hiding the correctly propagating alternative makes it the only narrow-screen creation path. Execution remains synthetic, which limits immediate harm, but the durable campaign and its policy evidence are incorrect.

**Suggested fix**: Ensure the retained creation and update flows submit and reload the selected purpose before removing the alternate controls. Add a mobile Marketing creation assertion against the stored workspace configuration and its expected unsubscribe preflight findings; the current acceptance test exercises the hidden legacy desktop flow.

## Minor

### 🟡 Shared base styles override narrow-screen column rules, `web/src/App.css:131`

**Problem**: The new unconditional `.split` declaration occurs after the existing max-width:850px single-column rule and wins the cascade. At 390px, Data Connections, Integration Hub, Email Standards, and Maintenance each render two 162px columns instead of one column. With card padding, form fields have approximately 120px available; Maintenance diagnostic values visibly extend into the adjacent preferences panel. The later `.stat-grid` base declaration at line 145 similarly supersedes the earlier responsive column rule.

**Why it matters**: These workbenches violate DESIGN.md's single-column small-screen contract and make local diagnostics and forms unnecessarily difficult to read. A document-width assertion alone misses overlapping content within the page.

**Suggested fix**: Place the responsive overrides after the shared base styles, preserving the established breakpoints. Check populated forms and diagnostic values at mobile widths, including the split cards and statistic grids.

### 🟡 Closed navigation remains in the keyboard focus order, `web/src/App.tsx:132`

**Problem**: Closing the drawer only translates its rail offscreen; its buttons remain focusable while their labels are hidden. After allowing the transition to settle, focusing Open navigation and pressing Shift+Tab focused the invisible Maintenance button at x=-282, right=-18. Only Escape restores focus explicitly; navigation selection and scrim dismissal have no corresponding focus handoff.

**Why it matters**: Keyboard users can land on controls outside the visible interface, and the hidden label plus aria-hidden icon leaves those controls without a useful accessible name. The passing axe sweep covers desktop routes and does not exercise this drawer state.

**Suggested fix**: Remove the closed mobile rail from interaction and the accessibility tree while retaining desktop navigation. Define focus behavior when opening, selecting a destination, and dismissing the drawer; verify both Tab directions and visible focus after closing.

## Strengths

- The implementation reuses the existing API helpers and Python service authorities. Bearer event authorization remains separate from browser session authorization; local HTTP webhook fixtures require an explicit backend environment flag, and external observations remain labelled as untrusted evidence.
- The added browser workflow verifies actual unauthorized/revoked rejection, event duplication, and the received webhook's HMAC and event identity. Data Connections snapshot handoff and unsubscribe-readiness boundaries also have connected browser coverage.
- A fresh Vite build reproduced the tracked CSS and JavaScript byte for byte. The existing macOS bundle contains the same frontend assets and independently retained a published message revision and staged restore across service stop/restart.

## Test coverage

All commands used disposable runtime state and external temporary build/test directories. No application code, tests, specs, or AGENTS files were edited; the findings file is this review's only repository write. Documentation edits by another actor appeared during the review and were inspected and preserved.

| Check | Status | Method, environment, and observed result |
| --- | --- | --- |
| Python regressions | PASS | macOS arm64, repository Python 3.15.2 environment; `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider --ignore=tests/e2e --basetemp=/tmp/mailforge-029-independent-pytest`: 248 passed. |
| Browser and accessibility suite | PASS | Same environment, installed Chromium, committed FastAPI-served assets; `pytest -q -p no:cacheprovider tests/e2e` with external basetemp: 6 passed. This includes the desktop axe route sweep, not comprehensive mobile accessibility qualification. |
| Frontend validation | PASS | Available Node 24: Oxlint completed with React effect/dependency warnings; application and tooling TypeScript projects passed no-emit checks. Vite built into an external temporary directory; CSS/JS hashes matched the tracked and packaged assets. Default shell Node 18 initially failed to launch Oxlint; rerunning with Node 24 resolved the environment issue. |
| Direct paths, refresh, Back/Forward | PASS | Existing browser acceptance plus independent direct navigation to the reviewed workbenches. Stable URL/view behavior worked in the exercised routes. |
| Grouped discovery and mobile drawer | FAIL | Group definitions contain the required areas; 390×844 runtime measurements and scrolling exposed unreachable lower destinations and invisible keyboard focus. Escape restoration passed. |
| Narrow layout and primary actions | FAIL | Mobile Campaigns creation remains visible but loses purpose; shared split cards remain two columns. Mobile Message Studio itself successfully previewed, saved, and published in the packaged runtime. |
| Retryable failure behavior | FAIL | Independent browser response interception reproduced a successful document creation followed by a detail-read failure; Retry duplicated the document. |
| Related-work links and service handoffs | PASS | Related links navigate existing views without creating records; the browser suite completed SQLite preview/snapshot and the existing revision-based Data Studio handoff. No replacement client business store was introduced. |
| Integration Hub operations | PASS | Browser suite independently passed client issue/revoke, unauthorized/revoked event rejection, accepted/duplicate events, subscription preview/enable, actual loopback delivery, HMAC, and stable event identity. |
| Email Standards operations | PASS | Browser suite saved an identity, recorded offline diagnostics, validated MIME, saved an endpoint, and retained `one_click_not_fully_verified`; campaign purpose propagation remains the separate failure above. |
| Maintenance preferences | FAIL | Independent browser save/navigation/reload plus `/runtime` readback demonstrated durable America/New_York with UTC displayed on reopen. |
| Packaged persistence and staged recovery | PASS | Existing arm64 bundle started through its bundled Python CLI against a new temporary data directory. At 390×844, browser create/preview/save/publish and backup/staged restore completed. Stop/start against the same directory retained the published subject/revision and staged restore database. The review-owned services were stopped afterward. |
| Existing service/authorization boundaries preserved | FAIL | Service ownership, Host/session/CSRF, bearer scope, fixture gate, and synthetic execution remain in place; the mobile campaign purpose omission nevertheless changes the policy input supplied by the retained workflow. |
| Complete package manifest | NOT VERIFIED | The available bundle's manifest check reported 202 mismatches, all `.pyc` files; no non-bytecode mismatch was found. Mutable bytecode prevents claiming a clean whole-bundle checksum or attributing the executor's earlier pre-launch result to this review. |
| External-provider, signing/notarization, production qualification | SKIPPED | Outside the authorized local synthetic review. No external provider, public receiver, live send, or real recipient operation was used. |

The suite provides meaningful happy-path and boundary coverage but misses the four reproduced major cases, full-height drawer navigation, closed-drawer focus, and shared mobile column behavior. Add targeted regressions with the fixes; broad additional test scaffolding is unnecessary.

### Evidence provenance and limits

Read the supplied review rubric, root/web AGENTS files, DESIGN.md, and specs 0019, 0018, and 0017. Reviewed the branch diff against the supplied remote-main merge base, the affected component/service context, tests, documentation, compiled asset linkage, and local visual evidence.

The requested Desktop evidence ZIP was absent. The local `.orchestra/evidence/029/` tree was available, including the current mobile Message Studio, Campaigns, navigation and tablet Reports captures, desktop workbenches, and four working-state captures added during review. The capture summary now describes 26 screenshots: its no-console-error/no-failed-request observations apply only to the baseline 22; the four working-state captures explicitly lack that instrumentation. Those supplied captures were treated as executor evidence. Runtime measurements, fault injection, tests, and the packaged durability checks described above were independently performed by this reviewer.

Tablet Reports at 1024px showed a three-column filter layout within the viewport (page width 1024px), consistent with its current capture. Fresh mobile Message Studio inspection showed a usable stacked editor and isolated preview; the partly visible rail in the supplied studio/navigation captures was not treated as proof of its settled state. The independently reproduced drawer overflow and focus defects are the actionable findings.

This report records independent engineering review, not business acceptance, production authorization, or merge approval. Scope checkboxes were not edited because the explicit read-only instruction limits repository writes to this findings file.
