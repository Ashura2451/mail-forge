# System changelog

## 2026-10-29 — Mailbox operations 030

- Extended the existing inbound/provider authorities with migration 23 folder
  projections, persisted UID/UIDVALIDITY and remote flags, conversation
  grouping, reply-all provenance, and sync/action reporting.
- Added bounded loopback-only IMAP folder discovery, incremental synchronization,
  UID-scoped flag/copy/move/archive actions, append-only action transitions, and
  explicit refusal of permanent deletion and EXPUNGE. Read-only and mutation
  disabled remain the account defaults; no real mailbox or live reply was used.
- Added browser, API, and CLI mailbox controls plus focused migration/thread/
  action tests. Validation passed with 250 Python tests, 6 browser/accessibility
  tests, frontend build, Ruff, arm64 package build, bundled runtime smoke, and
  bounded synthetic performance evidence.

## 2026-10-29 — Unified workspace final closure evidence

- Completed the 029 browser acceptance slice across the current workbenches,
  including mobile overlay navigation with Escape/focus restoration, truthful
  schema/integrity reporting, Integration Hub local client/event/webhook
  controls, Data Connections → Data Studio handoff, Email Standards actions,
  and staged Maintenance restore.
- Captured 26 current rendered-screen views plus console/request results: the
  original 22-screen set and four post-operation working-state captures for
  Integration Hub, Data Connections, Email Standards, and Maintenance. The
  baseline capture reported no console errors or failed requests; the
  targeted captures completed their browser assertions. The local browser
  workflow completed scoped-client authorization and revocation,
  event duplicate handling, signed loopback delivery, source preview and
  snapshot, sender diagnostics, unsubscribe readiness, and backup restore.
- Exercised the arm64 package across launch, browser operations, stop, restart
  against the same data directory, and durable-state verification. The bundle
  served the corrected frontend in a disposable packaged smoke. The final
  independent r3 review approved the correction pass with no remaining major
  or minor findings. No external provider, public callback, mailbox,
  recipient, live send, or signing/notarization is claimed; PR #17 remains
  open and unmerged pending integration.
- Correction pass for the initial independent review fixed mobile drawer
  scrolling and closed-drawer focus, removed unsafe mutation replay from
  Retry, protected dirty Maintenance edits while hydrating saved preferences,
  preserved Marketing purpose in the mobile campaign flow, and restored the
  narrow split-card layout. The final independent re-review approved these
  corrections.
- Final local validation: 248 non-browser Python tests and 6 browser/
  accessibility tests passed; frontend build, Ruff check, secret scan, and
  diff hygiene passed. Ruff format check retains the documented baseline drift.

## 2026-10-28 — Unified operator workspace

- Added a proposed unified-workspace specification and grouped React shell
  navigation for Data, Audiences, Communication, Incoming, Automation,
  Connectivity, Insights, and Application work.
- Added stable browser paths with refresh and Back/Forward recovery, responsive
  labelled navigation, related-work handoffs, page context, and retryable
  shell/action error feedback without changing backend authorities.
- Follow-up visual qualification added the shared panel, form, table, status,
  empty-state, and responsive layout foundation that the shell requires. The
  representative dashboard, Campaign Operations, mobile navigation, and
  Integration Hub screenshots now show the intended operator-console hierarchy
  and usable narrow-screen controls.
- Regenerated the packaged frontend assets and extended browser acceptance for
  direct routes, history navigation, and the narrow-screen navigation menu.
- Local evidence: 248 non-browser Python tests and 5 browser/accessibility
  tests passed; the arm64 package built with a clean pre-launch manifest check
  and its packaged launcher printed help. Signing/notarization, independent
  review, and real-provider qualification are not claimed.

## 2026-10-28 — Integration Hub

- Added schema 22 scoped integration clients, client audits, typed incoming
  event observations/conflicts, webhook subscriptions, and durable delivery
  attempts with visible retry, suspension, failure, and ambiguity states.
- Added fixed-origin, GET-only HTTP/REST source acquisition to Data Connections
  for bounded JSON, JSONL, and CSV reads with pagination limits, DNS/private
  address checks, credential references, provenance, and immutable snapshots.
- Added shared browser/API/CLI Integration Hub surfaces, Reporting inventory,
  Automation event enrollment, HMAC-SHA256 webhook signatures, and local
  diagnostics. No hosted callback, arbitrary proxy, unrestricted workflow, or
  live email path was added.
- Local synthetic tests cover migration, HTTP acquisition, token lifecycle,
  event idempotency/conflicts, bearer boundaries, webhook retries/signatures,
  and recovery semantics. No external credentials, provider, mailbox,
  recipient, or public endpoint was accessed.

