# 0012. Operational intelligence and reporting

**Date**: 2026-10-28
**Status**: Proposed

## Summary

Mailforge needs one trustworthy operational reporting capability that explains
what campaigns, providers, inbound processing, workflows, List Tools, Data
Studio, and Message Studio have actually observed. This decision adds a
canonical Python reporting service that derives bounded reports from the
existing SQLite authority and reuses the same calculations through the API,
CLI, and browser. It deliberately keeps unknown, simulated, ambiguous, and
externally unverified evidence distinct instead of filling gaps with invented
success metrics.

## Context

Mailforge already persists the facts needed for useful local reporting, but
they are exposed through separate workbenches and service-specific summaries.
Campaign intents and attempts, provider diagnostics, inbound trust and review
records, workflow runs, List Tools and Data Studio jobs, and Message Studio
revisions use different grains and timestamps. A dashboard that joins these
records ad hoc in React would eventually double-count recipients, confuse
attempts with recipients, or present SMTP acceptance as inbox delivery.

The 022 milestone must provide a practical local control room without adding a
second analytics database, hosted telemetry, tracking pixels, or a distributed
event platform. It must remain useful with synthetic/local evidence, preserve
the existing privacy and access boundary, and be bounded under realistic local
history sizes. Historical reports must remain reproducible from durable source
records and must identify what was not measured.

## Requirements

**User stories**:

- As a local operator, I want a single Reports and Insights area so that I can
  understand campaign, provider, inbound, workflow, and processing health.
- As an operator investigating a number, I want to open the supporting
  campaign, attempt, job, review, or workflow records so that every result is
  traceable.
- As an operator comparing periods, I want explicit local-time ranges and
  previous-period comparisons so that trends do not depend on the current
  recipient policy or provider configuration.
- As an operator exporting a report, I want bounded CSV, JSON, and printable
  HTML output containing definitions, filters, timestamps, and limitations.

**Acceptance criteria** (the contract):

- **AC-1**: A versioned metric-definition registry documents meaning, source
  records, time field, grain, numerator/denominator, filters, evidence class,
  and missing-data behavior for every reported metric.
- **AC-2**: A canonical `ReportingService` returns stable, bounded reports
  for an explicit IANA timezone and UTC date range, supporting today,
  yesterday, last 7 days, last 30 days, custom ranges, and a matching prior
  period without loading all history into Python.
- **AC-3**: Campaign reporting distinguishes campaigns, audience/snapshot
  recipients, eligible/excluded/suppressed/pending/prepared intents, attempts,
  accepted submissions, transient failures, permanent failures, ambiguous
  outcomes, cancellations, retries, pauses, and execution duration.
- **AC-4**: Provider reporting distinguishes configuration/readiness, local
  protocol tests, external-unverified state, diagnostic phase/status,
  authentication/TLS/refusal/rate-limit evidence, consecutive failures, and
  account-filtered activity.
- **AC-5**: Inbound reporting preserves source/account/folder, classification,
  trusted versus untrusted evidence, correlation, delivery/complaint/
  unsubscribe signals, review backlog/age, rule matches, applied actions,
  duplicate prevention, and processing failures without treating untrusted
  evidence as confirmed delivery failure.
- **AC-6**: Workflow reporting reflects durable workflow/enrollment/run/step
  states, waiting and scheduling delays, execution duration, retries, review
  requirements, failures, cancellations, and idempotency observations without
  treating queued or waiting work as completed.
- **AC-7**: List Tools and Data Studio reporting exposes source, valid,
  rejected, duplicate, provider-group, unknown-provider, input/output,
  transformation-error, cancellation, recovery, artifact, duration, and
  measured-throughput facts where durable records provide them.
- **AC-8**: Message Studio reporting remains scoped to document/revision counts,
  published usage, render validation failures, merge-field/attachment errors,
  and measured rendering performance; it never exposes personalized content in
  aggregate reports.
- **AC-9**: Reports provide campaign, provider, workflow, inbound, job, and
  artifact drilldowns whose counts reconcile to the underlying source records
  and whose pagination/order are stable.
