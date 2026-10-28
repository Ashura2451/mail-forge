# 0013. Campaign operations workspace

**Date**: 2026-10-28
**Status**: Proposed
**Task**: `MAILFORGE-CAMPAIGN-OPERATIONS-023`

## Summary

Mailforge's existing campaign, audience, message, provider, scheduler, policy,
outbound, and reporting services already contain the durable facts needed for a
useful campaign workspace, but the browser currently exposes them as separate
actions. This decision adds one thin campaign-operations facade and a coherent
browser/API/CLI workflow for drafting, preflight, preparation, scheduling,
synthetic execution, recovery, monitoring, and outcome inspection. It keeps
SQLite and the existing campaign/outbound authorities canonical and keeps
unrestricted live campaign sending unavailable.

## Context

The current Campaigns view can create a list- or segment-backed campaign and
invoke preparation, preview, simulation authorization, synthetic execution,
pause, resume, and cancel. Those controls are difficult to use as a complete
operator journey: draft fields are not editable through a durable update
contract, scheduling and readiness findings are not shown together, recipient
and attempt evidence is split across lower-level endpoints, and progress is
not presented as a campaign workspace.

The repository already has immutable audience and Message Studio snapshots,
policy-aware preparation, phase-aware attempts, durable leases, transactional
pacing, provider account records, timezone-aware schedule primitives, local
session/CSRF controls, backup/restore, and ReportingService `022.1`. The main
risk is introducing another source of truth or making a simulation look like a
live send. The implementation therefore needs a small orchestration boundary
that composes these services while leaving their ownership intact.

## Requirements

**User stories**:

- As a local campaign operator, I want one workspace to configure and inspect a
  campaign so that I do not have to assemble a safe send from unrelated tools.
- As an operator, I want preflight findings and immutable preparation evidence
  so that blockers, warnings, audience, content, sender, and limits are clear
  before execution.
- As an operator, I want durable synthetic scheduling and controls so that a
  browser refresh, close, or service restart does not lose campaign state.
- As an operator, I want recipient and attempt drilldowns plus an operational
  export so that persisted outcomes can be reconciled to ReportingService.

**Acceptance criteria**:

- **AC-1**: Campaign creation and draft editing use the existing CampaignStore
  campaign record, support name/description/purpose/audience/message/sender
  fields, reject stale updates, and do not expose credentials or raw message
  content in ordinary campaign lists.
- **AC-2**: The workspace can select a static list, dynamic segment revision,
  or supported prepared Data Studio audience and displays source identity,
  revision/hash when available, matched, eligible, excluded, and invalid
  counts from the canonical audience/eligibility services.
- **AC-3**: Message selection uses a published Message Studio revision or the
  existing bounded legacy template path, previews at least two synthetic
  recipients when available, and reports subject, preheader, text, HTML,
  merge-field warnings, conditional warnings, and attachment metadata without
  recreating the renderer in the browser.
- **AC-4**: A campaign binds one explicit provider account or the clearly named
  synthetic account; the workspace displays configuration/readiness/status and
  never rotates accounts automatically or exposes credential values.
- **AC-5**: Canonical preflight returns typed `pass`, `warning`, `blocker`, or
  `not_verified` findings for audience, content, sender, execution, and policy,
  with a source and actionable operator explanation. A local check never claims
  external provider qualification.
- **AC-6**: Preparation creates or reuses immutable audience and message
  snapshots, pins source revision/hash and prepared content, and later edits to
  the source list, segment, or Message Studio document do not rewrite the
  prepared campaign.
- **AC-7**: Schedule configuration uses the existing `ScheduleSpec` and
  `ScheduleEngine`, persists UTC facts and the requested IANA timezone, rejects
  invalid/DST-gap/ambiguous local times without an explicit fold, and supports
  immediate, scheduled, window, and bounded recurring synthetic execution.
- **AC-8**: Only the existing explicit simulation authorization can start a
  synthetic execution. No new generic live-send switch, live campaign route,
  automatic account switching, or external provider activity is introduced.