## 2026-10-28 — Email Standards and sender diagnostics

- Added schema 21 sender identities, durable diagnostic runs, unsubscribe
  endpoints/tokens, and deduplicated DMARC aggregate report metadata/rows.
- Added the canonical `EmailStandardsService` for bounded SPF/DKIM/DMARC/DNS,
  sender alignment, MIME/content validation, evidence classification, and
  dated Google/Yahoo guidance without provider-acceptance claims.
- Added cookie-free, POST-only RFC 8058 local processing with hashed,
  recipient/list-scoped opaque tokens and idempotent suppression through the
  existing policy authority. Marketing campaign preparation now derives these
  URLs and emits `List-Unsubscribe`; one-click headers remain blocked at the
  campaign boundary until a real DKIM signing integration exists.
- Added shared browser/API/CLI diagnostics, campaign preflight findings,
  reporting, bounded DMARC import, and synthetic fixture acceptance. No real
  provider, mailbox, credential, recipient, DNS, or live send was used.

## 2026-10-28 — Read-only data connections and immutable snapshots

- Added schema 18 durable connection profiles, append-only connection events,
  revisioned snapshots, checksums, cancellation, restart recovery, and private
  JSONL artifacts for SQLite, PostgreSQL, and XLSX sources.
- Added bounded read-only adapters with safe selections, parameterized filters,
  PostgreSQL symbolic credential references, and formula-safe XLSX handling.
- Added Data Studio handoff plus shared browser/API/CLI surfaces. Existing
  Contacts, Message Studio, Campaign, and Automation authorities remain the
  downstream owners of their respective workflows.
- Local synthetic validation covers SQLite/XLSX acquisition, API
  upload/preview/snapshot, idempotency, cancellation, checksum rejection, and
  Data Studio handoff. A disposable local PostgreSQL 16 cluster passed
  `verify-full` schema, preview, and snapshot qualification; no external
  provider, credential, mailbox, or recipient data was accessed.

## 2026-10-28 — Campaign operations workspace

- Added `CampaignOperationsService` as the shared browser/API/CLI orchestration
  boundary for campaign drafts, audience and message preparation, sender
  account preflight, schedules, synthetic controls, drilldowns, and bounded
  exports.
- Added optimistic draft editing, explicit schedule audit events, IANA/DST
  validation, provider readiness findings, durable audience preparation, and
  formula-safe recipient CSV export without creating a second campaign engine.
- Added the responsive Campaign Operations browser workspace and CLI parity
  commands. The execution boundary remains local synthetic simulation only;
  no live provider or unrestricted delivery path was added.
- Follow-up review fixes add due-schedule supervision, purpose propagation,
  ordinary-list redaction, source-safe draft updates, bounded drilldown SQL,
  message preview evidence, and export metadata/outcome correctness.
- Final local validation pass: 225 Python tests, 5 browser/accessibility tests,
  TypeScript/Vite build, and Oxlint (with the existing five React effect
  warnings). The build used the available Node 24 executable.

## 2026-10-28 — Operational intelligence and reporting

- Added schema-free `ReportingService` metric definitions and bounded,
  timezone-aware summary/comparison reports over the canonical SQLite records.
- Added campaign, provider, inbound, workflow, List Tools/Data Studio, and
  Message Studio reporting with stable drilldowns, evidence boundaries, and
  deterministic next-action insights.
- Added shared API and CLI report surfaces plus the browser Reports & Insights
  console with range controls, evidence notes, detail tabs, and CSV export.
- Added bounded JSON/CSV/HTML exports with spreadsheet formula protection and
  no raw message bodies, credentials, or unrestricted paths in aggregate output.
- Validation is local and synthetic; no real provider, mailbox, credential, or
  recipient data was accessed, and SMTP acceptance is not inbox delivery.

## 2026-10-28 — Inbound operations workbench

- Added schema 12 durable inbound attachment metadata, extracted values,
  versioned rules/runs, Message Studio draft provenance, and explicit mailbox
  action requests.
- Added the canonical inbound workbench facade for bounded search/filter/detail,
  safe header/address extraction with JSON/CSV export, deterministic rule
  validation/evaluation, explicit contact/list addition, and reply/forward
  preparation through Message Studio.
- Added a read-only POP3S UIDL/RETR adapter and preserved EML, MBOX, synthetic,
  and IMAP cursor/recovery behavior. Acquisition never marks, moves, deletes,
  or sends remote mail.
