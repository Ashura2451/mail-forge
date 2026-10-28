# Spec 0006: audience segmentation

**Date**: 2026-10-28
**Status**: Accepted

**Implementation ratification**: RATIFIED FOR IMPLEMENTATION
**Authority**: Architect/Planner
**Ratified**: 2026-10-28
**Scope**: MAILFORGE-AUDIENCE-SEGMENTATION-015

The lifecycle was held Proposed through implementation and is now Accepted
after the 015 implementation and local verification.

## Summary

Add durable audience segmentation to Mailforge's existing local-first contact,
list, campaign, and SQLite system. Operators can define typed custom fields,
import them safely, build saved dynamic segments, maintain static lists, preview
matches, and prepare campaigns against an immutable audience snapshot. The
browser, CLI, and HTTP API all call one Python segmentation service; none is a
second rule or eligibility authority.

## Context

> Premise note: This topic spans contact attributes, imports, lists, segment
> definitions, evaluation, campaign preparation, and browser workflows. They
> are separate implementation surfaces but one load-bearing decision: how an
> audience is represented, evaluated, and frozen for a campaign. One coherent
> build spec avoids incompatible partial implementations without adding a
> second service or milestone.

Mailforge already owns normalized contact identity, provenance, lists and list
memberships, campaigns, delivery intents, suppression, and eligibility in
src/mailforge. CampaignStore is the SQLite authority and currently reports
schema version 6. CampaignService prepares delivery intents from a list and
EligibilityService evaluates mutable recipient policy. The local FastAPI and
React application delegates business behavior to those services. There is no
custom-field registry, saved segment model, segment evaluator, or campaign
audience revision.

The missing audience contract creates two failure modes: a browser or CLI can
implement a subtly different filter, and a campaign can change recipients
after review. This feature adds one bounded, parameterized evaluator and makes
preparation record the exact source and revision it used while still
rechecking current policy at dispatch.

This specification follows the authorized local-first modular-monolith
direction in .aios/architecture.md and the existing CampaignStore migration
contract. It is local synthetic functionality only: it does not qualify
provider delivery, consent, hosted identity, remote workers, or live SMTP/IMAP.

## Requirements

**User stories**:

- As a local operator, I want typed contact fields and safe structured imports
  so audience data remains useful without corrupting contact identity or policy
  evidence.
- As an operator, I want static lists and saved dynamic segments so I can
  reuse and inspect audiences without writing SQL or Python.
- As a campaign operator, I want count/contact previews and an immutable
  prepared audience so the campaign I review is the campaign that runs.
- As a browser user, I want the same workflows through accessible responsive
  controls, with actionable validation and conflict errors.

**Acceptance criteria** (the contract):

- **AC-1**: Schema migration from version 6 to version 7 is additive,
  idempotent, foreign-key safe, preserves existing data, and is covered on
  fresh, upgraded, interrupted/retried, and backup/restore databases.
- **AC-2**: Operators can create, validate, archive, and list typed custom
  fields of TEXT, NUMBER, BOOLEAN, DATE, or DATETIME with stable IDs and
  controlled choices; missing differs from an explicitly stored empty string.
- **AC-3**: Structured CSV/JSON imports support explicit field mappings and
  typed validation, preserve address-only imports, reconcile duplicates and
  existing values deterministically, record conflicts, quarantine invalid
  values, and require explicit validated authorization for overwrites.
- **AC-4**: Static list membership reuses existing list authority, is unique
  and idempotent, records auditable add/remove history, and never changes
  consent, suppression, or eligibility evidence.
- **AC-5**: Operators can save, edit, archive, and inspect dynamic segments;
  each edit creates an immutable validated revision with a canonical definition
  hash and optimistic-concurrency protection.
- **AC-6**: The versioned filter AST supports ALL, ANY, NOT, registered fields,
  type-aware operators, nested evaluation, explicit missing semantics, and
  three-valued UNKNOWN propagation. Only TRUE is a final match; SQL injection,
  arbitrary SQL/Python, invalid fields/values, and excessive definitions are
  rejected with actionable errors.
- **AC-7**: Segment preview returns a deterministic evaluation timestamp,
  revision/hash, matching count, bounded paginated contacts, validation/errors,
  and practical criteria explanations without UI-side evaluation.
