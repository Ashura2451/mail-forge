# 0015. Data Connections and External Source Snapshots

**Date**: 2026-10-28
**Status**: Accepted
**Task**: `MAILFORGE-DATA-CONNECTIONS-025`

## Summary

Mailforge will provide a local Data Connections workbench for read-only
SQLite, PostgreSQL, and XLSX sources. Connections are durable profiles, but
consequential work uses immutable Mailforge-owned snapshots, so later source
changes cannot rewrite Data Studio jobs, contacts, or prepared campaigns.

## Context

Data Studio already processes bounded local CSV, TSV, TXT, JSON, and JSONL
streams, publishes durable artifacts, and records downstream consumers. The
missing boundary is safe acquisition from explicitly selected external data
sources and workbooks. Adding acquisition without a durable snapshot contract
would let mutable remote rows enter campaign and message workflows without
provenance or recovery.

The existing CampaignStore remains Mailforge's only operational database.
Connection adapters are read-only and bounded; credentials remain symbolic
references resolved only at execution time. Data Studio remains the
transformation authority, SegmentationService remains the contact authority,
MessageStudioService remains the personalization authority, and AutomationService
remains the workflow authority.

## Requirements

**User stories**:

- As an operator, I want to configure a named read-only source so I can reuse
  it without re-entering connection details.
- As an operator, I want to inspect schemas and bounded previews before import
  so I can select the correct data safely.
- As an operator, I want each refresh to create a durable local snapshot so
  downstream work remains reproducible.
- As an operator, I want to hand a completed snapshot to Data Studio and the
  existing Contacts, Message Studio, Campaigns, and Automation workflows.

**Acceptance criteria** (the contract):

- **AC-1**: A canonical `DataConnectionsService` owns profiles, discovery,
  previews, snapshots, provenance, and operational outcomes.
- **AC-2**: External SQLite sources open in read-only mode, expose tables/views
  and typed columns, support bounded previews and snapshots, and never mutate
  the source.
- **AC-3**: PostgreSQL sources use a maintained Psycopg 3 driver, symbolic
  credentials, TLS verification, read-only transactions, bounded cursors,
  validated identifiers, and parameterized filter values.
- **AC-4**: XLSX sources use read-only openpyxl loading, worksheet/header
  discovery, duplicate-column handling, bounded previews, date/number mapping,
  and an explicit formula/cached-value policy without macro or link execution.
- **AC-5**: Profiles and snapshots persist in additive schema 18 tables without
  plaintext credentials; API responses never expose secrets or arbitrary local
  filesystem paths.
- **AC-6**: Schema discovery and previews are bounded, selection uses explicit
  tables/views/worksheets, fields, validated filters, and stable ordering where
  available; arbitrary SQL and mutation statements are unavailable.
- **AC-7**: A snapshot is published atomically only after successful extraction,
  validation, checksum, record count, and provenance publication. Failed,
  canceled, or interrupted extraction is never represented as completed.
- **AC-8**: Duplicate requests with the same idempotency key return the existing
  snapshot; refresh creates a distinct immutable revision; cancellation and
  restart recovery preserve truthful durable state.
- **AC-9**: Completed snapshots can be handed to existing Data Studio jobs;
  Data Studio transformations, exports, contact imports, Message Studio
  previews, campaign preparation, and workflow actions remain their existing
  authorities.
- **AC-10**: The browser workbench supports add/upload, validate, discover,
  preview, save, snapshot, refresh, status/error/history inspection, disable,
  and Data Studio handoff with accessible narrow-screen controls.
- **AC-11**: CLI and API operations delegate to the same service and enforce
  bounded inputs, local session/CSRF security, credential redaction, and safe
  source ownership.
- **AC-12**: Existing Mailforge capabilities and backup/restore remain intact;
  snapshot artifacts are included in application-owned artifact backup state.
- **AC-13**: Python, frontend, migration, security, fixture, packaged-runtime,
  and focused browser validation is run; connected PostgreSQL acceptance is
  reported separately if no disposable PostgreSQL service is available.

## Options considered

### Option 1: Reuse Data Studio with source paths only

Pass external paths directly into existing Data Studio jobs.

**Pros**:
- Smallest code change.
- Reuses existing processing implementation.

**Cons**:
- No durable connection identity or remote schema contract.
- Mutable external data could change a job's input and leak filesystem paths.

### Option 2: Focused acquisition service with immutable local snapshots

