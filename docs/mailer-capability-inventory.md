# MAILER capability inventory

**Scope:** bounded source inventory for the legacy `MAILER/` area, prepared for
`MAILFORGE-MAILER-CORE-005` and `MAILFORGE-OUTBOUND-ENGINE-006` on
2026-10-27. `MAILER/.env` was not opened. The former `MAILER/main.py` source is
now retired; `MAILER/` is a data/resource compatibility boundary. This document
records observed responsibilities and their canonical owners; it does not
authorize a live send or real-data migration.

## Source capability inventory

| Source | Purpose | Input | Output | Side effects | State ownership | Core target / migration acceptance |
| --- | --- | --- | --- | --- | --- | --- |
| Former `MAILER/main.py` `ConfigLoader` / launch gate | Load YAML, resolve explicit environment mappings, and fail closed | `config.yaml`, caller-owned environment | Typed compatibility config and migration report | Reads only the requested config; never loads `.env` | Canonical `LegacyConfigTranslator` | **Migrated and retired**. Use `mailforge campaign translate-legacy`; secrets are redacted from reports. |
| Former `TargetLoader` | Read and normalize TXT/CSV targets | `all_mails.txt` or configured target file | Provenanced `ContactCandidate` records | Read-only file access | `load_legacy_targets` plus `CampaignService` | **Migrated** with deterministic identity, duplicate counts, invalid-record evidence, and source positions. |
| Former `CampaignConfig` / `CampaignManager` | Hold campaign settings and coordinate workers | YAML settings and target records | Durable campaign/intents/attempts | Legacy threads and counters | `CampaignService`, `OutboundService`, `CampaignStore` | **Migrated**; one canonical campaign coordinator, restart/recovery semantics, no duplicate worker engine. |
| Former `TemplateEngine` | Render subject/body, Jinja values, and spintax | HTML/text templates and target context | Bounded rendered content | Read-only template access | `SafeTemplateRenderer`, `MessageComposer` | **Migrated**; legacy `target` context is supplied through a bounded compatibility view. |
| Former `EmailBuilder` / `HeaderGenerator` | Construct personalized MIME | Rendered content, recipient, sender | Persisted MIME and content hash | No external side effect | `MessageComposer`, prepared-message store | **Migrated**; invisible anti-detection text injection is deliberately not carried forward. |
| Former `InvisibleTextInjector` / client simulation | Alter content or headers to evade detection/client heuristics | Rendered message and client flags | Modified message | Could change message semantics | None | **Not migrated by design**: unsafe, non-essential behavior with no trustworthy product contract. |
| Former `SMTPManager` | Connect, authenticate, probe, rotate, and submit through SMTP | Explicit caller-supplied SMTP config and MIME | SMTP response / exception | Network connection only after gate | `infrastructure.smtp.SmtpTransport` and `SMTPConnectionPool` | **Migrated as one canonical gated adapter**; no real provider qualification or live CLI is enabled. |
| Former `email_worker` | Apply rate/domain/reputation controls around one send | Queued target and campaign policy | Attempt classification | Sleeps, sends, updates files | `PolicyEngine`, durable reservations, `CampaignService` | **Migrated** to durable policy reservations and attempt state; no implicit retry after ambiguity. |
| Former `BounceHandler` / retry controls | Classify failures and update bounce state | SMTP result and historical lists | Delivery state and suppression | Durable state only | `migrate_legacy_state`, `CampaignStore` | **Migrated**: hard bounce → suppression/permanent failure; failed → deferred; sent history → ambiguous. |
| `MAILER/image_convert.py` | Convert local images for inline HTML use | Local image path | Inline HTML/data representation | Reads image files and encodes content | No campaign delivery state | Migrated to `src/mailforge/application/assets.py`; legacy helper source retired. |

## Legacy operational paths and metadata

| Path / artifact | Observed role | Migration treatment | Acceptance evidence |
| --- | --- | --- | --- |
| `all_mails.txt` | Default target source | Read-only compatibility input; no automatic import of real data | Synthetic import test records source provenance and deterministic contact identity. |
| Optional configured CSV target | Alternate target source | Read-only compatibility input | Same import contract as TXT, with source file and record position retained. |
| `sent.txt` | Historical successful-send exclusion list | Rehearsal maps entries to `ambiguous` legacy attempts; no blind retry | Migration rehearsal test and explicit no-ambiguous-retry rule. |
| `failed.txt` | Historical failure list | Rehearsal maps entries to `deferred`; retry requires explicit operator choice | Rehearsal test plus `retry_deferred=False` default. |
| `hard_bounces.txt` | Historical hard-bounce list | Rehearsal maps entries to `permanent_failure` and adds durable hard-bounce suppression | Rehearsal test verifies both state and suppression. |
| `events.json` | Legacy event history (filename is JSON-like; runtime appends event records) | Preserve as evidence during migration; do not treat as the new event authority | New operational event table is append-only for synthetic core actions. |
| `email_campaign.log` | Human/operator diagnostic log | Retain for compatibility and incident review; not a delivery ledger | No new secret-bearing values are written to durable core state. |
| `ledger.html` | Legacy visual/report artifact | Preserve; generate future reports from durable state without overwriting it | CLI `campaign report` returns structured counts. |
| `config.yaml` and referenced templates | Campaign, sender, SMTP, pacing, and template configuration | `LegacyConfigTranslator`, `prepare_legacy_campaign`, and canonical typed records | Report includes translated/deprecated/manual-review/validation dispositions; core stores non-secret references only. |
| `.env` | Local secret source | Never read for this inventory, never commit, never copy into SQLite or fixtures | Secret scan and working-tree review show it is not staged or printed. |