- **AC-8**: Campaign preparation can select a static list or exact segment
  revision, records source identity/revision/hash/evaluation time/statistics,
  and creates a transactionally consistent durable recipient snapshot through
  existing delivery intents or focused audience metadata.
- **AC-9**: Repeated preparation is idempotent; post-preparation contact,
  attribute, list, or segment changes do not alter prepared recipients; current
  suppression and eligibility are rechecked before dispatch; recurring runs
  evaluate a fresh run-specific audience.
- **AC-10**: Interrupted or contended preparation cannot leave a partially
  executable audience; rollback/retry behavior is deterministic and bounded.
- **AC-11**: CLI and /api/v1 expose the same service for field management,
  import mapping/reconciliation, static lists, segments, previews, and
  campaign audience selection with validated versioned responses.
- **AC-12**: React provides progressive-disclosure field/import mapping, static
  lists, nested AND/OR building, type-aware operators, revision recovery,
  preview, and campaign audience selection with keyboard, focus,
  accessibility, and responsive coverage.
- **AC-13**: Existing campaign, policy, inbound, provider, sorting/import,
  CLI, web security, and browser behavior remains passing; no external
  provider, mailbox, credential, or recipient data is contacted or imported.
- **AC-14**: Synthetic benchmarks report representative 1,000- and 10,000-
  contact evaluation/preview timings, bounded query behavior, and relevant
  EXPLAIN QUERY PLAN observations; no unmeasured cache is introduced.

## Non-goals

- Hosted identity, multi-user authorization, tenant isolation, remote workers,
  or a second contact database.
- A document store, distributed query service, full event-sourced membership
  system, or frontend rule evaluator.
- Consent inference, permission grants, suppression replacement, or a second
  eligibility evaluator.
- Regex, arbitrary expressions, unrestricted SQL, eval(), provider audience
  synchronization, predictive scoring, behavioral events, or automation
  journeys.
- Live SMTP/IMAP or claims of provider, inbox, legal, or consent readiness.

## Options considered

### Option 1: normalized typed attributes and canonical service

Store field definitions and one typed value per contact/field in relational
tables, validate a versioned AST, compile through a field/operator registry,
and keep all interfaces on one application service.

**Pros**: relational integrity, explicit missingness, stable IDs, one evaluator,
auditable migrations, and parameter binding.

**Cons**: more schema/compiler work and joins requiring measured indexes.

### Option 2: JSON attributes and application filtering

Put attributes in contact metadata JSON and filter in Python after loading.

**Pros**: small initial migration.

**Cons**: weak typed/searchable authority, ambiguous missingness, poor bounds,
and divergent-evaluator risk.

### Option 3: per-field contact columns

Add a SQLite column for each configured field.

**Pros**: familiar single-column SQL.

**Cons**: operator configuration becomes schema mutation; field labels, types,
and migrations become coupled and unsafe.

### Option 4: separate audience service/database

Synchronize contacts and audiences with a new process or store.

**Pros**: independent scaling later.

**Cons**: contradicts the local modular monolith and makes snapshot consistency
and recovery harder.

## Decision

**Chosen option**: Option 1: normalized typed attributes and canonical service.

Add segmentation inside src/mailforge with one additive CampaignStore
migration, one SegmentationService, a restricted typed AST compiler, current
contact/list authorities, and campaign preparation metadata. Keep it local,
synthetic, parameterized, and UI-independent (basis: .aios/architecture.md,
Specs 0001 and 0004, and the current CampaignStore/EligibilityService).

**Implementation skills**: data-integrity (Mailforge/.agents/skills/data-integrity/) · security (Mailforge/.agents/skills/security/) · test (Mailforge/.agents/skills/test/) · develop (Mailforge/.agents/skills/develop/) · check (Mailforge/.agents/skills/check/) · sync (Mailforge/.agents/skills/sync/)

## Rationale

The existing database already owns contact identity, list membership, campaign
state, delivery intents, operational events, and policy evidence. Normalized
typed values extend that authority without encoding operator fields as SQLite
schema or treating metadata JSON as a query engine. A registry-controlled
compiler keeps the user language finite, and parameterized queries keep values
out of generated SQL.