- **AC-10**: Deterministic operational insights explain an observed condition,
  its supporting record identities, and a next inspection action for repeated
  provider failures, ambiguous submissions, stale workflow waits, review
  backlogs, repeated processing failures, missing evidence, and abnormal job
  duration; insights never mutate operational state or claim unsupported cause.
- **AC-11**: The browser has an accessible Reports and Insights area with
  summary cards, time/filter controls, trends, breakdowns, evidence notes,
  loading/error/empty states, responsive tables/charts, keyboard navigation,
  and links to existing canonical workbenches.
- **AC-12**: API and CLI report surfaces reuse the same `ReportingService`
  calculations, reject invalid or excessive filters, use parameterized SQL,
  and never expose raw SQL, unrestricted filesystem paths, credentials, or
  private message bodies in aggregate output.
- **AC-13**: CSV, JSON, and printable HTML exports are bounded, safe against
  spreadsheet formula injection, include report type, range, generation time,
  filters, metric-definition version, units, evidence classification, and
  limitations, and do not include recipient-level content unless an explicit
  detailed export is authorized.
- **AC-14**: Repeated report requests, duplicate operational events,
  late-arriving notifications, missing/corrupt evidence, DST boundaries,
  backup/restore, and migrations preserve metric correctness and historical
  unknown states.
- **AC-15**: Representative local reporting workloads measure aggregate and
  detail latency, export throughput, peak memory, query plans, and database
  impact across thousands of events and tens of thousands of attempts/jobs;
  indexes are added only where measurements justify them.
- **AC-16**: Connected synthetic browser acceptance covers data processing,
  campaign preparation/execution outcomes, exclusions, trusted/untrusted
  inbound evidence, workflow success/failure, Reports and Insights filtering,
  comparison, drilldown, reconciliation, export, browser/service restart, and
  backup/restore without real-provider activity.

## Options considered

### Option 1: One canonical reporting service over existing SQLite records

Add typed report inputs, metric definitions, SQL-backed aggregates, drilldown
queries, insights, and export preparation in one Python application service.
The API, CLI, and browser call that service.

**Pros**:

- Preserves existing authorities and local-first operation.
- Makes metric grain, timestamps, evidence, and missing-data semantics explicit
  once.
- Rebuilds naturally from durable records and needs no new operational system.

**Cons**:

- Complex aggregate SQL must be measured and indexed carefully.
- SQLite report concurrency shares resources with operational work.

### Option 2: Frontend-owned analytics over existing endpoints

Load domain lists into React and calculate charts and comparisons in the
browser.

**Pros**:

- Small initial backend change.
- Flexible presentation experiments.

**Cons**:

- Duplicates metric semantics across clients and cannot safely handle large
  history.
- Encourages inconsistent grains, denominators, privacy leakage, and stale
  calculations.

### Option 3: Separate analytics database or event platform

Publish operational facts into a second reporting store or hosted analytics
service.

**Pros**:

- Can isolate large analytical workloads at a later scale.
- May support richer historical slicing.

**Cons**:

- Creates a competing source of truth, reconstruction and delivery failure
  problems, and a privacy/telemetry boundary the product does not need.
- Adds operational cost before the repository has measured SQLite limits.

## Decision

**Chosen option**: Option 1: One canonical reporting service over existing
SQLite records.

Implement `ReportingService` in `src/mailforge/application/reporting.py` with
typed bounded filters, a versioned in-code metric registry, explicit UTC/local
time conversion, SQL aggregate queries, stable detail pagination, deterministic
insights, and safe export preparation. Add only measured indexes through the
next additive CampaignStore migration; do not add a reporting database or
frontend metric calculations.

**Implementation skills**: `design` (`.agents/skills/design/`) · `data-integrity` (`.agents/skills/data-integrity/`) · `security` (`.agents/skills/security/`) · `performance` (`.agents/skills/performance/`) · `test` (`.agents/skills/test/`) · `check` (`.agents/skills/check/`)

## Rationale