- **AC-9**: Pause, resume, cancel, and repeated consequential commands are
  idempotent or return the existing durable state; accepted or in-flight
  protocol outcomes are never relabeled as canceled.
- **AC-10**: Account pacing, concurrency, cooldown, daily/hourly/minute/domain
  limits, worker leases, policy revision checks, retry bounds, and ambiguity
  handling continue to be enforced by the existing outbound/store authorities
  across concurrent workers.
- **AC-11**: Monitoring shows persisted campaign state, prepared/pending,
  excluded, attempted, accepted, transient, permanent, ambiguous, canceled,
  account, queue, last activity, next eligible time, and diagnostic events with
  simulation/external evidence labels.
- **AC-12**: Recipient and attempt drilldowns are bounded and paginated, expose
  eligibility reason, account/policy identity, message revision, preparation
  time, attempt count, protocol phase, outcome, correlation/evidence identity,
  and review requirement without exposing personalized bodies by default.
- **AC-13**: Campaign reporting delegates to ReportingService `022.1`; recipient
  and attempt totals reconcile to the same SQLite rows, accepted submission is
  not inbox delivery, ambiguous outcomes remain distinct, and current policy
  changes do not rewrite historical facts.
- **AC-14**: JSON/CSV operational exports include campaign identity, range or
  snapshot metadata, counts, units, evidence class, definition version, and
  safe bounded recipient/attempt rows; CSV formula injection is escaped.
- **AC-15**: API and CLI expose equivalent draft, audience, content, sender,
  preflight, prepare, schedule, simulation-mode, control, progress, recipient,
  attempt, report, and export operations using validated bounded inputs and
  consistent errors.
- **AC-16**: Browser behavior is complete at desktop and narrow widths, works
  with keyboard and screen readers, has loading/error/empty/conflict states,
  uses existing Mailforge design tokens, and makes simulation/no-live-delivery
  boundaries visible.
- **AC-17**: Existing CampaignService, OutboundService, EligibilityService,
  MessageStudioService, SegmentationService, ProviderOperationsService,
  scheduler, worker, ReportingService, and backup/restore regression suites
  remain green.
- **AC-18**: A connected local synthetic lifecycle exercises draft → audience →
  message preview → account/pacing → preflight → prepare → schedule → run →
  pause/resume/cancel/restart → recipient/attempt drilldown → report/export,
  including accepted, temporary, permanent, ambiguous, suppression, and no-live
  provider behavior.
- **AC-19**: Preparation, reservation, progress, and report queries have
  reproducible bounded measurements for small and larger synthetic campaigns;
  no throughput result is presented as SMTP deliverability.
- **AC-20**: Backup/restore of a populated campaign workspace preserves the
  exact campaign, schedule, snapshots, attempts, events, and ReportingService
  reconciliation results in a disposable restored database.

## Options considered

### Option 1: Add a thin CampaignOperationsService facade (chosen)

Add orchestration methods for workspace composition, draft updates, preflight,
schedule/control contracts, drilldowns, and exports while delegating campaign
state, eligibility, rendering, policy, worker, and reporting work to existing
services.

**Pros**:

- Gives the browser/API/CLI one coherent contract without duplicating engines.
- Keeps load-bearing domain rules in their existing authorities.
- Makes cross-domain acceptance and reconciliation testable.

**Cons**:

- Adds one application service and response vocabulary to maintain.
- Some legacy campaign fields remain in the existing configuration JSON until a
  later schema decision proves a normalized table necessary.

### Option 2: Expand CampaignService and web handlers directly

Put every new orchestration concern into CampaignService and add endpoint-local
queries for the workspace.

**Pros**:

- Fewer new files and a smaller apparent service graph.
- Reuses existing campaign method names directly.

**Cons**:

- Mixes lifecycle authority with UI aggregation, provider readiness, and export
  concerns.
- Makes API/CLI parity and bounded cross-domain testing harder.

### Option 3: Introduce a campaign-operations database/projection