Preparation is a durable boundary rather than a live join at dispatch.
Recording exact revision and evaluation facts makes review and recovery
explainable, while EligibilityService remains responsible for mutable sending
policy immediately before dispatch. No cache is justified before benchmark
evidence demonstrates a measured need.

## Feature design

### Data model

Migration 7 adds:

- contact_field_definitions: stable id, unique immutable machine_key, label,
  field_type (TEXT, NUMBER, BOOLEAN, DATE, DATETIME), bounded validation JSON,
  optional bounded controlled values, active/archived status, and metadata.
  Labels may change; machine keys do not. Populated fields cannot change type
  in place; a deliberate validated conversion/replacement is required.
- contact_attribute_values: (contact_id, field_id) primary key with foreign
  keys to contacts and definitions; value_type; exactly one typed value column
  (text, number, boolean, date, or datetime); source/provenance, actor, and
  timestamps. A missing row is missing; an empty text row is present. Service
  validation enforces equality with the definition type.
- contact_attribute_conflicts: contact/field, source/position, bounded
  existing/incoming summaries, disposition (preserved, overwritten,
  quarantined, rejected, duplicate_source_conflict), actor, reason, and
  timestamps. Do not retain secret-bearing or unbounded source content.
- list_membership_history: list/contact, add/remove, timestamp, source, actor,
  idempotency key, reason, and operation reference. Existing
  list_memberships remains the effective current authority.
- segments: stable identity, name/description, active/archived status, current
  revision, metadata, and creation/update provenance.
- segment_revisions: immutable revision identity, segment foreign key, revision
  number, AST version, canonical definition JSON, definition hash, author/change
  provenance, and timestamp. Revisions are unique per segment.
- campaign_audiences: one row per prepared campaign audience with campaign
  foreign key/uniqueness, source type/id (list or segment), exact revision and
  hash when dynamic, evaluation clock/time, matched/prepared/excluded counts,
  status (preparing, prepared, failed), and failure reason.

Built-in fields are registered descriptors (contact.email,
contact.display_name, contact.lifecycle_status, and list membership); custom
fields resolve by stable id. Archived fields remain readable by historical
revisions but are not selectable in new definitions. Delivery intents remain
the prepared recipient set; no redundant membership authority is created.

### Value sourcing

| Action | Value produced or displayed | Source |
|---|---|---|
| Field management | id, machine key, label, type, status, validation | request plus field-definition row |
| Structured import preview | source row/column, normalized contact, typed value, disposition | bounded upload, sorter normalization, field registry, existing attribute row |
| Membership view | effective membership and history | list_memberships and list_membership_history |
| Segment edit | current revision, new revision, canonical JSON, hash | expected revision request, AST validator, segment_revisions |
| Segment preview | count, page, evaluation time/clock, revision/hash, explanation | one database snapshot, frozen service clock, validated AST |
| Eligibility preview | policy decision and reason | existing EligibilityService and current suppression/policy rows |
| Campaign preparation | source identity, revision/hash, evaluation time, counts, recipient intents | campaign request, segment/list authority, evaluator, CampaignStore transaction |
| Campaign dispatch | current allowed/suppressed outcome | existing EligibilityService at reservation time |

### Database migration

Verify the actual version immediately before implementation; current evidence
reports 6, so the next migration is 7. The migration is additive and runs in
the existing immediate transaction. It must be fresh/upgrade/repeat safe,
preserve all old rows, enforce foreign keys and uniqueness, and support
integrity/backup/restore tests. It does not rewrite user data or down-migrate
automatically. A migration failure rolls back the transaction.

### Rule-expression grammar and evaluation

Definitions are canonical UTF-8 JSON with sorted keys and no insignificant
whitespace:

~~~json
{"version":1,"root":{"kind":"ALL","children":[
  {"kind":"PREDICATE","field_id":"field-id","operator":"EQUALS","value":"x"}
]}}
~~~

Nodes are ALL(children), ANY(children), NOT(child), and
PREDICATE(field_id, operator, value). ALL/ANY require at least one child; NOT
exactly one. Operators are EQUALS, NOT_EQUALS, CONTAINS, STARTS_WITH,
ENDS_WITH, IN, GREATER_THAN, LESS_THAN, GREATER_THAN_OR_EQUAL,
LESS_THAN_OR_EQUAL, IS_PRESENT, and IS_MISSING. The registry rejects
type-inappropriate operators; presence operators have no value. IN is a
non-empty typed array.