## Risks and explicit boundaries

- Legacy relative paths and append-only files are not transactionally coupled.
- Target validation does not establish consent, ownership, or suppression
  authority; the new core requires explicit policy records.
- A send can become ambiguous after SMTP DATA acceptance; `sent.txt` alone is
  not sufficient evidence to safely retry.
- Legacy worker throttles and counters are process-local; durable reservation
  is required for concurrent or restartable execution.
- Existing MAILER behavior is preserved as a compatibility surface. This task
  adds a synthetic, SQLite-backed campaign core and does not migrate real
  recipients, templates, credentials, logs, or delivery history.

## Outbound capability reconciliation — 006

The following matrix reconciles useful legacy capabilities against the modern
outbound preparation boundary. “Implemented” means covered by the modern
package and synthetic tests; it does not mean live provider readiness.

| Capability ID | Legacy capability | Modern implementation | Test / evidence | Migration status and remaining limitation |
| --- | --- | --- | --- | --- |
| MAILER-001 | TXT target loading | `CampaignService.import_candidates` plus ingest contracts | campaign-core and outbound preparation tests | Implemented for structured local candidates; legacy files remain compatibility inputs and are not auto-imported. |
| MAILER-002 | CSV target loading | shared ingest and import service | sorting parity tests; campaign import contract | Implemented at the package boundary; CSV-to-campaign operator handoff remains explicit. |
| MAILER-003 | YAML campaign settings | `LegacyConfigTranslator` plus typed `TemplateDefinition`, `SenderPolicy`, and `ScheduleSpec` records | translation, preparation, validation, and redaction tests | Implemented for local compatibility; unsupported policy sections are reported for review. |
| MAILER-004 | SMTP pool configuration | explicit caller-owned config, gated `SmtpTransport`, and `SMTPConnectionPool` | TLS, gate, reuse, health, policy, and cleanup tests | Implemented as a local adapter; provider qualification and live CLI remain disabled. |
| MAILER-005 | Sender rotation / selection | weighted pool rotation plus deterministic sender-policy capacity reservation | pool and concurrent-worker tests | Implemented locally; provider health-aware rotation is not claimed. |
| MAILER-006 | From / Reply-To headers | validated `EmailMessage` composition | MIME header tests | Implemented for prepared messages; sender-domain authorization remains operational work. |
| MAILER-007 | Plain-text body | `EmailMessage.set_content` from rendered template text | MIME and preview tests | Implemented. |
| MAILER-008 | HTML body and multipart alternative | standard-library multipart composition | MIME parse tests | Implemented; no inbox rendering or deliverability claim. |
| MAILER-009 | Jinja personalization | sandboxed Jinja with primitive-only context and strict undefined values | deterministic, escaping, and sandbox tests | Implemented with bounded filters; arbitrary legacy template compatibility is not promised. |
| MAILER-010 | Spintax / special values | seeded deterministic expansion for legacy-style choices/date/time/random integer | renderer regression tests | Implemented deterministically; legacy random output is not reproduced byte-for-byte. |
| MAILER-011 | Header and message identity generation | validated headers, per-intent prepared record, generated Message-ID, content hash | prepared-message persistence tests | Implemented for local preparation; Message-ID uniqueness is not a provider acceptance signal. |
| MAILER-012 | Attachments and inline images | confined `AssetResolver`, size/MIME checks, regular and CID-related parts | asset and MIME tests | Implemented for local assets; remote assets and image conversion adapters remain out of scope. |
| MAILER-013 | Unsubscribe headers | `List-Unsubscribe` and gated RFC 8058 header pair | one-click prerequisite tests | Header preparation is implemented; HTTPS POST endpoint, DKIM signing, and provider verification are not supplied. |
| MAILER-014 | Sending windows and weekdays | UTC-backed IANA timezone schedule engine | window, DST gap, fold, and due-time tests | Implemented for preparation/eligibility; no production scheduler process exists. |
| MAILER-015 | Pacing and cooldown | schedule pacing metadata and sender-policy cooldown state | policy/schedule unit coverage | Implemented as local policy state; actual sleep, provider quotas, and clock distribution are not implemented. |
| MAILER-016 | Per-minute/hour/day and per-domain limits | transactional policy reservations and durable minute/hour domain counters | policy capacity, concurrent-worker, and migration tests | Implemented locally; provider-reported quotas are not integrated. |
| MAILER-017 | Warmup and adaptive reputation | durable warmup ramp plus domain and sender failure-rate cooldowns in `PolicyEngine` | policy, migration, restart, and no-live-send checks | Migrated as bounded local controls; provider qualification and deliverability evidence remain external. |
| MAILER-018 | Retry classification | accepted, deferred, permanent, ambiguous, canceled attempt states | campaign-core and outbound tests | Implemented locally; ambiguous outcomes require explicit reconciliation and are never auto-retried. |
| MAILER-019 | SMTP phase evidence | pool phase marker, attempt state, and lease recovery model | recovery, pool, and ambiguity tests | Local adapter records before-data/accepted/unknown boundaries; provider-specific phase evidence remains open. |
| MAILER-020 | Worker concurrency | SQLite immediate transactions, leases, worker IDs, and reservations | two-worker test | Implemented for local synthetic workers; production process supervision is absent. |
| MAILER-021 | Sent / failed / bounce files | `migrate_legacy_state` plus durable intent, attempt, suppression, transition, and event tables | state, conflict, idempotency, and migration tests | Implemented for explicit snapshots; conflicts fail safe by preserving ambiguous/suppression precedence and source files remain untouched. |
| MAILER-022 | Progress and reporting | campaign reports, preview, CLI demo/report paths | CLI and JSON serialization tests | Implemented for local state; no dashboard or monitoring integration. |
| MAILER-023 | Crash recovery | expired lease recovery, started-attempt ambiguity, explicit reconciliation decisions | recovery tests | Implemented locally; real provider reconciliation and operator runbooks remain open. |
| MAILER-024 | Legacy entry point / live send | retired runner; canonical gated SMTP adapter and prepared-message execution API | source-retirement, two-gate, and static review | Source retired; real provider qualification, incoming mail, recipient delivery, and credential use remain disabled. |

