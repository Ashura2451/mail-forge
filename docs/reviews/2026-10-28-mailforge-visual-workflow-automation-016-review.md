# Review, Mailforge visual workflow automation 016, 2026-10-28

**Task:** `MAILFORGE-VISUAL-WORKFLOW-AUTOMATION-016`
**Branch:** `feat/mailforge-visual-workflow-automation-016`
**Reviewed by:** same-model inline review; independent reviewer unavailable
because the reviewer-agent spawn returned `agent thread limit reached`.
**Verdict:** local synthetic slice verified; full planner acceptance is not
claimed.

## Review result

The implementation adds one durable `AutomationService`, SQLite migration 8,
versioned graph revisions, bounded validation, source-event deduplication,
segment-aware conditions, UTC waits, allowlisted local actions, durable leases,
the local API/CLI, and an accessible browser builder. No provider, mailbox,
credential, or recipient data was accessed.

The following limits remain material and are recorded rather than presented as
completed qualification:

- Trigger kinds are represented and accepted through the canonical enrollment
  service, but automatic hooks from every existing contact/list/inbound source
  and a hosted-style scheduler are not claimed. The local operator or an
  existing trusted caller must submit the source event while the service is
  running.
- Wait execution is durable and UTC-based. IANA zones are validated, but a
  local-wall-clock recurrence/DST resolver and an always-on missed-occurrence
  scheduler are outside this local slice.
- Workflow campaign actions call canonical campaign preparation only; they do
  not send mail. Existing campaign/SMTP ambiguity protections remain owned by
  the existing outbound path and are not re-qualified as workflow delivery.
- Browser acceptance covers the workflow revision lifecycle and the existing
  local route suite. The complete 50-step synthetic scenario in the planner
  packet, including every automatic trigger source, backup/restore, and
  workflow-specific process interruption, is not claimed as passed.

## Acceptance matrix

| Criterion | Status | Evidence |
| --- | --- | --- |
| A. Specification and authority | PASS | `docs/specs/0007-visual-workflow-automation.md`; explicit planner authorization; status remains Proposed per repository spec convention |
| B. Canonical automation authority | PASS | `AutomationService` is shared by API, CLI, and in-process worker |
| C–D. Durable drafts and immutable published revisions | PASS | Migration 8 tables; revision-pinning tests; active edits create a new draft |
| E. Bounded graph validation | PASS | Cycle, reachability, branch, depth, size, wait, unsafe-key, and action-reference checks |
| F. Practical trigger enrollment | PARTIAL | Manual and typed event enrollment are implemented; automatic source hooks/scheduler are not claimed |
| G. Event/enrollment idempotency | PASS | Dedupe key, once-per-contact policy, cross-workflow event handling, regression tests |
| H. Segmentation conditions | PASS | Existing `SegmentationService`, pinned segment revision, TRUE/FALSE/UNKNOWN handling |
| I. Durable waits/restart recovery | PARTIAL | SQLite waits and controlled-clock recovery pass; no full process-interruption/scheduler qualification |
| J–K. Canonical actions and policy | PASS (synthetic) | List membership and campaign preparation delegate to existing services; no send path from automation |
| L. Ambiguous delivery protection | INHERITED / NOT WORKFLOW-QUALIFIED | Existing outbound protocol tests pass; workflow action is prepare-only |
| M. Idempotent/recoverable execution | PASS (local) | Durable run state, action correlations, bounded worker, lease contention regression |
| N. Pause/resume/cancel | PASS | API, CLI service, and browser controls are present; broader interruption matrix remains unclaimed |
| O. Functional visual builder | PASS | Add/configure/remove nodes, separate condition branches, connections, validation, save/publish/activate |
| P. Browser workflow monitoring | PASS (slice) | `test_browser_visual_automation_revision_lifecycle`; existing browser suite 4 passed |
| Q. API/CLI shared authority | PASS | Routes and `mailforge automation` commands delegate to `AutomationService` |
| R. Security invariants | PASS (local) | Existing host/session/CSRF controls, bounded input, unsafe graph-key rejection, secret scan |
| S. Accessibility/responsive behavior | PASS | Existing axe/keyboard/mobile suite passed; frontend lint has two non-failing React effect warnings |
| T. Existing behavior | PASS | Full Python suite: 175 passed |
| U. No real data | PASS | Disposable SQLite and synthetic fixtures only |
| V. Complete connected scenario | PARTIAL | Core local path passes; planner's full trigger/restart/backup matrix is not claimed |
| W. Performance evidence | PASS (local target) | 1,000 enrollment and advancement measurements recorded in `docs/performance/` |
| X. Engineering validation | PASS | Pytest, E2E, TypeScript/Vite, Oxlint, Ruff, lock, package, secrets, and diff checks |
| Y. Review truthfulness | PASS | Independent review unavailable is explicitly recorded |
| Z. Capability/documentation sync | PASS | Capability atlas, baseline, integration, web, user, performance, and changelog updates |
| AA–AB. Publication | PENDING | Requires final staged audit, exact commit, push, and remote-head verification |
| AC. Main integration | NOT DONE | Feature branch only; no main merge or publication claim |

## Validation evidence

- `.venv/bin/pytest -q` — 175 passed, one existing Starlette/httpx warning.
- `.venv/bin/pytest -q tests/e2e` — 4 passed.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run lint` — passed with two
  existing React effect warnings.
- `PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build` — passed.
- `uv run ruff check src tests` and format check — passed.
- `uv lock --check`, `uv build`, `bash scripts/check-secrets.sh`, and
  `git diff --check` — passed.

No independent approval, real-provider qualification, inbox-delivery claim, or
main-branch integration is asserted by this review.