- Expanded the browser/API/CLI Inbound surface with message inspection, rules,
  extraction, and draft controls while preserving review-gated suppression and
  workflow event authority.
- Validation is local and synthetic. 193 Python tests, API/browser checks, and
  frontend build pass; no live provider, mailbox, credential, or recipient
  data was accessed, and independent review was unavailable.

## 2026-10-28 — Provider Operations

- Added schema 11 provider accounts, separate inbound settings, expiring
  operation grants, OAuth-flow metadata, and durable controlled-test evidence.
- Added the canonical `ProviderOperationsService` with generic SMTP/IMAP,
  Google Workspace/Gmail, and Microsoft 365/Outlook capability presets;
  authorization-code/PKCE state handling; OAuth refresh seams; XOAUTH2 SMTP
  and IMAP authentication; account lifecycle; diagnostics; and inbound setup.
- Added one-shot, idempotent controlled test-send semantics with explicit
  sender/recipient authorization and phase-aware SMTP evidence. Simulation and
  live campaign paths remain separate; ambiguous post-DATA outcomes are not
  replayed automatically.
- Added provider account API/CLI operations and a browser account workspace.
- Validation is local and synthetic/fixture-based. No real provider,
  credential, mailbox, or recipient data was accessed; real-provider
  qualification was not performed.

## 2026-10-28 — Message Studio

- Added schema 10 durable Message Studio documents, immutable revisions,
  bounded local assets, recipient-specific attachment mappings, and immutable
  campaign content snapshots.
- Added the canonical `MessageStudioService` for structured blocks, safe merge
  fields, conditional segments, deterministic HTML/plain-text rendering, CID
  images, attachment manifests, and recipient previews.
- Added secured `/api/v1/message-studio/*` operations, `mailforge message`
  list/validate/render commands, campaign revision pinning, and workflow
  preparation reuse of the canonical renderer.
- Added a responsive structured browser studio with keyboard block reordering,
  merge-field insertion, local uploads, revision controls, isolated preview,
  and campaign revision selection.
- Validation remains local and synthetic. No provider, mailbox, credential,
  or real-recipient data was accessed, and arbitrary HTML/script execution is
  not supported.

## 2026-10-28 — Operational workflows and List Tools

- Added SQLite schema 9 for durable operational-event cursors, schedule
  occurrences, List Tools jobs, checkpoints, and private artifact manifests.
- Wired contact imports, list membership changes, trusted synthetic inbound
  reconciliation, and List Tools completion into active automation triggers
  with durable idempotency and bounded in-process restart reconciliation.
- Added UTC/relative/IANA local scheduled triggers with bounded recurrence and
  DST-safe local-time conversion.
- Added an accessible standalone List Tools browser route for TXT, CSV, JSON,
  and JSONL provider organization. It delegates to the canonical streaming
  sorter, emits provider evidence and downloadable TXT/CSV/master/summary/error
  artifacts, and never silently creates Contacts or lists.
- Validation remains local and synthetic; no provider, mailbox, credential, or
  real-recipient data was accessed.

## 2026-10-28 — Visual workflow automation

- Added SQLite schema 8 for durable visual workflow definitions, immutable
  revisions, source-event deduplication, enrollments, pinned runs, waits,
  action correlations, step history, and audit evidence.
- Added the canonical `AutomationService`, versioned API, `mailforge automation`
  CLI, bounded synthetic worker, revision lifecycle, graph validation, UTC wait
  handling, and allowlisted list/campaign-preparation/note actions.
- Added an accessible Automation console with a structured visual canvas,
  explicit keyboard edge editor, revision controls, synthetic enrollment, and
  durable run history.
- This is local synthetic evidence only: no provider, mailbox, credential, or
  real-recipient data was accessed, and no live delivery is enabled.

## 2026-10-28 — Advanced audience segmentation

- Added CampaignStore schema 7 for typed contact attributes, conflict/audit
  records, static membership history, immutable segment revisions, and durable
  per-run campaign audience snapshots.
- Added fixed-point NUMBER parsing using signed 64-bit scaled integers at
  scale 1,000,000, with finite/range/precision checks and no floating-point
  storage or comparison.
- Added the canonical segmentation service, bounded parameterized AST compiler,
  structured import preview/reconciliation, revision CAS, eligibility
  rechecks, and explicit source/revision conflicts for prepared audiences.
- Added `mailforge field`, `mailforge segment`, and dynamic segment campaign
  controls across the FastAPI API and React local console, including recurring
  snapshot run keys and accessible responsive controls.
