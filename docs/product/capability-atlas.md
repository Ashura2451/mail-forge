# Mailforge product capability atlas

**Baseline date:** 2026-10-29
**Scope:** repository-backed product capability claims and the next dependency-aware roadmap.
**Canonical interpretation:** “implemented” means the named local code path
and evidence exist. It does not mean provider approval, inbox delivery,
production operations, or business authorization.

## Status vocabulary

- **IMPLEMENTED AND VERIFIED** — local behavior is covered by focused tests and
  a runnable path.
- **IMPLEMENTED BUT NOT FULLY VERIFIED** — code and tests exist, but provider,
  corpus, scale, or operational qualification is missing.
- **SIMULATED** — deliberately fake, parser-backed, or synthetic; never a live
  side effect.
- **PARTIAL** — a useful slice exists but important behavior is absent.
- **NOT IMPLEMENTED** — no supported implementation exists.
- **EXTERNAL INTEGRATION PENDING** — the local seam exists; an external system
  or credentialed adapter is not approved or connected.
- **DECISION REQUIRED** — product or operational ownership is undefined.
- **BLOCKED** — a prerequisite prevents safe implementation.

## Capability matrix

| ID / domain | Status | Current owner and evidence | Dependencies | Security / operations | Acceptance and next action |
| --- | --- | --- | --- | --- | --- |
| 01. Source ingestion and extraction | **IMPLEMENTED AND VERIFIED** | `application.ingest`, `domain.email`; TXT/CSV/JSON/JSONL and bounded JSONL tests | Local readable source; shared normalization | Size/depth limits; no network required | Ingest valid/invalid/duplicate records with provenance; extend corpus only with synthetic fixtures. |
| 02. Sorting, validation, dedupe, classification | **IMPLEMENTED AND VERIFIED** | `application.sorting`, `domain.sorting`; parity and enhancement tests | 01; provider/disposable catalogs; optional DNS | DNS is offline by default; outputs are deterministic and formula-safe | Run package and legacy adapter flows against synthetic sources; keep `docs/sorting-capability-matrix.md` current. |
| 03. Contact identity, lists, and provenance | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `infrastructure.campaign_store`, `application.campaigns`; synthetic campaign tests | SQLite schema; normalized address | Stable local identity is distinct from address; no real-data migration | Prove duplicate/source policy and rollback on a representative synthetic import; decide real ownership before migration. |
| 04. Consent, preferences, and suppression | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.eligibility`, `application.campaigns`, schema 5 evidence/preferences/declarations/lifecycle tables; policy-authority tests | 03; operator/business policy and jurisdiction remain external | Address-only imports remain `authorization_unknown`; mandatory suppressions win; untrusted inbound evidence is review-only | Qualify business policy, jurisdiction, retention, and real-provider trust before live sending. |
| 05. Campaign lifecycle and durable state | **IMPLEMENTED AND VERIFIED** | `application.campaigns`, `domain.campaign`, `CampaignStore`; lifecycle/recovery/report tests | 03–04; SQLite transactions | Leases, transitions, ambiguous outcomes, and append-only events are durable | Run synthetic demo, pause/resume, backup/restore, and recovery; define production worker ownership later. |
| 06. Templates, personalization, and content assets | **IMPLEMENTED AND VERIFIED** | `application.message_studio`, `templates`, `composition`; structured editor, deterministic preview, sandbox, strict undefined, CID MIME, attachment, and asset tests | Contact context; SQLite message revisions/assets; typed segment revisions | Sandbox and asset confinement; bounded expansion; no arbitrary code or imported HTML execution | External email-client qualification and approved legacy template import remain pending. |
| 07. Outbound preparation and policy | **IMPLEMENTED AND VERIFIED** | `application.outbound`, `policy`, `scheduling`; prepared MIME, schedule, reservation, and concurrency tests | 03–06; SQLite authority | Suppression recheck, sender capacity, hashes, leases, no ambiguity retry | Keep prepared bytes reviewable; qualify a provider adapter only after 08 decisions. |
| 08. SMTP/provider delivery | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.outbound`, `application.providers`, `application.provider_operations`, `infrastructure.smtp`; provider account/capability registry, XOAUTH2 adapters, runtime-only refresh wrapper, persisted server-issued operation grants, phase evidence, and loopback TLS tests | 04, 07; provider credentials; release controls | Tokens and refreshed material stay runtime-only; controlled test authorization is issued and checked server-side; external contact and real sending remain unauthorized; post-DATA ambiguity is retained | Provider-specific authentication, sandbox evidence, external credential authorization, and release gate remain pending. |
| 09. Inbound acquisition and parsing | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.inbound`, `inbound_parser`, `inbound_sources`; TLS IMAP UID adapter, read-only POP3S UIDL adapter, EML/MBOX/synthetic tests, loopback protocol tests | Local files or explicitly configured provider; schema 12 | Read-only bounded acquisition, UIDVALIDITY/UIDL cursors, raw retention choice, safe attachment metadata, and untrusted evidence separation | Qualify provider corpus, sender trust, mailbox acknowledgment, and real IMAP/POP3 authentication before external use. |
| 10. Bounce, complaint, unsubscribe, and reply handling | **IMPLEMENTED BUT NOT FULLY VERIFIED** | DSN/ARF/reply classification, correlation, review, shared suppression action, schema 12 workbench/rule/extraction/draft history, and policy-history tests | 04, 09; trust/authentication evidence | From/subject/MIME validity is not authentication; rule actions are bounded; automatic real actions are disabled | Qualify provider trust and verify per-recipient DSN and complaint cases against an approved corpus. |
| 11. Analytics, tracking, and reporting | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.reporting`, versioned metric registry, bounded SQLite aggregates, previous-period comparison, campaign/provider/inbound/workflow/job drilldowns, deterministic insights, API/CLI/browser Reports & Insights, and safe exports; focused synthetic tests | 05, 07; existing durable source records; larger workload measurement remains | No open/click beacon, inbox claim, recipient content in aggregates, or external analytics sink; accepted submission is not inbox delivery | Run connected browser acceptance and measured scale qualification; tracking and hosted analytics remain separate decisions. |
| 12. Scheduling, automation, and workflows | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.automation`, schema 8/10, versioned graph/revision API, Message Studio campaign references, and accessible Automation console; focused synthetic tests | 05, 07, 17; bounded in-process worker | UTC waits, immutable revisions, deduplication, step history, and no-live-network actions; local scale only | Run browser/restart/performance acceptance; hosted multi-user automation and real-provider actions remain out of scope. |
| 13. Interfaces and operator experience | **IMPLEMENTED AND VERIFIED** | `interfaces.cli`, `interfaces.web`, `web/`; inbound search/rules/extraction/drafts, segmentation API/CLI/browser controls, compiled-serving, and Playwright desktop/narrow acceptance | Application services; local filesystem; FastAPI/React build | Loopback Host/origin/session/CSRF boundary; no multi-user or tenant model | Maintain the local console; hosted access, identity, and remote execution require separate approval. |
| 14. Persistence, backup, restore, and migration | **IMPLEMENTED BUT NOT FULLY VERIFIED** | Versioned `CampaignStore` schema 23, typed attribute/segment/audience/message-studio/inbound-workbench/provider/automation/mailbox migrations, backup/restore, lifecycle planning, legacy rehearsal; SQLite tests | Schema versioning; local operator storage | Suppression/evidence/preference/provider/workflow/message and mailbox history survive backup/restore; actual deletion is not automatic | Define backup cadence, encryption, retention, RPO/RTO, and real-data migration rollback. |
| 15. Security, integrations, and operations | **IMPLEMENTED LOCALLY / NOT FULLY QUALIFIED** | Secret-safe credential boundary, fail-closed SMTP gate, TLS verification, `KeychainCredentialResolver`, `OperationalAssuranceService`, durable health/incidents/jobs observations, and redacted diagnostic export | All domains; release and provider choices | Local health and incident evidence is durable and bounded; Keychain resolution fails closed when unavailable; provider trust, real quotas, hosted monitoring, signing, and production SLOs remain unverified | Qualify signed/notarized Keychain continuity, measured workload limits, recovery objectives, provider observations, and an operator incident runbook. |
| 16. Local browser application | **IMPLEMENTED AND VERIFIED** | `interfaces.web`, `web/src`, `docs/architecture-decision-013-local-web-application.md`, `docs/web-operational-acceptance-014.md`; API lifecycle, inbound review, restart, closure, responsive, and axe checks | 03–10, FastAPI/Uvicorn, React/TypeScript/Vite | Synthetic worker only; compiled assets are packaged; browser closure does not own work; one local SQLite runtime owner | Hosted access, live provider qualification, multi-user identity, and remote execution remain separate decisions. |
| 17. Audience segmentation | **IMPLEMENTED AND VERIFIED** | `application.segmentation`, schema 7, `tests/test_audience_segmentation.py`, web API/CLI/browser controls, fixed-point and 1k/10k preview evidence | Contacts/lists, eligibility, SQLite, local React console | Typed values are registry-bound and parameterized; unknown remains non-match; recurring executions receive independent snapshots; external data is not accessed | Qualify larger real-shaped synthetic corpora and product policy before any real-recipient migration. |
| 18. Campaign operations workspace | **IMPLEMENTED LOCALLY / SYNTHETICALLY VERIFIED** | `application.campaign_operations`, shared browser/API/CLI surfaces, draft CAS, preflight, audience/message preparation, schedules, controls, drilldowns, and bounded exports; focused service/API tests | Existing campaign, segmentation, Message Studio, outbound, provider, reporting, and SQLite authorities | No second campaign engine; no live sending; provider account status, pacing, concurrency, and ambiguous outcomes remain explicit | Complete connected synthetic acceptance, independent review, performance measurement, and backup/restore reconciliation before stronger operational claims. |
| 19. Structured Data Studio | **IMPLEMENTED LOCALLY / CONNECTED SYNTHETICALLY** | `application.data_studio`, schema 17 recipes/revisions/jobs/artifacts/consumer provenance/recipient snapshots/worker leases, multi-source set operations and bounded joins, TXT/CSV/TSV/JSON/JSONL exports, contact import, Message Studio batch preparation, workflow action, provider catalog classification, and disabled-by-default trusted extension contract | Existing SQLite authority, provider catalog, local artifact storage | Bounded source reads, pinned revisions, atomic publication, source-row provenance, no arbitrary code or remote scripts | Controlled real-provider readiness and trusted external code extensions remain deferred; broad connected recovery/accessibility scenarios remain follow-up. |
| 20. External data connections and snapshots | **IMPLEMENTED LOCALLY / CONNECTED SYNTHETICALLY** | `application.data_connections`, schema 18 connection profiles/events/snapshots, bounded read-only SQLite/PostgreSQL/XLSX adapters, checksummed JSONL artifacts, Data Studio handoff, browser/API/CLI parity, and disposable PostgreSQL TLS qualification | Existing SQLite authority, symbolic credential resolver, Data Studio and downstream Contacts/Message Studio/Campaign/Automation authorities | No source writes, arbitrary SQL, plaintext credentials, uncontrolled paths, macros, or remote workers; preview/schema/row/artifact bounds are explicit | Continue representative source-size measurement and native keychain/secret-store qualification; no universal database or spreadsheet compatibility claim. |
| 21. Email standards and sender diagnostics | **IMPLEMENTED LOCALLY / CONNECTED SYNTHETICALLY** | `application.email_standards`, schema 21 identities/diagnostics/unsubscribe/DMARC records, bounded DNS/SPF/DKIM/DMARC and MIME checks, campaign preflight, browser/API/CLI/reporting, RFC 8058 fixture endpoint | Sender identity, Message Composer, Campaign Operations, Eligibility, Inbound trust boundary, Reporting, SQLite | DNS is opt-in; imported auth headers are untrusted; tokens are hashed and list-scoped; no provider acceptance, inbox, DKIM crypto, or live-send claim; campaign one-click is blocked until a signer exists | Continue provider-specific credentialed qualification and real deliverability work only through a separate release decision; local synthetic acceptance is complete. |
| 22. Integration Hub | **IMPLEMENTED LOCALLY / CONNECTED SYNTHETICALLY** | `application.integration_hub`, schema 22 clients/events/subscriptions/deliveries, fixed-origin HTTP Data Connections, browser/API/CLI/reporting, Automation enrollment, signed webhook transport, focused synthetic tests, and 029 browser/package evidence | Data Connections, Automation, Reporting, credential resolver, CampaignStore | GET-only bounded acquisition; no plaintext tokens; explicit scopes; external observations remain untrusted; HMAC at-least-once delivery; no hosted callback, arbitrary proxy, or live-send path | Local browser/package acceptance is recorded; independent review, external provider qualification, public callbacks, secret-store adapters, signing/notarization, and production scale remain separate decisions. |
| 23. Mailbox operations and conversations | **IMPLEMENTED LOCALLY / FIXTURE QUALIFIED** | `application.mailbox_operations`, controlled IMAP folder discovery/sync, UID/UIDVALIDITY identity, conversation grouping, Message Studio drafts, durable action reconciliation, browser/API/CLI surfaces, schema 23, and packaged loopback acceptance | Inbound, Provider Operations, Message Studio, Reporting, CampaignStore | Read-only by default; mutations are loopback-fixture-only, UID scoped, expiring-grant controlled, and fail closed; deletion and EXPUNGE are unavailable | Provider-specific capability, timeout/reconciliation, and authorized external mailbox qualification remain pending. |

## Ownership map

| Concern | Canonical modern owner | Compatibility or external boundary |
| --- | --- | --- |
| Address vocabulary | `src/mailforge/domain/email.py` | Legacy scripts call package helpers where migrated. |
| Ingestion and sorting | `src/mailforge/application/ingest.py`, `sorting.py`, `interfaces/cli.py` | Preserved SORTER/MSORTER data/config/checkpoint/report paths; source wrappers retired. |
| Campaign, policy, lifecycle, and inbound durable state | `src/mailforge/infrastructure/campaign_store.py` | Legacy MAILER files are not imported automatically. |
| Content composition | `application/message_studio.py`, `templates.py`, `composition.py` | Legacy `MAILER` templates/config remain compatibility inputs; Message Studio owns new structured documents and revisions. |
| Recipient eligibility and outbound policy/preparation | `application/eligibility.py`, `campaigns.py`, `policy.py`, `scheduling.py`, `outbound.py` | `infrastructure/smtp.py` is a transport boundary; `ProviderService` supplies configuration without becoming campaign authority. |
| Inbound evidence | `application/inbound*.py`, `application/providers.py` | `InboundService` owns acquisition/processing and `InboundOperations` owns workbench projections; IMAP/POP3 adapters acquire read-only evidence; sender authentication and provider trust remain external pending. |
| Workflow orchestration | `application.automation.AutomationService`, schema 8, `interfaces.web`, `interfaces.cli` | Visual definitions are local orchestration only; campaign, eligibility, segmentation, and inbound authorities remain canonical. |
| External source acquisition | `application.data_connections.DataConnectionsService`, schema 18, `interfaces.web`, `interfaces.cli` | SQLite/PostgreSQL/XLSX adapters are read-only acquisition boundaries; Data Studio owns transformation, SegmentationService owns Contacts, MessageStudioService owns content, CampaignService owns preparation, and AutomationService owns workflow execution. |
| Integration clients, event observations, and webhooks | `application.integration_hub.IntegrationHubService`, schema 22, `interfaces.web`, `interfaces.cli` | Data Connections owns external reads; Automation owns workflow enrollment; Reporting owns aggregates; credential resolution owns secret values; outgoing delivery is durable at-least-once transport observation. |
| Operator commands | `interfaces/cli.py` | Typer and script wrappers remain supported. |

## Competitor comparison

| Product family | Evidence-backed strength | Mailforge position |
| --- | --- | --- |
| Gammadyne Mailer | Broad desktop send/receive, merge, lists/databases, scheduling, throttling, tracking, bounce and automation controls | Local browser UX, preparation, durable synthetic state, inbound evidence, and fixture mailbox operations exist; real provider execution, tracking, and production automation do not. |
| Mautic | Campaign workflows, lead nurturing, landing/forms, web tracking, analytics, integrations, API | A local structured Automation console and reports exist; no hosted behavioral marketing platform, landing/forms, web tracking, or public API service. |
| ActiveCampaign | Multi-step automation, segmentation, CRM/integrations, A/B testing, reporting, webhooks/API | Local automation, segmentation, reporting, and signed webhook delivery exist; no CRM/ecommerce integration, A/B testing, external event tracking, or hosted dashboard/API. |
| Brevo | Segments, consent groups, campaign/transactional/automation channels | Consent and segmentation are open product decisions; current suppression is local and explicit, not a hosted marketing platform. |

## Gap register

| Gap | Blocking dependency | Risk if skipped | Exit evidence |
| --- | --- | --- | --- |
| Real recipient migration | Consent/identity/retention/rollback decision | Duplicate or unauthorized sends; unrecoverable state divergence | Rehearsal with synthetic production-shaped data and rollback proof. |
| Live SMTP/provider adapter | Provider contract and phase-aware reconciliation | Duplicate delivery or false success after ambiguous DATA outcome | Loopback contract tests now pass; provider sandbox evidence, external credential authorization, and release gate remain. |
| Real IMAP | Auth, trust verification, UID/cursor and retention policy | Treating forged evidence as policy authority | Loopback UID/UIDVALIDITY proof now exists; provider sandbox corpus, trust evidence, and acknowledgment policy remain. |
| Production scheduler/worker | Ownership, idempotency, supervision, recovery objectives | Lost or duplicated work | Failure-injected worker test and operational runbook. |
| Consent/preferences | Business owner and jurisdiction | Compliance and reputational harm | Policy test matrix with audit/revocation behavior. |
| Tracking/analytics | Privacy/retention and product decision | Unapproved personal data collection | Explicit opt-in design, event schema, retention test. |
| UI/API/service boundary | Operator workflow and auth/tenant decision | Premature surface with no safe authorization model | Approved spec plus end-to-end authenticated workflow. |
| Monitoring/deployment | Runtime target, SLOs, secret management | Silent failure and unsafe release | Health checks, structured metrics/logging, rollback rehearsal. |

## Dependency-aware roadmap

1. **Now — truth and guardrails:** keep the atlas, audit, boundary tests,
   locked dependencies, synthetic tests, and fail-closed side-effect gates
   green.
2. **Next — policy qualification:** the local authority is implemented; qualify
   business policy, jurisdiction, retention, provider trust, and approval
   evidence. This unlocks safer production-shaped migration rehearsal but not
   live sending by itself.
3. **Now — external qualification:** the provider-independent loopback SMTP
   and IMAP adapters are locally protocol-tested. Choose provider and mailbox
   contracts, then obtain sandbox evidence for authentication, rate limits,
   retry/reconciliation, trust, and read-only inbound behavior.
4. **Then — operations:** choose CLI worker, desktop, or service topology;
   add supervision, scheduling, monitoring, secret management, release gates,
   and recovery objectives.
5. **Now — product expansion:** audience segmentation, local automation,
   reporting, and Integration Hub slices are implemented; tracking/analytics
   and hosted UI/API surfaces remain separate decisions.

## Acceptance checklist

- [x] Modern package ownership and legacy compatibility are explicitly mapped.
- [x] Capability status is separated from provider or production readiness.
- [x] Every row names evidence, dependency, security/operations concern, and
  next acceptance action.
- [x] Competitor comparison is based on official product documentation.
- [x] Real recipients, credentials, SMTP, IMAP, and destructive cleanup remain
  out of scope for this consolidation.
- [x] The atlas links to the detailed sorting and MAILER inventories rather
  than replacing their compatibility evidence.