The existing SQLite schema already records the authoritative facts and the
local operator is the current trust boundary. Keeping reporting beside those
records makes source identity, late evidence, and backup/restore behavior
auditable without synchronizing a second database. The service boundary also
prevents the browser, CLI, and API from quietly developing different metric
definitions.

Frontend-only analytics is rejected because the required history and drilldown
surface would either load too much data or silently change denominators. A
separate analytics platform is rejected until measured workload evidence shows
SQLite is insufficient; it would violate the local-first and no-telemetry
constraints before solving a demonstrated problem.

## Feature design

**Data model sketch**:

No new reporting fact tables are required. Existing records remain authoritative:

| Source | Grain and key fields | Reporting use |
|---|---|---|
| `campaigns`, `campaign_transitions`, `campaign_audiences`, `campaign_audience_recipients` | campaign/run and audience-recipient records; campaign/status transition timestamps | campaign lifecycle, snapshot size, pauses/failures, audience evidence |
| `delivery_intents`, `prepared_messages`, `delivery_attempts`, `operational_events` | one recipient intent, one prepared message, one attempt, one event | eligibility, preparation, attempts, retry/outcome history, operational evidence |
| `provider_accounts`, `provider_profiles`, `provider_diagnostics`, `provider_test_submissions` | account/configuration and diagnostic/test observations | readiness, protocol diagnostics, account activity and evidence class |
| `inbound_sources`, `inbound_runs`, `inbound_messages`, `inbound_classifications`, `inbound_correlations`, `inbound_reviews`, `inbound_actions`, `inbound_rule_runs` | source/run/message/classification/review/action | inbound acquisition, trust, review, rules and failure reporting |
| `automation_workflows`, `automation_enrollments`, `automation_runs`, `automation_step_history`, `automation_waits`, `automation_action_correlations` | workflow/enrollment/run/step/wait | workflow health, delay, retries, failure and idempotency reporting |
| `list_processing_jobs`, `list_processing_artifacts`, `data_studio_jobs`, `data_studio_artifacts`, `data_studio_consumers` | processing job/artifact/consumer | List Tools and Data Studio throughput, counts, failures, artifacts |
| `message_documents`, `message_revisions`, `message_campaign_snapshots` | document/revision/campaign snapshot | authoring and template usage counts without personalized content |

`MetricDefinition` is a typed immutable registry record, not a persisted fact
table. Each definition has `key`, `label`, `meaning`, `source_tables`,
`time_field`, `grain`, `numerator`, `denominator`, `filters`, `evidence_class`,
and `missing_data_behavior`. Report results carry the registry version,
definition keys, source counts, and evidence notes.