Add bounded adapters and a durable profile/snapshot boundary, then hand
completed JSONL snapshots to Data Studio.

**Pros**:
- Preserves existing authorities while making provenance and recovery explicit.
- Works consistently for databases and workbooks.

**Cons**:
- Adds one schema migration and adapter maintenance.
- Snapshot storage consumes local disk and refreshes are explicit.

### Option 3: General connector/plugin framework

Create a generic connector registry and arbitrary query/plugin model.

**Pros**:
- Potentially extensible to many source families.

**Cons**:
- Larger trust boundary and security surface.
- Encourages unbounded SQL, network, and plugin behavior outside this milestone.

## Decision

**Chosen option**: Option 2: Focused acquisition service with immutable local snapshots.

Implement read-only SQLite, PostgreSQL, and XLSX adapters behind one
`DataConnectionsService`. Store profiles and snapshot metadata in CampaignStore
schema 18; store completed snapshots as private, checksummed JSONL artifacts;
delegate transformation and downstream use to existing services.

## Rationale

The focused service is the smallest design that makes source data safe for
consequential local workflows. It preserves the local-first architecture and
existing Data Studio/Contacts/Campaigns authorities while isolating network,
filesystem, credential, and parsing risk at the acquisition boundary.

## Feature design

**Data model sketch**:

| Entity | Key fields | Relationships and constraints |
|---|---|---|
| `data_connections` | `id`, unique `name`, `source_type`, sanitized `config_json`, optional `credential_ref`, `selection_json`, `limits_json`, enabled/test fields, timestamps | One profile has many snapshots; no plaintext credential value; source type is `sqlite`, `postgresql`, or `xlsx`. |
| `data_connection_snapshots` | `id`, `connection_id`, revision, selection/schema/consistency JSON, status, artifact path/checksum, counts, idempotency key, timestamps | FK to profile; unique `(connection_id, revision)` and idempotency key; only completed rows expose a usable artifact. |
| `data_connection_events` | `id`, profile/snapshot IDs, event type/status, redacted detail JSON, timestamp | Append-only operational provenance for tests, discovery, extraction, cancellation, and failure. |

**State transitions**:

