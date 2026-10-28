# 0007, visual workflow automation

**Status**: Proposed
**Task authority**: `MAILFORGE-VISUAL-WORKFLOW-AUTOMATION-016`
**Date**: 2026-10-28

## Summary

Mailforge will add a local visual automation builder for durable, synthetic-only
contact workflows. Operators can define a versioned trigger/condition/wait/action
graph, validate and publish it, enroll synthetic contacts, and inspect each run
after restart or failure. The feature stays inside the existing FastAPI/React,
SQLite, and bounded in-process worker architecture; it does not send live mail
or add hosted infrastructure.

## Context

Mailforge already has durable contacts, lists, segment revisions, campaign
policy, inbound evidence, operational events, and a supervised synthetic worker.
The missing capability is a single operator-visible way to connect those
authoritative facts into a workflow whose definition, enrollment, waiting, and
execution history survive browser closure, process restart, and retries.

The feature is deliberately local-first. There is no user/org authorization
system beyond the existing loopback local-operator boundary, and no provider or
recipient data is required. Workflow actions must remain explainable and
bounded: message actions prepare or enqueue synthetic campaign work through the
canonical campaign service, while arbitrary code, SQL, shell commands, private
paths, credentials, and live SMTP are excluded.

> ⚠️ Premise note: “visual automation” can easily become a second scheduler and
> dispatch engine. That would create competing policy and delivery authorities.
> This spec treats the visual graph as a durable orchestration definition and
> routes audience evaluation, eligibility, campaign preparation, and synthetic
> communication through existing services.

## Requirements

**User stories**:

- As a local operator, I want to build a readable workflow from “When this
  happens” through conditions, waits, actions, and “End” so that repeatable
  contact operations are reviewable rather than hidden in scripts.
- As a local operator, I want immutable published revisions and durable run
  history so that editing a workflow cannot change work already in progress.
- As a local operator, I want simulation-only enrollment and execution so that
  I can exercise policy, suppression, waiting, retry, and recovery without
  contacting a real provider.

**Acceptance criteria** (the contract):

- **AC-1**: An operator can create, edit, save, validate, publish, activate,
  pause, archive, and list versioned workflow definitions through the local web
  console; published revisions are immutable and active edits create a new draft.
- **AC-2**: The workflow editor renders a readable visual graph with one entry
  trigger, explicit connections, condition branches, waits, actions, and End;
  the same graph can be edited through keyboard-accessible controls without
  drag-and-drop.
- **AC-3**: Validation rejects malformed JSON, missing references, multiple
  entry triggers, unreachable termination, cycles, invalid branch structure,
  excessive nodes/depth/conditions, unbounded wait/action configuration, and
  unsupported side effects before publication or activation.
- **AC-4**: Supported triggers include manual enrollment, contact imported,
  contact added to a selected list, scheduled occurrence, and trusted
  reconciled inbound event; every enrollment records source identity, workflow
  revision, contact/subject, trigger, timestamps, provenance, correlation, and
  a durable deduplication key.
- **AC-5**: Repeating the same source event cannot create a second enrollment;
  the default re-entry policy is once per contact per workflow, and a workflow
  cannot self-enroll from its own internal events.
- **AC-6**: Conditions reuse canonical segmentation semantics, pin an exact
  segment revision by default, preserve TRUE/FALSE/UNKNOWN outcomes, and never
  override the canonical eligibility/suppression decision.
- **AC-7**: Wait nodes store UTC due times derived from explicit relative or
  absolute configuration, validate IANA time zones and DST ambiguity, and apply
  a bounded missed-occurrence/catch-up policy after downtime or restart.
- **AC-8**: Actions are limited to add/remove static-list membership, prepare or
  enqueue supported synthetic campaign work through `CampaignService`, record a
  structured event/operator note, and End; every action is idempotent and
  auditable, and no live SMTP is reachable from workflow execution.