Copy campaign configuration, audience, progress, and outcomes into a reporting
or operations store optimized for the workspace.

**Pros**:

- Could simplify read models at much larger scale.
- Allows independent UI query tuning.

**Cons**:

- Creates a competing campaign authority and reconciliation burden.
- Duplicates private recipient/content data and complicates backup/restore.
- Not justified by the current local SQLite workload or product boundary.

## Decision

**Chosen option**: Option 1: Add a thin CampaignOperationsService facade.

Build the campaign workspace as a local, read/write orchestration boundary over
CampaignStore and the existing application authorities. Persist draft operation
settings in the existing campaign configuration JSON, schedules in
`campaign_schedules`, and all consequential facts through existing transitions,
attempts, leases, pacing reservations, message/audience snapshots, and
operational events. Add no new database or migration unless implementation
evidence proves an existing structure cannot preserve a required invariant.

**Implementation skills**: `architect` (`.agents/skills/architect/`) · `design` (`.agents/skills/design/`) · `develop` (`.agents/skills/develop/`) · `data-integrity` (`.agents/skills/data-integrity/`) · `security` (`.agents/skills/security/`) · `impact-analysis` (`.agents/skills/impact-analysis/`) · `performance` (`.agents/skills/performance/`) · `test` (`.agents/skills/test/`) · `check` (`.agents/skills/check/`) · `document` (`.agents/skills/document/`) · `sync` (`.agents/skills/sync/`)

## Rationale

The repository already has the correct durable ownership and strong local
synthetic safety gates. A facade is the smallest change that can make the
operator journey coherent without moving state or reimplementing policy and
transport rules. A new operations database would create exactly the split
authority this milestone is required to avoid, while direct endpoint logic
would make it too easy for browser and CLI behavior to diverge.

## Feature design

**Data model sketch**:

| Authority | Key fields used by 023 | Relationship / constraint |
|---|---|---|
| `campaigns` | id, name, description, audience source/revision/run, message revision, provider account, configuration JSON, status, authorization, timestamps | one campaign owns schedules, transitions, intents, snapshots, events |
| `campaign_schedules` | mode, timezone, scheduled UTC/local input, window, weekdays, pacing, cooldown, recurrence, catch-up, fold | one-to-one with campaign; existing unique campaign key |
| `campaign_audiences` / `campaign_audience_recipients` | source identity, revision/hash, evaluation time, matched/prepared/excluded counts, recipient states | one campaign/run snapshot; source revision is immutable once prepared |
| `message_campaign_snapshots` / `prepared_messages` | revision id, content hash, document/MIME identity, attachment manifest, prepared time | one snapshot per campaign/run/revision; prepared message per intent |
| `delivery_intents` / `delivery_attempts` | recipient identity, eligibility reason, state, lease, attempt number, phase, outcome, errors, timestamps | one intent per campaign/contact; unique attempt number per intent |
| provider accounts / sender policies | account ref, status/readiness, sender policy and configured limits | explicit campaign binding; no implicit rotation |
| operational events | aggregate, event type, payload, occurrence | append-only audit trail for draft/control/schedule actions |

No new table is planned. `configuration_json` receives only bounded non-secret
operation settings such as communication purpose, sender display fields,
execution mode, and operator-visible pacing overrides; message content and
credentials remain owned by Message Studio and provider credential references.

**State transitions**:

Existing domain transitions remain authoritative:

`draft → validating → ready → scheduled → running → completing → completed`

