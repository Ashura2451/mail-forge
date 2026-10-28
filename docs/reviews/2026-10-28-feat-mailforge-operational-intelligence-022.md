# Review, feat/mailforge-operational-intelligence-022, 2026-10-28

**Reviewed by**: inherited reviewer model (author unknown; independence degraded because a different model family was unavailable)
**Scope**: 18 task files, branch vs `origin/main` at `a3e5060`
**Verdict**: Changes requested

## Summary

The requested privacy, custom-range, comparison, detail-error, inbound/jobs
filter, workflow-collection, insight-identity, and CLI-test changes are present.
The focused reporting/API/browser checks and frontend validation pass. Remaining
issues are concentrated in filter propagation and browser export/error edge
cases, so the implementation is improved but not yet ready for acceptance.

## Major

### 🟠 Summary aggregates still ignore selected workflow/source scope, `src/mailforge/application/reporting.py:867-879,836-839`
**Problem**: `_workflow_metrics()` applies `workflow_id` to runs and waits but not to workflow status totals or step failures. `_inbound_metrics()` applies source/account/folder scope to messages, classifications, trust, and reviews but its run aggregate at lines 836-839 has no source scope. The jobs detail filter exists, but `_processing_metrics()` does not apply `job_kind` to the summary processing metrics.
**Why it matters**: A filtered report can show a mixture of scoped and unscoped values, so summary cards and comparisons do not reconcile to the selected workflow/source/job population.
**Suggested fix**: Propagate the same scope predicates and bound parameters to every aggregate query, or explicitly mark each unscoped value as a current/global snapshot. Add seeded tests with two workflows, two inbound sources, and both job kinds.

### 🟠 Insight evidence can still point outside the selected range/filter, `src/mailforge/application/reporting.py:1015-1029,1042-1046,1060-1064,1079-1083`
**Problem**: Provider-failure, pending-review, waiting-run, and stale-wait `record_ids` queries do not reuse the selected time or domain predicates. For example, provider IDs select every failed diagnostic, pending reviews select every pending review, and waiting IDs ignore `workflow_id`; only the ambiguous-attempt and processing-job paths reuse report predicates.
**Why it matters**: An insight can state a scoped count while linking to unrelated records, undermining traceability and making operator investigation misleading.
**Suggested fix**: Build record-ID queries from the same scoped query fragments used to compute each metric, with bounded limits and matching time semantics; test that every returned ID belongs to the selected report.

### 🟠 Custom-range detail exports and summary reload errors remain broken, `web/src/App.tsx:86-92,110`
**Problem**: The custom range is sent to the summary/detail API, but the CSV link only includes `preset`, `timezone`, and no `start`/`end`; a custom detail export therefore requests `preset=custom` without required dates and returns 400. Separately, `reload()` has no `try/catch`, so summary refreshes triggered by invalid custom ranges, invalid timezone input, or network errors become unhandled promise rejections instead of using the visible error state.
**Why it matters**: A primary browser workflow can display valid custom results but fail when exporting them, and the user receives no actionable error for summary failures.
**Suggested fix**: Include encoded `start` and `end` in the export URL and route summary reload failures through a visible `role="alert"` state; add browser/API tests for custom export and invalid timezone/date responses.

## Minor

### 🟡 Comparison coverage is narrower than the declared reporting surface, `web/src/App.tsx:70-71,107`
The browser now renders a prior-period campaign-count delta, but it does not expose comparison values for attempts, accepted/ambiguous outcomes, inbound, workflows, providers, or processing. This is acceptable as a first visible comparison only if the remaining metrics are documented as unavailable; otherwise extend the comparison presentation or add explicit `not measured` labels.

### 🟡 Focused tests do not exercise the newly fixed filter and insight paths, `tests/test_reporting.py:91-143`, `tests/test_web.py:96-114`
The new tests cover allowlisted campaign detail, CLI summary/export, and invalid timezone, but no test seeds records outside the selected source/workflow/job scope or asserts insight IDs stay within scope. The browser test also does not exercise custom dates, custom export, summary error handling, or populated workflow definitions.

## Strengths

- Campaign detail now uses an explicit allowlist and the privacy regression test proves configuration/message text is absent.
- Custom date controls, prior-period campaign comparison, detail refresh, visible detail errors, inbound/jobs filters, workflow definitions, and bounded insight record IDs are all represented in the updated implementation.
- CLI tests now exercise canonical summary/export behavior and invalid timezone handling; focused reporting/API/browser checks pass.

## Test coverage

Executed `PYTHONPATH=src uv run pytest -q tests/test_reporting.py tests/test_web.py tests/e2e/test_browser_acceptance.py tests/e2e/test_accessibility.py` successfully: 26 passed, with one existing Starlette/httpx deprecation warning. Executed `git diff --check`, Ruff on affected Python files, and Node 24 frontend typecheck/build/lint successfully; oxlint emitted React `set-state-in-effect` warnings. Full-suite completion, populated cross-scope reconciliation, and custom-export browser behavior remain unverified.

Scope: no matching row was found in `docs/scope/`; no scope checklist was ticked.