Limits are depth 5, predicates 64, IN values 100, comparison strings 512
characters, serialized definition 32 KiB, and preview pages 100. No regex.
Only the registry creates SQL structure; all user values are parameters, and
browser field names never become identifiers. Invalid definitions fail with
path/limit errors.

Each lookup is TRUE for a matching present value, FALSE for a non-matching
present value, and UNKNOWN for missing values. IS_PRESENT/IS_MISSING resolve
missingness explicitly. NOT UNKNOWN is UNKNOWN, not TRUE. AND/OR use standard
three-valued truth tables (false dominates AND, true dominates OR, otherwise
UNKNOWN); only final TRUE matches. Empty text is present. NUMBER is parsed and
validated with decimal arithmetic, normalized to a signed 64-bit integer at
scale 1,000,000, and accepts up to 12 integer digits and six fractional digits
subject to the signed range. It rejects non-finite values, overflow,
unsupported precision, invalid representations, and silent rounding. Numeric
filters compare integer storage, never lexicographic text or binary float
conversions. Canonical import/export uses a fixed-point decimal string derived
from the scaled integer. DATE is ISO calendar date; DATETIME is UTC ISO-8601;
BOOLEAN is true/false only.
Evaluation uses one read snapshot and a frozen service-supplied clock.

### Import reconciliation

Existing sorter/ingestion remains authoritative for address parsing,
normalization, validation, source order, and contact identity. Address-only TXT
and compatible imports retain current behavior. Structured CSV/JSON adds an
explicit column-to-field mapping and typed validation. Source rows are
processed in order: create new contacts/valid values; fill missing existing
values; preserve populated values by default; record conflicts; reject or
quarantine invalid attributes without corrupting valid contact state.
Conflicting duplicate rows record deterministic source order. Overwrite is an
explicit validated operation with old/new summaries, actor, source, and
reason. Imports never modify suppression, preferences, authorization, or
consent evidence.

### Static membership and revisions

SegmentationService calls existing list add/remove operations and records
history only for a new effective change. Uniqueness and idempotency keys make
repeated operations harmless; membership never establishes consent. Segment
edits use expected current revision compare-and-swap. A valid edit inserts a
new immutable revision and moves the current pointer atomically; a mismatch
returns 409 and does not overwrite newer work. Old revisions remain available.

### Segment evaluation and campaign snapshot

Preview returns segment/revision IDs, revision number, definition hash,
evaluation timestamp/clock, matching count, bounded contacts, validation
errors, and a practical explanation tree. Matching is separate from policy
eligibility; only EligibilityService decides current sending policy.

Campaign creation accepts exactly one list or segment source and may pin an
exact segment revision. Preparation resolves/evaluates it under a consistent
snapshot, records source identity/revision/hash/time/statistics, and commits
audience metadata plus delivery intents atomically. Repeated preparation with
the identical source and revision returns the existing result and never
appends duplicates. A different source or revision against an already prepared
campaign returns an explicit conflict unless the existing lifecycle contract
authorizes re-preparation; it never replaces recipients, changes the recorded
revision, reuses the wrong snapshot, or resets delivery outcomes. Changes after
preparation do not change recipients; current suppression and eligibility are
rechecked at dispatch.

Each recurring execution has its own durable audience snapshot. The
implementation must reuse an existing distinct recurring campaign/run identity
if the recurrence model provides one; otherwise it adds only the smallest
compatible run identity. Each occurrence independently evaluates its audience,
records its exact revision and recipients, leaves prior snapshots unchanged,
and rechecks current eligibility at dispatch. The audience uniqueness key is
therefore scoped to the execution identity, not only the logical campaign.

External work is never held inside a write transaction. A failed/interrupted
preparation rolls back or reconciles to failed before retry; no partial
audience becomes executable. Large sets may stream ids through bounded buffers
but final readiness is atomic.

### Service, interfaces, and browser

SegmentationService owns definitions, typed values, import reconciliation,
membership orchestration, AST validation/compiler, revisions, preview, and
audience resolution. CampaignService owns lifecycle and calls it. Store owns
SQL/migrations/transactions. EligibilityService remains the only mutable
policy evaluator.