with existing `paused`, `pausing`, `failed`, and `canceled` branches. Preflight
blockers prevent preparation/scheduling; an unresolved ambiguous or deferred
execution remains paused/reviewable. Resume starts the existing worker; cancel
marks only not-yet-submitted work canceled. No second state machine is added.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/campaigns` | GET/POST | bounded list; draft fields | sanitized campaign/workspace | local session | 400, 409 |
| `/campaigns/{id}` | GET/PATCH | fields plus `expected_updated_at` | updated workspace | local session + CSRF for PATCH | 404, 409, 422 |
| `/campaigns/{id}/workspace` | GET | run key, preview limit | draft, audience, content, account, preflight, schedule, progress, report | local session | 404 |
| `/campaigns/{id}/preflight` | POST | optional refreshed draft inputs | typed findings and readiness summary | local session + CSRF | 400, 404 |
| `/campaigns/{id}/schedule` | PUT | `ScheduleSpec` fields | persisted schedule and next eligibility | local session + CSRF | 400, 409 |
| `/campaigns/{id}/prepare` | POST | run key, sender/content overrides | immutable preparation report | local session + CSRF | 400, 409 |
| `/campaigns/{id}/authorize` | POST | simulation declaration/mode | idempotent authorization | local session + CSRF | 400, 409 |
| `/campaigns/{id}/simulate` | POST | no external destination | worker start/status | local session + CSRF | 400, 409 |
| `/campaigns/{id}/pause`, `/resume`, `/cancel` | POST | idempotency/revision where needed | durable state/control result | local session + CSRF | 400, 409 |
| `/campaigns/{id}/progress` | GET | bounded | persisted progress and next activity | local session | 404 |
| `/campaigns/{id}/recipients` | GET | limit/offset/run key | privacy-safe intents and eligibility | local session | 400, 404 |
| `/campaigns/{id}/attempts` | GET | limit/offset | phase-aware attempts and evidence | local session | 400, 404 |
| `/campaigns/{id}/export` | GET | format and bounded filters | safe JSON/CSV report | local session | 400, 413 |

Existing lower-level endpoints remain compatible and delegate to the same
services. CLI subcommands mirror the workspace, preflight, schedule, control,
recipient, attempt, report, and export actions.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Draft form | name, description, purpose, source, revision, account, sender, pacing | validated request plus campaign configuration and existing IDs |
| Audience card | matched, eligible, excluded, invalid, source/hash/evaluated time | SegmentationService audience snapshot or list membership plus EligibilityService |
| Content card/preview | revision, content hash, subject, preheader, text/HTML, warnings, attachments | MessageStudioService published revision, renderer, asset manifest |
| Account card | name, provider/profile, status, readiness, limits, suspension | ProviderOperationsService and sender policy; credential values never returned |
| Preflight | severity, code, explanation, action, evidence class | named authority checks over campaign, audience, message, provider, schedule, policy |
| Plan | prepared count, revision, account, schedule, pacing, retry behavior, mode | immutable snapshots, campaign schedule, sender policy, execution mode |
| Progress | state, counts, queue, worker, last/next activity | CampaignService report, intents, attempts, transitions, schedule engine |
| Recipient/attempt row | eligibility, revision, attempt count, phase, outcome, evidence | delivery intents, prepared messages metadata, delivery attempts, inbound correlation IDs |
| Report/export | definitions, totals, comparison, units, limitations | ReportingService `022.1` and campaign source rows |

**Key invariants**:

- CampaignStore remains the only durable campaign authority.
- Audience and message revisions are pinned before preparation and cannot be
  silently replaced on a prepared run.
- Current eligibility is re-evaluated before dispatch; policy changes do not
  rewrite prior attempts.
- A recipient intent is not an attempt, an SMTP/provider acceptance is not
  inbox delivery, and ambiguous post-DATA outcomes are never automatically
  replayed.
- Consequential controls use existing transactional/idempotent store methods;
  concurrent workers share leases, pacing reservations, and sender limits.
- All list and export queries are bounded and stable ordered.
- Simulation is the only browser/CLI execution mode; live SMTP remains behind
  the existing explicit application gate and has no new campaign route.

**Security model**:

The local session/origin/CSRF boundary remains the API boundary. The local
operator may manage local campaign state, but campaign mutations require CSRF
and validate resource IDs, expected revisions, and state transitions. The UI
and ordinary exports expose no credentials, raw message bodies, personalized
content, or unrestricted paths. Provider readiness is configuration/local
observation only. No external network operation is invoked by preflight,
preparation, browser monitoring, or synthetic acceptance.

**Configuration required**:

- No new environment variables or credentials.
- Existing provider credential references and live-send gate remain unchanged.

**Critical test scenarios**:

- Draft update conflict and sanitized workspace response, verifies **AC-1** and
  **AC-15**.
- Static list and segment/Data Studio source reconciliation before and after
  preparation, verifies **AC-2**, **AC-6**, and **AC-13**.
- Published Message Studio revision preview for two contacts and source-edit
  immutability, verifies **AC-3** and **AC-6**.
- Provider readiness warning versus synthetic account and no credential leak,
  verifies **AC-4**, **AC-5**, and **AC-8**.
- DST gap/fold schedule rejection and due execution, verifies **AC-7**.
- Concurrent worker reservation, pacing, suppression-after-prepare, bounded
  retry, ambiguous outcome, pause/resume, cancel race, and restart recovery,
  verifies **AC-9**, **AC-10**, and **AC-11**.
- Recipient/attempt pagination and report/export reconciliation, verifies
  **AC-12**, **AC-13**, and **AC-14**.
- Disposable backup/restore retains workspace and report facts, verifies
  **AC-20**.
- Connected browser lifecycle at desktop/narrow viewports with keyboard and
  accessibility checks, verifies **AC-16** and **AC-18**.

## Build plan

The repository has no scope approach recorded. Use a production-oriented
Tracer Bullet: establish one thin draft → preflight → prepare → monitor path
through Store, service, API, CLI, browser, and tests first, then thicken it with
schedule/control, drilldowns, export, and connected recovery.

1. Add typed campaign-operation vocabulary and `CampaignOperationsService` as a
   thin facade over CampaignService, OutboundService, MessageStudioService,
   SegmentationService, ProviderOperationsService, and ReportingService;
   satisfy **AC-1** through **AC-6**, **AC-13**, and **AC-15**.
2. Add atomic draft updates, preflight findings, schedule persistence/control
   orchestration, and audit events using existing CampaignStore tables and
   transitions; satisfy **AC-5**, **AC-7** through **AC-10**, and **AC-20**.
3. Add bounded recipient/attempt drilldowns and campaign operational exports
   that reuse ReportingService definitions and source identities; satisfy
   **AC-11** through **AC-14**.
4. Extend API and CLI contracts with shared request models, conflict/error
   handling, and no-live-send enforcement; satisfy **AC-8**, **AC-9**, **AC-15**.
5. Replace the current Campaigns action strip with a feature-based responsive
   workspace component using existing DESIGN.md tokens, then run browser and
   accessibility acceptance; satisfy **AC-1** through **AC-18**.
6. Add focused unit/integration/API/browser/recovery tests, synthetic connected
   lifecycle and reproducible campaign benchmarks; satisfy **AC-17** through
   **AC-20** and the complete validation requirements.

## Consequences

**Positive**:

- One understandable operator journey over the existing durable authorities.
- API, CLI, and browser use the same preflight, state, and reporting semantics.
- Preparation, recovery, and evidence remain inspectable after refresh/restart.
- No new analytics store, SMTP implementation, renderer, eligibility engine, or
  account-rotation path.

**Negative / tradeoffs**:

- The facade must translate several existing service response shapes.
- Some draft operation settings remain bounded JSON until a measured need for
  normalized columns emerges.
- Browser acceptance remains synthetic/local and cannot prove external delivery.

**Neutral**:

- Message render-failure metrics remain `not_measured` when no durable source
  record exists.
- Existing five Oxlint effect warnings and the Starlette/httpx warning remain
  outside this milestone unless the changed surface makes them actionable.

## Follow-up

- [ ] Run the complete 48-step local connected acceptance, including browser
  close/reopen, service restart, account suspension, duplicate-submission check,
  and backup/restore reconciliation.
- [ ] Obtain an independent post-fix review from a different model/provider;
  local review evidence must not be described as approval.
- [ ] Decide separately whether durable render-validation failure records are
  warranted; keep them `not_measured` until the canonical authority exists.