- Profile: `enabled ↔ disabled`; test status is an outcome, not authority.
- Snapshot: `pending → running → completed`; `pending/running → canceled`;
  `running → failed`; interrupted `running` becomes `failed` on service recovery.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/data-connections` | GET/POST | paginated list; name, type, config, credential ref | redacted profiles | local session | 400 invalid profile, 409 duplicate |
| `/api/v1/data-connections/sources` | POST | source type, profile name, bounded XLSX/SQLite upload | profile ID and safe metadata | local session + CSRF | 400 invalid/oversized source |
| `/api/v1/data-connections/{id}/test` | POST | none or bounded timeout override | redacted test result | local session + CSRF | 404, 409 disabled, 400 connection failure |
| `/api/v1/data-connections/{id}/schema` | GET | none | tables/views or worksheets and typed fields | local session | 404, 400 source unavailable |
| `/api/v1/data-connections/{id}/preview` | POST | selection, fields, filters, limit | bounded rows, schema, provenance | local session + CSRF | 400 selection/limit/query failure |
| `/api/v1/data-connections/{id}/snapshots` | GET/POST | selection, idempotency key | snapshot status/history | local session + CSRF for POST | 409 duplicate/state, 400 source failure |
| `/api/v1/data-connections/snapshots/{id}` | GET | snapshot ID | status, counts, checksum, provenance | local session | 404 |
| `/api/v1/data-connections/snapshots/{id}/cancel` | POST | none | canceled status | local session + CSRF | 404, 409 terminal state |
| `/api/v1/data-connections/snapshots/{id}/data-studio` | POST | recipe revision, correlation ID | existing Data Studio job | local session + CSRF | 404, 409 incomplete/duplicate |
| `mailforge data-connections ...` | CLI | profile/source/selection arguments | same redacted records as API | local operator | bounded validation errors |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Profile list | name/type/status | `data_connections` columns; path/password omitted by service serializer |
| Schema | tables/views/worksheets and fields | adapter metadata APIs or workbook header row |
| Preview | rows, types, source row identity | read-only adapter result and explicit selection |
| Snapshot status | counts, checksum, timestamps, errors | `data_connection_snapshots` and append-only events |
| Data Studio handoff | job ID and pinned source | existing `DataStudioService.create_job` from completed artifact |
| Contact/campaign/message use | transformed rows and frozen values | existing Data Studio artifact/consumer and Message Studio snapshot contracts |

**Key invariants**:

- Adapter operations are read-only and use validated identifiers; no raw user
  SQL reaches an external connection.
- Snapshot artifacts are private, atomic, checksummed, immutable after
  publication, and remain distinct across refresh revisions.
- A source snapshot is completed only when all selected rows within the explicit
  limit are written and metadata is committed.
- Credentials are symbolic references validated by the existing credential
  contract and resolved only in adapter execution.
- Cancellation and worker ownership prevent a partial artifact from being
  published or mistaken for a completed snapshot.

**Security model**:

The authenticated loopback operator session may manage local profiles and
snapshots. The browser may upload a bounded SQLite/XLSX file, but cannot submit
an arbitrary server filesystem path or host-scan/network destination. PostgreSQL
profiles require a symbolic credential reference, TLS verification by default,
connection/query timeouts, and read-only transaction setup. Profile responses,
logs, events, snapshots, backups, and errors redact credentials and source row
values unless the bounded preview explicitly requests rows. No multi-tenant or
public deployment authorization is introduced.

**Configuration required**:

- `env:<NAME>` credential references are supported for PostgreSQL passwords;
  values are supplied by the operator environment and never persisted.

**Critical test scenarios**:

- Happy path: read-only SQLite and XLSX discovery, preview, completed snapshot,
  Data Studio handoff, verifies **AC-2**, **AC-4**, **AC-7**, **AC-9**.
- Failure case: tampered/invalid source, duplicate request, cancellation, and
  interrupted worker leave no successful partial snapshot, verifies **AC-6**,
  **AC-8**, **AC-11**.
- Auth/permission: invalid credential/TLS or unsafe source/filter input is
  rejected without secret/path disclosure, verifies **AC-3**, **AC-5**, **AC-11**.

## Build plan

The build uses a tracer-bullet approach: establish one durable SQLite/XLSX
source-to-snapshot-to-Data-Studio path first, then add PostgreSQL, interfaces,
and downstream polish.

1. Add additive schema 18 tables, snapshot/artifact invariants, and migration
   tests, satisfies **AC-1**, **AC-5**, **AC-7**, **AC-8**.
2. Implement bounded SQLite and XLSX adapters with normalized records,
   discovery, previews, formula policy, and source immutability tests, satisfies
   **AC-2**, **AC-4**, **AC-6**.
3. Implement Psycopg 3 PostgreSQL read-only adapter, credential resolution,
   identifier/query validation, and controlled failure tests, satisfies
   **AC-3**, **AC-6**, **AC-11**.
4. Implement snapshot execution, atomic publication, recovery, cancellation,
   checksums, provenance, idempotency, and Data Studio handoff, satisfies
   **AC-7**, **AC-8**, **AC-9**, **AC-12**.
5. Add API and CLI parity with redaction and bounded request validation,
   satisfies **AC-5**, **AC-6**, **AC-10**, **AC-11**.
6. Add the Data Connections browser workbench and accessibility/responsive
   states, reusing the existing design system and Data Studio handoff, satisfies
   **AC-9**, **AC-10**, **AC-12**.
7. Validate source fixtures, package dependency compatibility, existing feature
   regressions, security cases, and documented connected-runtime limits,
   satisfies **AC-12**, **AC-13**.

## Consequences

**Positive**:

- External data is explicit, inspectable, reproducible, and safe to use in
  existing local workflows.
- Source adapters do not become new campaign, contact, or transformation
  authorities.
- Refreshes and failures are visible with durable provenance.

**Negative / tradeoffs**:

- Snapshots use local disk and require explicit refresh.
- PostgreSQL qualification depends on a controlled disposable server and
  compatible arm64 packaging dependencies.
- Schema 18 migration and new adapter tests increase maintenance surface.

**Neutral**:

- XLSX support is limited to normal `.xlsx`; `.xls`, `.xlsm`, macros, formulas,
  and external workbook links are not executed or broadly supported.

## Follow-up

- [ ] Run controlled PostgreSQL integration and representative source-size
  performance qualification when a disposable PostgreSQL service is available.
- [ ] Consider native keychain-backed credential resolution for database
  passwords; milestone 025 uses the existing symbolic reference boundary.