- Local synthetic validation covers 1,000 and 10,000-contact previews; no
  provider, mailbox, credential, or real-recipient data was accessed.

## 2026-10-28 — Local web application and operator console

- Added `mailforge serve` with loopback-only FastAPI/Uvicorn hosting, versioned
  `/api/v1` contracts, Host/origin checks, in-memory local session cookies, and
  CSRF protection for state changes.
- Added a React/TypeScript/Vite console for dashboard, contacts/import preview,
  campaign preparation and synthetic execution, inbound review, providers, and
  activity. Compiled assets are served from the Python package.
- Added a bounded supervised in-process worker around canonical outbound
  services. Browser closure does not own or duplicate durable campaign work;
  no real SMTP/IMAP or external recipient was contacted.
- Added bounded browser EML acquisition through the canonical inbound parser,
  visible classification/correlation/trust metadata, and review-gated
  suppression actions.
- Added valid restart reconciliation, a database process lock, a connected
  loopback SMTP/IMAP lifecycle acceptance, browser-closure/fresh-session
  coverage, responsive checks, and maintained axe accessibility checks.

## 2026-10-28 — Provider interoperability and local protocol qualification

- Added schema 6 secret-free SMTP/IMAP provider profiles, symbolic credential
  references, structured provider diagnostics, and canonical provider CLI
  workflows.
- Extended the existing gated SMTP boundary for implicit TLS, mandatory
  STARTTLS, runtime authentication, capability checks, explicit transaction
  phases, response codes, and ambiguity-safe durable outcomes.
- Added a read-only TLS IMAP UID adapter with capability negotiation,
  UIDVALIDITY-aware cursors, bounded `BODY.PEEK[]` acquisition, size limits,
  source provenance, and reuse of canonical inbound persistence and policy
  processing.
- Added actual loopback SMTP/IMAP TLS protocol fixtures and synthetic
  credential tests. No external provider, mailbox, credential, or real
  recipient was contacted.
- Provider-specific OAuth2, provider quota/health claims, inbox placement,
  and controlled real-provider qualification remain pending.

## 2026-10-27 — Unified recipient policy authority

- Added one reusable eligibility evaluator for campaign preview, preparation,
  scheduling, synthetic dispatch, resume, retry, and final dispatch rechecks.
- Added SQLite schema 5 records for purpose-scoped authorization evidence,
  recipient preferences, operator declarations, suppression history, contact
  deactivation, exports/corrections, and deletion planning with retention
  conflict reporting.
- Preserved address-only list workflows: imports do not invent consent, and
  unknown evidence remains visible without blocking validation, deduplication,
  preview, preparation, or authorized synthetic simulation.
- Extended the canonical CLI for eligibility, suppressions, evidence,
  preferences, declarations, contact export, deactivation, and deletion plans.
- No real provider, mailbox, credential, or recipient data was accessed; this
  milestone does not claim legal compliance or live-provider readiness.

## 2026-10-27 — MAILER executable retirement and canonical compatibility

- Retired `MAILER/main.py`; preserved MAILER YAML, templates, lists, logs,
  event evidence, and `.env.example` without reading `.env` or migrating real
  data.
- Added bounded config/target translation, report-only and explicit import CLI
  commands, idempotent historical state rehearsal, provenance, suppression,
  and rollback-safe local authority behavior.
- Added the gated, TLS-verifying SMTP connection pool and prepared-message
  adapter under the canonical package. It does not enable live delivery or
  duplicate campaign orchestration.
- Deliberately did not migrate invisible-text/client-simulation behavior due to
  its unsafe, non-essential semantics; provider qualification remains open.

## 2026-10-27 — Legacy sorter source retirement

- Retired the verified SORTER/MSORTER Python wrappers after parity evidence;
  preserved their operational data, configuration, checkpoints, logs, and
  report paths without reading or migrating real data.
- Standardized canonical sorting ownership under `mailforge` and moved the
  unused image conversion helper to `application.assets`.
- Added a fail-closed, explicitly gated stdlib SMTP adapter with synthetic
  lifecycle coverage; MAILER remains deferred because its worker, YAML, state,
  warmup, reputation, and migration contracts are now represented by bounded
  local policy controls; provider qualification and deliverability evidence
  remain external release requirements.

## 2026-10-27 — Architecture consolidation and capability baseline

- Consolidated repository ownership around the packaged modular monolith while
  retaining `MAILER/`, `MSORTER/`, and `SORTER/` as compatibility boundaries.
- Added the architecture audit, accepted consolidation spec, official product
  landscape research, and a 15-domain capability atlas with gaps and a
  dependency-aware roadmap.