Add versioned /api/v1 resources for fields, structured import preview/apply,
list membership/history, segments CRUD/revisions/preview, and campaign
audience/prepare. Add corresponding mailforge field, import, list, segment,
and campaign-audience commands. Responses are bounded and never expose SQL,
filesystem paths, secrets, or unbounded source rows. Conflict, complexity,
invalid-field/value, state, lock, and policy errors are explicit.

React adds progressive-disclosure field management, multi-column mapping,
static-list controls, nested AND/OR/NOT segment building, type-aware
operators, save/edit with conflict recovery, count/contact preview, and
campaign source/revision selection. Existing simple TXT import remains simple.
Controls must be labeled, keyboard/focus accessible, responsive at 390px, and
announce validation/status changes. The frontend never evaluates rules.

### Security, transactions, recovery, and performance

Keep loopback bind, Host/origin, session, CSRF, CSP, upload bounds, and existing
secret/artifact rules. Validate every value server-side; allowlist fields and
operators; parameterize all values; prohibit SQL/eval/regex/unbounded inputs.
Redact conflict evidence and never store credentials.

Use CampaignStore.transaction(immediate=True) for migration, imports,
membership changes, revision edits, and preparation. Previews use one
read-snapshot connection and frozen clock. Compare-and-swap protects revisions;
unique keys protect retries. SQLite busy errors receive bounded retryable
responses. No external operation runs under a write lock. Audience metadata
and intents commit together; a partial preparation is never executable.

Benchmark 1,000 and 10,000 synthetic contacts across simple, nested, missing,
membership, and mixed predicates; report count/preview latency, memory/lock
behavior, and EXPLAIN plans. Index only measured access paths (attribute
primary key/value lookups, memberships, revisions, and audience keys). Do not
add a cache before evidence.

## API and CLI contract

| Surface | Contract |
|---|---|
| Fields | List/create/update/archive typed definitions; reject unsafe type changes |
| Imports | Bounded preview with explicit mapping, then apply with dispositions |
| Lists | Idempotent add/remove/history using existing membership authority |
| Segments | CRUD, immutable revisions, expected-revision edit, bounded preview |
| Campaigns | list/segment source, exact revision, snapshot report, prepared audience |

The CLI and API call the same service and return the same semantic outcomes.

## Proposed stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Existing Python package | Canonical services and tests |
| Framework | Existing services, FastAPI, argparse, React/Vite | Current interfaces |
| DB | SQLite through CampaignStore, migration 7 | Existing durable authority |
| Auth | Loopback session/CSRF/Host/origin controls | Hosted identity is out of scope |
| Hosting | Local Uvicorn with compiled assets | Existing web boundary |
| Observability | Operational events, audit/history, reports, query plans | Durable local evidence |

## Build plan

Use the repository's implicit tracer-bullet approach: one end-to-end local
thread, then thicken it without a second authority.

1. Add migration 7 and constraints for fields, attributes, conflicts, history,
   segments, revisions, and audiences; satisfies **AC-1**, **AC-2**, **AC-4**,
   **AC-5**, **AC-8**.
2. Implement typed values, canonical AST validation/compiler, UNKNOWN
   semantics, limits, and preview; satisfies **AC-2**, **AC-6**, **AC-7**.
3. Implement structured mapping/reconciliation, conflict/quarantine, explicit
   overwrite, and membership history; satisfies **AC-3**, **AC-4**, **AC-10**.
4. Implement immutable segment CRUD, compare-and-swap, dynamic evaluation, and
   benchmark/query-plan evidence; satisfies **AC-5**, **AC-6**, **AC-7**, **AC-14**.
5. Integrate atomic audience snapshots, idempotent preparation, recurring
   reevaluation, and dispatch-time eligibility; satisfies **AC-8**, **AC-9**,
   **AC-10**.
6. Add CLI and API adapters and then browser workflows/tests; satisfies **AC-11**,
   **AC-12**, **AC-13**.
7. Run focused, migration, concurrency, security, performance, browser,
   existing regression, build, artifact, and independent-review checks;
   satisfies **AC-1** through **AC-14**.

## Consequences

