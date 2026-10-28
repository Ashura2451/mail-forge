# System changelog

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
