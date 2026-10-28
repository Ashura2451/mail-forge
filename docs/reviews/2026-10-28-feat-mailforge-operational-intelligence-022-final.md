# Review, feat/mailforge-operational-intelligence-022, 2026-10-28

**Reviewed by**: inline reviewer (author model/tool unknown; independent worker/model unavailable)
**Scope**: 22 working-tree paths, uncommitted vs `origin/main` at `a3e5060`
**Verdict**: Changes requested

## Summary

The change adds a centralized reporting service, API/CLI surfaces, exports,
focused tests, and a Reports & Insights browser route. The implementation has
good bounded input validation, parameterized report queries, explicit evidence
labels, and privacy-focused campaign-detail tests. It is not ready for merge:
several filtered reports cannot reconcile to their own drilldowns, List Tools
artifact reporting is incomplete, and the browser surface does not expose the
required scoped filters or source-record links.

## Major

### 🟠 Filtered aggregates and drilldowns do not share one scope, `src/mailforge/application/reporting.py:529-558,842-869,881-919`
**Problem**: Inbound action counts ignore `source_ref`, `account_ref`, and
`folder`, while inbound message/classification/trust counts apply those
filters. Workflow status totals are current snapshots rather than range-scoped,
and `workflows()` does not apply `workflow_id` to workflow definitions or step
failure counts. These values are returned beside range- and filter-scoped runs.
**Why it matters**: A report can show a filtered count and a different
unfiltered breakdown, so operators cannot reconcile summary cards, insights,
and drilldown records to the selected population.
**Suggested fix**: Centralize each domain's scope predicate and time semantics,
reuse it for every aggregate and detail query, and label any intentionally
current-snapshot metric separately from ranged metrics. Add seeded cross-scope
tests for two workflows and two inbound sources/accounts/folders.

### 🟠 List Tools artifacts are omitted and job pagination is not globally stable, `src/mailforge/application/reporting.py:614-647,924-977`
**Problem**: `jobs()` and `_processing_metrics()` query only
`data_studio_artifacts`; the durable `list_processing_artifacts` table is
never included. In addition, each job table is limited/offset before the two
result sets are merged and sorted, so a page can omit records that belong on
the global page boundary.
**Why it matters**: The requested List Tools artifact/output reporting is
incorrect, and paginated processing drilldowns can skip or duplicate records
as the two sources interleave. This violates the stable reconciliation
contract and can make failed-job evidence incomplete.
**Suggested fix**: Query both artifact authorities with explicit `kind` and
source identity, then apply one deterministic global order and pagination
after a bounded union (or use a SQL union with one outer limit/offset). Add
tests with interleaved List Tools/Data Studio jobs and artifacts.

### 🟠 Reports & Insights lacks required scoped filters and source-record links, `web/src/App.tsx:51-117`
**Problem**: The browser exposes only range, timezone, and custom dates. It has
no provider/account, workflow, inbound source/account/folder, job-kind, or
campaign scope controls. Detail rows are rendered as plain text and do not
link to campaign attempts, workflow runs, inbound reviews, processing jobs,
or artifacts; the summary provider breakdown and insight evidence IDs are not
interactive.
**Why it matters**: Core requested workflows—account filtering, operational
drilldown, and inspecting the records supporting a metric—are available only
through lower-level APIs, not through the product surface. The browser
acceptance currently proves navigation and export existence, not scoped
reconciliation or record inspection.
**Suggested fix**: Add bounded controls for the supported filters, propagate
them to summary/detail/export requests, and render canonical links or clearly
labelled drilldown actions for the source records returned by the service.
Exercise those paths in connected browser acceptance.

## Minor

### 🟡 Metric registry omits required unit and deduplication/origin metadata, `src/mailforge/application/reporting.py:38-65,97-212`
The registry records source, time field, grain, numerator/denominator,
filters, evidence class, and missing-data behavior, but not the packet's
required unit, deduplication identity, or simulated-versus-external origin
field for each metric. Those semantics are partly implicit in prose and the
envelope, which makes metric review and future additions less reliable.

### 🟡 Provider insight evidence returns profile references, not diagnostic record identities, `src/mailforge/application/reporting.py:1064-1070`
The provider-failure insight selects `profile_ref` even though
`provider_diagnostics` has a durable `id`. Multiple failures for one profile
therefore collapse to the same evidence identity and cannot point to the
specific observation that triggered the finding.

## Strengths

- `ReportFilters` bounds ranges, pagination, timezone input, and enumerated
  job kinds; report SQL uses bound parameters.
- The focused suite covers registry shape, read-only behavior, DST conversion,
  formula-safe CSV output, campaign-detail privacy, API errors, CLI parity,
  browser navigation, and accessibility routing.
- Evidence envelopes explicitly distinguish local synthetic observations from
  real-provider qualification and SMTP acceptance from inbox delivery.

## Test coverage and residual unknowns

Executed:

`PYTHONPATH=src uv run pytest -q tests/test_reporting.py tests/test_web.py tests/e2e/test_browser_acceptance.py tests/e2e/test_accessibility.py`

Result: **26 passed**, with one existing Starlette/httpx deprecation warning.

Not verified in this final review: full Python regression, representative
performance/query-plan measurements, large-history/export memory behavior,
the complete 40-step connected synthetic acceptance, backup/restore report
reconciliation, and a genuinely independent model/provider review. No scope
row matched this feature, so no scope checklist was ticked.

## Executor follow-up after review snapshot

The review above was captured before the final implementation pass. The
reported issues were addressed in the working tree as follows:

- inbound action/run, workflow, job-kind, insight-evidence, and provider
  diagnostic scopes now reuse the selected report filters and time semantics;
- jobs use one globally ordered SQL union and include both Data Studio and
  List Tools artifact authorities;
- the browser exposes the supported provider, workflow, inbound, account,
  folder, and job-kind filters, propagates them to detail/export requests, and
  provides workbench actions for source inspection;
- metric registry entries now declare unit, deduplication, and origin.

Executor validation after those fixes: `222 passed` in the full Python suite,
`5 passed` in browser/accessibility acceptance, TypeScript/build/oxlint pass
(with the existing five React effect warnings), `uv lock --check`, package
build, secret scan, and `git diff --check` all pass. This follow-up is not a
new independent review or an approval override; the original reviewer verdict
remains the recorded review snapshot, and no genuinely independent post-fix
approval was available.