- **AC-9**: Runs persist the workflow revision, current node, status, start/end
  times, wait due time, retry count, failure reason, and audit/step history;
  valid transitions include PENDING, RUNNING, WAITING, PAUSING, PAUSED,
  COMPLETED, FAILED, CANCELED, and REQUIRES_REVIEW.
- **AC-10**: Worker claims are durable and bounded; restart recovery resumes
  safe pending/waiting work, bounded retries distinguish pre-side-effect,
  confirmed, and ambiguous outcomes, and ambiguous external outcomes stop in
  REQUIRES_REVIEW without a duplicate submission.
- **AC-11**: The local API and CLI expose the same application service for
  workflow CRUD, revisions, validation, publication, activation, pausing,
  synthetic enrollment, run controls, history, and step diagnostics with
  bounded pagination and existing session/CSRF/host protections.
- **AC-12**: Browser acceptance covers contact/list/segment/campaign/workflow
  setup, publish/activate, event enrollment and deduplication, condition and
  wait progression, suppression, browser close/reopen, revision pinning,
  failure/retry, ambiguous no-duplicate handling, restart, pause/resume, and
  persisted history using synthetic data only.
- **AC-13**: The feature preserves existing backend/CLI/browser behavior,
  meets the existing design system, exposes visible focus and semantic labels,
  works at desktop and narrow mobile widths, and adds no critical or serious
  automated accessibility findings.
- **AC-14**: SQLite migration 8 is additive, backup/restore and integrity checks
  cover workflow state, concurrent claims are safe, and documented performance
  measurements cover validation, enrollment deduplication, and bounded worker
  throughput for the local target.

## Options considered

### Option 1: In-process SQLite orchestration with an accessible DOM canvas

Store graph definitions and run state in the existing database, execute bounded
steps through one `AutomationService`, and render nodes as an ordered visual
canvas plus explicit connection/branch editors.

**Pros**:

- Reuses existing authorities, migrations, process lock, worker, and security
  boundary with the fewest new moving parts.
- The same graph has a deterministic keyboard path and does not require a
  browser-only spatial interaction model.

**Cons**:

- It is intentionally local-scale and needs careful query/index design as
  workflow history grows.
- A free-form spatial editor is deferred; the first canvas is structured and
  bounded rather than an unconstrained diagramming tool.

### Option 2: Add a maintained graph/canvas dependency and keep execution local

Use a React graph library for node placement and edge interaction while keeping
the same SQLite and application-service model.

**Pros**:

- Richer spatial editing and visual routing can arrive sooner if the product
  needs arbitrary layouts.

**Cons**:

- Adds a dependency and accessibility integration surface before a measured need
  exists; graph libraries still need a separate keyboard editing model.
- It does not simplify the durable execution problem.

### Option 3: External workflow engine or hosted queue

Move scheduling, retries, and workflow execution to a hosted/orchestrated
service.

**Pros**:

- More operational scale and mature distributed scheduling primitives.

**Cons**:

- Contradicts the local-first, synthetic-only, single-machine boundary and adds
  credentials, deployment, recovery, and privacy concerns without evidence of
  that scale requirement.

## Decision

**Chosen option**: Option 1: In-process SQLite orchestration with an accessible DOM canvas.

Implement one canonical `AutomationService` backed by SQLite migration 8. The
web UI will present a visual ordered graph and a non-drag connection editor;
the API and CLI call the same service. Existing segmentation, eligibility,
campaign preparation, policy, inbound trust, and operational event services
remain authoritative.

**Implementation skills**: `architect` (`Mailforge/.agents/skills/architect`) ·
`develop` (`Mailforge/.agents/skills/develop`) · `data-integrity`
(`Mailforge/.agents/skills/data-integrity`) · `security`
(`Mailforge/.agents/skills/security`) · `design` (`Mailforge/.agents/skills/design`) ·
`test` (`Mailforge/.agents/skills/test`) · `check` (`Mailforge/.agents/skills/check`) ·
`release` (`Mailforge/.agents/skills/release`)

## Rationale