**State transitions**: Reporting is read-only. It observes existing durable
  state machines and never changes campaign, provider, inbound, workflow, or
  processing state.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/reports/summary` | GET | `preset`, `start`, `end`, `timezone`, `provider_ref`, `campaign_id`, `workflow_id` | report envelope, metrics, trends, insights, evidence notes | existing local session/CSRF boundary for browser; CLI local authority | 400 invalid range/timezone/filter; 413 excessive range |
| `/api/v1/reports/campaigns` | GET | summary filters plus `limit`, `offset` | campaign rows with counts and source links | same | 400/413 |
| `/api/v1/reports/campaigns/{id}` | GET | range, timezone, pagination | campaign metrics, recipient-state breakdown, attempt rows | same | 404, 400 |
| `/api/v1/reports/providers` | GET | summary filters, account filter, pagination | account readiness, diagnostics and activity | same | 400 |
| `/api/v1/reports/workflows` | GET | summary filters, workflow filter, pagination | workflow/run health and delays | same | 400 |
| `/api/v1/reports/inbound` | GET | summary filters, source/account filter, pagination | classification/trust/review breakdown and rows | same | 400 |
| `/api/v1/reports/jobs` | GET | summary filters, kind, pagination | List Tools/Data Studio job rows and artifacts | same | 400 |
| `/api/v1/reports/export` | GET | report type, filters, `format=csv|json|html` | bounded download with report metadata | existing local export boundary; detailed export not included | 400, 413, 415 |
| `mailforge reports summary` | CLI | same typed filter flags | JSON report envelope | local filesystem authority | parser/range errors |
| `mailforge reports export` | CLI | report type, filters, format, output path | safe bounded file | local filesystem authority | invalid path/range/format |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Resolve range | UTC start/end and local label | preset/custom input plus IANA `timezone`; conversion uses standard-library `zoneinfo` |
| Campaign summary | campaign count, audience size, intent states, attempts/outcomes | named campaign, audience, intent, prepared-message, and attempt tables; attempt time is `delivery_attempts.created_at` |
| Provider summary | account status and diagnostic evidence | `provider_accounts.status`, `provider_profiles`, `provider_diagnostics`, `provider_test_submissions`; diagnostic `observed_at` is the observation time |
| Inbound summary | acquired/processed/trusted/untrusted/review/action counts | inbound source/run/message/classification/correlation/review/action tables; acquisition uses `acquired_at`, reviews use `created_at` |
| Workflow summary | active/enrolled/running/waiting/completed/failed/retry/delay counts | workflow/run/step/wait tables; run creation/start/completion fields determine each measure |
| Processing summary | source/output/rejected/error/duration/throughput/artifact counts | `list_processing_jobs`, `list_processing_artifacts`, `data_studio_jobs`, and `data_studio_artifacts`; job start/completion timestamps |
| Message summary | document/revision/published/usage/error counts | Message Studio document/revision/snapshot records; no body or personalized context |
| Trend/comparison | period values and deltas | same metric definition and denominator executed against current and equal-duration prior UTC ranges |
| Drilldown link | canonical record type/id and display label | source primary keys and existing route identifiers; no raw filesystem path |
| Insight | finding, evidence references, next action | deterministic thresholds over report rows plus source record IDs; unknown evidence produces a limitation, not zero |
| Export metadata | type, range, filters, generated time, definition version, limitations | request inputs plus report envelope and metric registry |

**Key invariants**:

- A recipient count is derived from distinct `delivery_intents` or snapshot
  recipient keys; an attempt count is derived from `delivery_attempts` and is
  never substituted for recipients.
- Accepted SMTP/provider submissions remain `accepted_submission`; no report
  labels them inbox-delivered without trusted inbound evidence.
- Ambiguous, unknown, simulated, and externally unverified observations remain
  separate categories; missing rows are not silently converted to zero when
  the source is unavailable.
- Every aggregate has one documented time field and one grain. Comparisons
  reuse the exact metric definition and duration.
- Detail queries are parameterized, bounded, stable ordered, and paginated.
- Reporting is read-only and does not record duplicate derived facts.
- Aggregate output excludes message bodies, personalized content, credentials,
  raw SQL, and unrestricted filesystem paths.
- Reports include `generated_at`, timezone/range, definition version, filters,
  evidence classification, and limitations.

**Security model**:

The local operator is the existing authority. Browser endpoints use the
existing loopback/session/CSRF checks. CLI reports require local filesystem
access to the configured SQLite authority. Aggregate reports expose no
recipient addresses or personalized content. Campaign and recipient detail
drilldowns return only the minimum existing metadata needed to reconcile a
metric; detailed recipient exports are not part of this milestone. SQL uses
bound parameters, filters have explicit limits, exports use safe filenames and
formula-injection protection, and diagnostics are redacted through existing
secret checks. No real-provider calls or telemetry are introduced.

**Configuration required**:

- No new environment variables, credentials, providers, or hosted services.

**Critical test scenarios**:

- Synthetic campaign with accepted, transient, permanent, ambiguous, and
  retried attempts reconciles recipient and attempt grains, verifies **AC-3**,
  **AC-9**, and **AC-14**.
- Trusted and untrusted inbound DSN/complaint records remain distinct and
  review backlog counts only pending reviews, verifies **AC-5** and **AC-14**.
- Workflow run history separates waiting, failed, completed, and retried steps,
  verifies **AC-6**.
- Data Studio/List Tools jobs report counts and throughput from durable job
  records, verifies **AC-7**.
- Invalid timezone/range, oversized range, SQL-like filter text, pagination
  overflow, and formula export inputs are rejected or escaped, verifies **AC-12**
  and **AC-13**.
- DST boundary and equal-duration prior comparison return stable local labels and
  matching definitions, verifies **AC-2**, **AC-14**.
- Repeated browser/API requests return the same source-backed report and do not
  create rows, verifies **AC-9**, **AC-14**.
- Backup/restore of a populated synthetic authority preserves report results,
  verifies **AC-14** and **AC-16**.

## Build plan

The repository has no recorded scope approach; this plan assumes a Tracer
Bullet: prove one complete summary path through SQLite, Python, API, CLI, and
browser first, then add each domain drilldown and export without introducing a
parallel metric implementation.

1. Add the architecture/reporting contract and metric-definition registry
   shape, including evidence classes, filter limits, range resolution, and
   source/grain metadata; satisfies **AC-1**, **AC-2**, **AC-12**.
2. Implement the canonical SQL-backed `ReportingService` summary and stable
   drilldown query helpers over existing tables; add only measured indexes in
   one additive schema migration if query plans justify them; satisfies
   **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-9**.
3. Add deterministic insight rules, evidence/limitation envelopes, and safe
   CSV/JSON/HTML export preparation; satisfies **AC-10**, **AC-13**, **AC-14**.
4. Expose versioned report API routes and CLI commands that delegate entirely
   to the service; add validation and source-link contracts; satisfies
   **AC-9**, **AC-12**, **AC-13**.
5. Build the Reports and Insights browser area using the existing DESIGN.md
   contract, with filters, summary/trends, breakdowns, drilldown links, notes,
   empty/error/loading states, keyboard access, and responsive tables; satisfies
   **AC-11**, **AC-16**.
6. Add focused metric/reconciliation/security/timezone/performance tests and a
   connected synthetic browser journey covering the packet’s 40-step flow;
   satisfies **AC-14**, **AC-15**, **AC-16**.
7. Measure representative reports and exports, tune only evidence-backed
   indexes/query plans, run full regression and accessibility validation, and
   update the capability atlas, user/API/CLI docs, changelog, and repository
   truth; satisfies **AC-15**, **AC-16**.

## Consequences

**Positive**:

- One metric authority prevents dashboard/API/CLI drift and makes reports
  explainable from durable records.
- Operators gain useful diagnostics without external telemetry or a second
  database.
- Explicit evidence classes prevent overclaiming delivery or provider health.

**Negative / tradeoffs**:

- Reporting SQL and metric definitions become a maintained application surface.
- SQLite shares resources with reporting queries; measured indexes and bounded
  pagination are required.
- Some requested metrics remain `not measured` until a canonical source records
  them.

**Neutral**:

- Existing domain services remain authoritative and report generation is
  read-only.
- The first dashboard is local/synthetic and does not qualify real providers,
  inbox placement, engagement, or hosted production readiness.

## Follow-up

- [ ] Revisit whether a rebuildable projection is justified only after the
  measured reporting workload exceeds bounded SQLite query limits.
- [ ] Define a separate authorized detailed-recipient export decision if
  operators later need recipient-level report files.
- [ ] Add real-provider qualification only through a separate release and
  authorization decision.

## Migration plan

**Strategy**: no migration needed unless measured query plans justify additive
reporting indexes; then use one additive, rollback-safe schema migration.

**Phases**:

1. Ship service/API/CLI/browser calculations directly over existing records and
   collect local timing evidence without changing source facts.
2. Add only indexes proven necessary by `EXPLAIN QUERY PLAN` and workload
   measurements; keep all derived values rebuildable from existing tables.

**Rollback**: revert reporting code and remove unused additive indexes only in a
   controlled migration; no operational records are rewritten or deleted.

**Risks**: a mistaken metric definition can mislead operators even when SQL is
  technically correct; source/grain tests and the registry are mandatory.