**Positive**: one inspectable audience contract; safe imports; reproducible
campaign review; existing policy and identity authorities remain intact.

**Negative / tradeoffs**: more schema/compiler/test surface, SQLite join and
contention limits, and deliberate work for populated field type changes.

**Neutral**: simple address-only imports remain simple; audit/revision rows add
durable storage but preserve explainability.

## Migration plan

**Strategy**: additive, one-migration local rollout.

**Phases**:

1. Add and test migration 7 without rewriting existing rows.
2. Deploy service/interfaces while preserving list-only campaign compatibility.
3. Enable segment audiences after focused and full synthetic evidence.

**Rollback**: stop using segment audiences and restore a pre-migration backup
if needed. Do not automatically delete new segment data or down-migrate.

**Risks**: lock contention, invalid legacy assumptions, and UI/API drift from
the compiler; mitigate with immediate-transaction, parity, and migration tests.

## Critical test scenarios

- Fresh/version-6 migration, interrupted/repeat migration, FK, preservation,
  backup/restore: **AC-1**.
- Missing vs empty; all types; controlled values; archive/type-change; negative,
  zero, positive, fractional, boundary, invalid precision, and overflow NUMBER:
  **AC-2**.
- Every operator, nested ALL/ANY/NOT, UNKNOWN truth table, invalid field,
  injection, and complexity limits: **AC-6**.
- Duplicate/conflicting imports, quarantine, explicit overwrite, and policy
  non-overwrite: **AC-3**.
- Concurrent attributes/edits, immutable revisions, static history:
  **AC-3**, **AC-4**, **AC-5**.
- Dynamic changes affect fresh preview, not prepared recipients; duplicate and
  interrupted preparation; lock contention: **AC-7**, **AC-8**, **AC-9**,
  **AC-10**.
- Suppression before dispatch, recurring fresh evaluation, independent
  occurrence snapshots, and source/revision preparation conflicts: **AC-9**.
- CLI/API parity, browser builder/campaign selection, accessibility, keyboard,
  focus, and responsive 390px: **AC-11**, **AC-12**, **AC-13**.
- 1k/10k benchmarks, query plans, full Python/frontend/browser/security/build
  checks, and no network/provider activity: **AC-13**, **AC-14**.

## Acceptance matrix

| Area | Evidence | AC |
|---|---|---|
| Migration | Fresh/upgrade/retry/backup/FK tests | AC-1 |
| Fields | CRUD, constraints, missing/empty, archive tests | AC-2 |
| Import | Mapping, duplicate/conflict/quarantine/overwrite | AC-3 |
| Lists | Idempotency and history | AC-4 |
| Segments | Revision/concurrency/evaluation | AC-5–AC-7 |
| Campaigns | Exact snapshot, duplicate prep, recurring, suppression | AC-8–AC-10 |
| Interfaces | CLI/API parity and browser workflows | AC-11–AC-12 |
| Regression | Full suite, security, no external communication | AC-13 |
| Performance | 1k/10k timings and query plans | AC-14 |

## Follow-up

- [ ] After implementation, advance this spec from Proposed to Accepted only
  when the feature is shipped and verified; ratification alone does not claim
  implementation.
- [ ] After implementation, update .aios/architecture.md,
  .aios/repo_truth.md, capability atlas, API/CLI/browser documentation,
  performance evidence, and SYSTEM_CHANGELOG.md from actual evidence.
- [ ] Keep provider qualification, hosted identity, consent, and live delivery
  as separate future acceptance obligations.

## References

**Project sources**:

- .aios/architecture.md and .aios/repo_truth.md.
- docs/specs/0001-mailer-core.md, 0004-architecture-consolidation.md, and
  0005-local-web-application.md.
- src/mailforge/infrastructure/campaign_store.py,
  src/mailforge/application/campaigns.py, and
  src/mailforge/application/eligibility.py.
- .agents/skills/data-integrity/, security/, test/, check/, and sync/.

**Practices & standards**:

- Parameterized queries and allowlisted query compilation for untrusted values.
- Three-valued logic for nullable predicates and explicit missing semantics.
- Compare-and-swap optimistic concurrency for immutable revisions.
- Idempotency keys and uniqueness constraints for retry-safe operations.
- Atomic snapshot preparation and benchmark-before-cache performance practice.