This matrix is a capability reconciliation, not a claim of provider readiness.
The former runner source is retired, while MAILER data/resources remain in
place. No real recipient data, legacy campaign state, SMTP credential, provider
connection, incoming delivery signal, or live send was used for this retirement.

## Retirement disposition — 010

The complete local mapping and operator sequence are recorded in
`docs/mailer-retirement-010.md`. The canonical replacements are
`mailforge campaign translate-legacy`, `import-legacy`, and
`rehearse-legacy-state`. Any future live/provider work remains a separate
release decision requiring provider qualification and phase-aware reconciliation.

## Inbound operations reconciliation — 007

The modern inbound boundary is local-first and evidence-oriented. It does not
claim inbox access, sender authenticity, or production provider readiness.

| Capability | Modern implementation | Evidence | Limitation |
| --- | --- | --- | --- |
| `.eml` and MBOX acquisition | Read-only `EmlFileSource` and `MboxSource` | Source, cursor, dedupe, and replay tests | Local filesystem only |
| Synthetic mailbox acceptance | `SyntheticMailboxSource` plus `inbound synthetic-demo` | Deterministic DSN/reply/OoO/complaint/unsubscribe fixture | Synthetic trust is deliberately special-cased |
| IMAP boundary | `SimulatedImapSource`, fake endpoint, disabled real adapter | UID/UIDVALIDITY, cursor invalidation, disappearance, and read-only tests | No external IMAP connection, authentication, or ACK |
| RFC 5322/MIME safety | Bounded `SafeInboundParser` with standard-library policy parser | Size, preview, malformed, encoded, HTML, attachment, DSN, and ARF coverage | Corpus/limits need provider qualification |
| Delivery status | Per-message and per-recipient DSN fields/actions | Failed, delayed, and multiple-recipient tests | Does not rewrite accepted SMTP attempts |
| Feedback/unsubscribe | Signal → correlation → trust → policy → review/action | Trusted synthetic exact action and untrusted review tests | No automatic replies; RFC 8058 endpoint is outbound-only and separate |
| Durable operations | Schema 5 source/run/cursor/message/evidence/action/review, recipient-policy, and lifecycle tables plus transactional campaign pacing | Migration, replay, backup compatibility, restart, policy, and pacing tests | SQLite is local authority only; retention policy is not operationally approved |

No real mailbox, recipient migration, SMTP delivery, network call, or
credential was used for 007. `MAILER/.env` was not read or staged.