The existing product already solves the hard policy and delivery questions in
the local monolith. A second engine or a new queue would split authority and
make the most important failure cases—suppression, ambiguous delivery, and
restart recovery—harder to reason about. SQLite transactions, explicit leases,
and the existing supervised worker are sufficient for the requested synthetic
local workload when node and history limits are enforced.

The structured canvas is a product decision, not a shortcut around
accessibility. Every node and edge has a form control representation, and the
graph renderer is derived from the same validated definition. A maintained
graph library remains a future option if measured operator workflows require
free placement; it is not a prerequisite for the first complete vertical slice.

## Feature design

**Data model sketch**:

- `automation_workflows`: `id`, unique `name`, `description`, lifecycle
  `DRAFT|ACTIVE|PAUSED|ARCHIVED`, default re-entry policy, current draft and
  published revision ids, timestamps, actor metadata.
- `automation_revisions`: immutable `id`, workflow FK, revision number, state
  `DRAFT|PUBLISHED`, canonical graph JSON/hash, limits summary, created/published
  timestamps and actor. Unique `(workflow_id, revision_number)` and hash.
- `automation_nodes`: revision FK, stable node id, kind, display label,
  bounded config JSON, position/order; unique `(revision_id, node_id)`.
- `automation_edges`: revision FK, source/target node ids, branch key,
  ordering; unique `(revision_id, source, target, branch_key)`.
- `automation_triggers`: revision FK, trigger kind/config, enabled flag;
  unique `(revision_id, trigger_key)`.
- `automation_source_events`: source identity, event kind, subject/contact,
  provenance/correlation JSON, occurred/received timestamps, dedupe key;
  unique dedupe key.
- `automation_enrollments`: workflow/revision/source-event/contact identity,
  re-entry key, status, timestamps and provenance; unique source event and
  `(workflow_id, contact_id, reentry_key)`.
- `automation_runs`: enrollment/revision FK, status, current node, started,
  waiting/due/completed timestamps, retry/failure fields, worker lease; unique
  enrollment.
- `automation_step_history`: run/node attempt, input/output summary, status,
  side-effect phase, error, timestamps; unique `(run_id, node_id, attempt)`.
- `automation_waits`: run/node, due UTC, original timezone/local value,
  catch-up policy, resumed timestamp; unique run/node.
- `automation_action_correlations`: run/node, idempotency key, action kind,
  target and canonical campaign/event correlation, outcome; unique idempotency.
- `automation_audit`: workflow/revision/run/action, actor, event, safe metadata,
  timestamp. All JSON fields are checked for secret-bearing keys/values.

All foreign keys use `ON DELETE CASCADE` only for revision-owned child rows;
source events, enrollments, runs, and audit history are not hard-deleted by UI
operations. Pagination is mandatory for history and event lists.

**State transitions**:

- Workflow: `DRAFT → ACTIVE → PAUSED → ACTIVE`; `DRAFT → ARCHIVED`,
  `PAUSED → ARCHIVED`; active edits create a new `DRAFT` revision and do not
  mutate published revisions.