- Added import-direction and cycle regression tests and removed six declared
  dependencies with no repository imports; regenerated `uv.lock`.
- Preserved legacy code, operational files, generated state, local secrets,
  and all real-recipient/provider operations.

## 2026-10-27 — Durable MAILER campaign core

- Added a local SQLite campaign core for synthetic contacts, suppression,
  campaign lifecycle, delivery intents, attempts, recovery, and reports.
- Added `mailforge campaign` preview, simulation, and reporting commands plus a
  testable legacy reconciliation helper.
- Added the bounded legacy MAILER capability inventory and additive migration
  contract.
- Kept legacy MAILER files and real SMTP outside the migration boundary.

## 2026-10-27 — Policy-governed outbound preparation

- Added deterministic sandboxed template rendering, MIME composition,
  attachment confinement, prepared-message hashes, schedules, sender policy
  reservations, worker leases, and explicit reconciliation state.
- Added the synthetic `campaign outbound-demo` path and regression coverage for
  personalization, MIME, DST scheduling, suppression, policy limits, and
  concurrent workers.
- Kept real SMTP, provider health, DKIM/HTTPS unsubscribe infrastructure,
  incoming mail, and legacy-state migration outside the implementation boundary.
## 2026-10-27 — Durable inbound email operations

- Added schema 3 inbound sources, runs, cursors, immutable source identities,
  content hashes, optional raw retention, parse/classification/correlation
  evidence, DSN/ARF detail, actions, reviews, and replay.
- Added bounded non-executing RFC 5322/MIME parsing and read-only EML, MBOX,
  synthetic, and simulated IMAP acquisition. Real IMAP remains disabled.
- Added trust-aware classification and policy: only trusted synthetic exact
  evidence can automatically apply hard-bounce, complaint, or unsubscribe
  suppression; untrusted or ambiguous evidence is review-only; no automatic
  replies are sent.
- Added `mailforge inbound` inspection, review, decision, replay, statistics,
  import, and synthetic-demo commands.
- Validation is synthetic/local only; no real mailbox, SMTP, network, or
  recipient migration was performed.
## 2026-10-28 — Data Studio 021 foundation

- Integrated inbound operations 020 into `origin/main` at `eac22ba` before
  branching `feat/mailforge-data-studio-021`.
- Added the canonical `DataStudioService`, schema 13 recipe/revision/job/
  artifact tables, bounded structured-source inspection, typed declarative
  transforms, deterministic previews, provider-catalog classification,
  aggregation, pinned jobs, and atomic JSONL/error artifacts.
- Added shared versioned API, CLI, React Data Studio workbench, focused tests,
  and browser acceptance. Removed the unnecessary tracked `.github` workflow;
  portable local validation remains available.
- This is a partial local slice. Joins, downstream integrations, multi-format
  exports, trusted extensions, large-workload measurements, and full connected
  acceptance remain open; no external provider or recipient data was used.

## 2026-10-28 — Data Studio 021 completion slice

- Added schema 14 consumer provenance, multi-source append/union/intersect/
  subtract and indexed INNER/LEFT joins with deterministic provenance and
  collision handling.
- Added atomic UTF-8 TXT/CSV/TSV/JSON/JSONL exports with safe filenames,
  deterministic fields, and spreadsheet formula-injection protection.
- Added explicit contact-import reconciliation, canonical Message Studio
  personalization previews, pinned Automation recipe actions, and a narrow
  disabled-by-default trusted local extension contract.
- Extended the browser workbench for multi-source selection, combine mode,
  export choice, downloads, contact destinations, and message previews.
- Real-provider qualification remains readiness-only; no external action or
  private recipient/provider data was used.

## 2026-10-28 — Local application distribution

- Added a macOS arm64 application bundle builder with the locked Python runtime,
  package, dependencies, and compiled browser assets.
- Added stable local data-directory management, lifecycle controls, health
  diagnostics, first-run setup, runtime maintenance, and complete backup/restore
  validation boundaries.
- Preserved loopback security, SQLite authority, supervised workers, and the
  explicit simulation-only external-operation boundary.

## 2026-10-29 — Mailbox operations closure hardening

- Hardened fixture-only mailbox actions with same-connection UIDVALIDITY
  checks, atomic idempotency claims, identity-aware postcondition verification,
  conservative disconnect/read-failure ambiguity, and fenced inbound-run
  recovery.
- Updated loopback protocol coverage for folder discovery, flags, COPY/MOVE,
  message identity, and durable mailbox projection updates. No real provider,
  mailbox, credential, or recipient data was accessed.