- Revision: `DRAFT → PUBLISHED`; published revisions are immutable.
- Run: `PENDING → RUNNING → WAITING → RUNNING → COMPLETED`; `RUNNING →
  PAUSING → PAUSED → RUNNING`; any safe running state may become `CANCELED`;
  bounded retry leads to `FAILED`; ambiguous side effects lead to
  `REQUIRES_REVIEW`.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/automations` | GET/POST | page/limit; name, description | paginated workflows; id/status | local session | 400, 409 |
| `/automations/{id}` | GET/PATCH | workflow id; metadata | workflow and revision summary | local session | 404, 409 |
| `/automations/{id}/revisions` | GET/POST | page; graph definition | revisions; draft id | local session | 400, 409 |
| `/automations/{id}/validate` | POST | revision id or graph | errors, warnings, graph summary | local session | 400 |
| `/automations/{id}/publish` | POST | draft revision id | published revision | local session | 409 |
| `/automations/{id}/activate` | POST | published revision id | active status | local session | 409 |
| `/automations/{id}/pause` | POST | none | paused status | local session | 409 |
| `/automations/{id}/enroll` | POST | contact id/source event | enrollment/run id, dedupe result | local session | 400, 409 |
| `/automations/{id}/runs` | GET | page/limit/status | paginated run summaries | local session | 404 |
| `/automations/runs/{run_id}` | GET | run id | run, steps, waits, actions | local session | 404 |
| `/automations/runs/{run_id}/pause\|resume\|cancel` | POST | none | durable control result | local session | 409 |
| `/automations/runs/{run_id}/advance` | POST | synthetic clock/step bound | result and diagnostics | local session | 409 |

The CLI mirrors these operations under `mailforge automation` and emits JSON
for machine use; it does not introduce a second execution implementation.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Workflow list | name, lifecycle, revision, run counts | workflow/revision/run DB rows |
| Canvas | node labels, kinds, edges, branch names | canonical revision graph JSON |
| Trigger enrollment | contact, source identity, provenance, dedupe result | request plus source-event row and contact authority |
| Condition step | TRUE/FALSE/UNKNOWN and pinned revision | `SegmentationService` result and segment revision DB row |
| Wait step | due UTC, local explanation, catch-up result | node config, IANA zone, durable wait row, UTC clock |
| Message action | synthetic campaign/run correlation and policy outcome | `CampaignService`/`OutboundService`, eligibility, policy, campaign rows |
| Run history | current node, status, attempts, failure, side-effect phase | run/step/action/audit tables |
| UI status | local/simulation/no-network boundary | existing runtime status contract and fixed product policy |

**Key invariants**:

- A published revision is immutable and every run stores its revision id.
- A valid graph has exactly one entry trigger, at least one reachable End,
  explicit branch edges, no cycles, and bounded complexity.
- One source event can enroll at most once; default contact re-entry is once per
  workflow unless an explicit bounded policy says otherwise.
- No workflow action bypasses eligibility, suppression, policy reservations, or
  the canonical campaign service; message actions are synthetic only.
- A side effect has one durable idempotency key. Confirmed or ambiguous external
  outcomes cannot be silently retried.
- Wait due times are UTC and workers never execute before the durable due time.
- Every mutation and operator control creates safe operational/audit evidence.

**Security model**:

All endpoints are loopback-only local-operator endpoints behind the existing
Host/session/origin/CSRF middleware. Workflow definitions are treated as
untrusted input and may contain only allowlisted node kinds and bounded scalar,
list, and object values. No arbitrary code, SQL, shell, filesystem path,
credential, secret, URL fetch, or live SMTP capability is exposed. Event and
audit payloads use the existing secret-bearing value checks. List/run endpoints
paginate and cap limits; graph JSON, node count, depth, wait duration, and note
length are bounded.

**Configuration required**:

No new environment variables or credentials. The feature uses the existing
SQLite path, loopback web boundary, process lock, and synthetic transport.

**Critical test scenarios**:

- Happy path: create a workflow, publish/activate it, enroll a synthetic
  contact, evaluate a pinned segment, wait, perform a list/action note, and
  complete with durable history, verifying **AC-1**, **AC-2**, **AC-4**,
  **AC-6**, **AC-7**, **AC-8**, and **AC-9**.
- Failure case: submit cyclic/oversized graphs, duplicate an event, interrupt
  before/after a synthetic action, restart, and verify bounded retry or review
  without duplicate side effects, verifying **AC-3**, **AC-5**, **AC-10**, and
  **AC-14**.
- Auth/permission: reject requests without the existing session/CSRF boundary
  and reject unsupported action configuration, verifying **AC-8** and **AC-11**.
- Browser/responsive: complete the primary workflow with keyboard controls,
  reopen in a fresh session, and audit narrow viewport/focus/ARIA behavior,
  verifying **AC-2**, **AC-12**, and **AC-13**.

## Build plan

The implementation follows a tracer-bullet order: durable graph and one manual
enrollment first, then condition/wait/action execution, then publication and
controls, then browser polish and recovery evidence.

1. Add additive SQLite migration 8, domain vocabulary, safe JSON/graph limits,
   repository methods, backup/restore coverage, and atomic claim/idempotency
   helpers, satisfying **AC-3**, **AC-5**, **AC-9**, and **AC-14**.
2. Implement the canonical `AutomationService` with graph validation,
   draft/revision lifecycle, manual/source-event enrollment, condition
   delegation, waits, allowlisted actions, retry/review state transitions, and
   operational/audit events, satisfying **AC-1**, **AC-3**, **AC-4**, **AC-5**,
   **AC-6**, **AC-7**, **AC-8**, **AC-9**, and **AC-10**.
3. Extend the bounded runtime worker and restart reconciliation to claim and
   advance automation runs without competing process ownership, satisfying
   **AC-9**, **AC-10**, and **AC-14**.
4. Add versioned web API routes and CLI commands that delegate to the service,
   retaining pagination, session/CSRF, bounded payloads, and no-live-network
   behavior, satisfying **AC-1**, **AC-4**, **AC-8**, and **AC-11**.
5. Build the Automation console area using existing DESIGN.md tokens: ordered
   visual canvas, explicit keyboard edge editor, node configuration forms,
   validation/publish controls, run history and diagnostics, satisfying
   **AC-1**, **AC-2**, **AC-12**, and **AC-13**.
6. Add unit, integration, API, browser, accessibility, restart, backup/restore,
   concurrency, and bounded performance tests; update capability, API/CLI,
   user-operation, performance, changelog, and local truth documentation,
   satisfying **AC-6**, **AC-7**, **AC-10**, **AC-12**, **AC-13**, and **AC-14**.
7. Run the full validation matrix, obtain an independent read-only review when
   available, commit as `feat: add Mailforge visual workflow automation`, and
   publish only the feature branch, satisfying **AC-13** and **AC-14**.

## Consequences

**Positive**:

- Operators gain a durable, inspectable automation path without another system
  of record or provider connection.
- Existing policy, segmentation, inbound trust, and campaign behavior remain
  reusable and testable through one application authority.
- Revision pinning and step history make failure and recovery explainable.

**Negative / tradeoffs**:

- SQLite history and an in-process worker are bounded local infrastructure, not
  a distributed automation platform.
- The first visual editor favors clarity and keyboard access over free-form
  diagram layout.
- More durable tables and state transitions increase migration and test burden.

**Neutral**:

- A future measured need could justify a maintained graph library or worker
  extraction, but neither is part of this task.
- No live provider qualification, consent verification, hosted deployment, or
  external delivery evidence is produced.

## Follow-up

- [ ] Reassess a maintained graph/canvas library only after measured operator
  workflows show that the structured canvas limits task completion.
- [ ] Define a separate hosted/multi-user authorization decision before adding
  remote workers, organizations, or external provider execution.

## References

**Project sources**:

- `DESIGN.md`, existing visual and accessibility system
- `docs/specs/0004-architecture-consolidation.md`, local modular-monolith boundary
- `docs/specs/0005-local-web-application.md`, loopback web security contract
- `docs/specs/0006-audience-segmentation.md`, typed condition and revision semantics
- `src/mailforge/application/campaigns.py`, canonical campaign authority
- `src/mailforge/application/segmentation.py`, canonical audience authority
- `src/mailforge/infrastructure/campaign_store.py`, SQLite and migration authority
- `.aios/vision.md` and `.aios/architecture.md`, local-first product constraints

**Practices & standards**:

- SQLite transactional idempotency and lease-based worker claims
- Explicit state machines and immutable published revisions
- OWASP-aligned session, CSRF, input-boundary, and secret-handling practices
- WCAG 2.2 keyboard/focus and semantic control practices
