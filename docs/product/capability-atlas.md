# Mailforge product capability atlas

**Baseline date:** 2026-10-28
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
| 06. Templates, personalization, and content assets | **IMPLEMENTED AND VERIFIED** | `application.templates`, `composition`; sandbox, strict undefined, MIME, asset tests | Contact context; local asset root; template definitions | Sandbox and asset confinement; bounded expansion; no arbitrary code | Add approved template migration mapping from legacy YAML/Jinja; no automatic legacy template import yet. |
| 07. Outbound preparation and policy | **IMPLEMENTED AND VERIFIED** | `application.outbound`, `policy`, `scheduling`; prepared MIME, schedule, reservation, and concurrency tests | 03–06; SQLite authority | Suppression recheck, sender capacity, hashes, leases, no ambiguity retry | Keep prepared bytes reviewable; qualify a provider adapter only after 08 decisions. |
| 08. SMTP/provider delivery | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.outbound`, `application.providers`, `infrastructure.smtp`; gated campaign path, schema 6 phases/response codes, and actual loopback TLS tests | 04, 07; provider credentials; release controls | Local protocol testing is allowed; external contact and real sending remain separately unauthorized; post-DATA ambiguity is retained | Provider-specific authentication, sandbox evidence, release approval, and controlled single-message qualification remain pending. |
| 09. Inbound acquisition and parsing | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.inbound`, `inbound_parser`, `inbound_sources`; TLS IMAP UID adapter, EML/MBOX/synthetic tests, loopback protocol tests | Local files or explicitly configured provider; schema 6 | Read-only bounded acquisition, UIDVALIDITY cursors, raw retention choice, and untrusted evidence separation | Qualify provider corpus, sender trust, mailbox acknowledgment, and real IMAP authentication before external use. |
| 10. Bounce, complaint, unsubscribe, and reply handling | **IMPLEMENTED BUT NOT FULLY VERIFIED** | DSN/ARF/reply classification, correlation, review, shared suppression action, and policy-history tests | 04, 09; trust/authentication evidence | From/subject/MIME validity is not authentication; automatic real actions are disabled | Qualify provider trust and verify per-recipient DSN and complaint cases against an approved corpus. |
| 11. Analytics, tracking, and reporting | **PARTIAL** | Local campaign reports and operational events; CLI JSON output | 05, 07; event/identity model | No open/click beacon, inbox claim, or external analytics sink | Decide privacy/retention and tracking consent; implement only after product need is approved. |
| 12. Scheduling, automation, and workflows | **IMPLEMENTED BUT NOT FULLY VERIFIED** | `application.automation`, schema 8, versioned graph/revision API and accessible Automation console; focused synthetic tests | 05, 07, 17; bounded in-process worker | UTC waits, immutable revisions, deduplication, step history, and no-live-network actions; local scale only | Run browser/restart/performance acceptance; hosted multi-user automation and real-provider actions remain out of scope. |
| 13. Interfaces and operator experience | **IMPLEMENTED AND VERIFIED** | `interfaces.cli`, `interfaces.web`, `web/`; segmentation API/CLI/browser controls, compiled-serving, and Playwright desktop/narrow acceptance | Application services; local filesystem; FastAPI/React build | Loopback Host/origin/session/CSRF boundary; no multi-user or tenant model | Maintain the local console; hosted access, identity, and remote execution require separate approval. |
| 14. Persistence, backup, restore, and migration | **IMPLEMENTED BUT NOT FULLY VERIFIED** | Versioned `CampaignStore` schema 8, typed attribute/segment/audience migrations, provider and automation revision/run tables, backup/restore, lifecycle planning, legacy rehearsal; SQLite tests | Schema versioning; local operator storage | Suppression/evidence/preference/provider/workflow history survive backup/restore; actual deletion is not automatic | Define backup cadence, encryption, retention, RPO/RTO, and real-data migration rollback. |
| 15. Security, integrations, and operations | **PARTIAL** | Secret-safe credential boundary, fail-closed SMTP gate, TLS verification, local-only diagnostics, explicit adapters, and redacted provider reports | All domains; release and provider choices | No tenancy, deployment, secret-manager/keychain adapter, provider trust, quota telemetry, or production monitoring is implemented | Approve threat model, deployment boundary, secret-store integration, observability, provider integrations, and incident runbook. |
| 16. Local browser application | **IMPLEMENTED AND VERIFIED** | `interfaces.web`, `web/src`, `docs/architecture-decision-013-local-web-application.md`, `docs/web-operational-acceptance-014.md`; API lifecycle, inbound review, restart, closure, responsive, and axe checks | 03–10, FastAPI/Uvicorn, React/TypeScript/Vite | Synthetic worker only; compiled assets are packaged; browser closure does not own work; one local SQLite runtime owner | Hosted access, live provider qualification, multi-user identity, and remote execution remain separate decisions. |
| 17. Audience segmentation | **IMPLEMENTED AND VERIFIED** | `application.segmentation`, schema 7, `tests/test_audience_segmentation.py`, web API/CLI/browser controls, fixed-point and 1k/10k preview evidence | Contacts/lists, eligibility, SQLite, local React console | Typed values are registry-bound and parameterized; unknown remains non-match; recurring executions receive independent snapshots; external data is not accessed | Qualify larger real-shaped synthetic corpora and product policy before any real-recipient migration. |

## Ownership map

| Concern | Canonical modern owner | Compatibility or external boundary |
| --- | --- | --- |
| Address vocabulary | `src/mailforge/domain/email.py` | Legacy scripts call package helpers where migrated. |
| Ingestion and sorting | `src/mailforge/application/ingest.py`, `sorting.py`, `interfaces/cli.py` | Preserved SORTER/MSORTER data/config/checkpoint/report paths; source wrappers retired. |
| Campaign, policy, lifecycle, and inbound durable state | `src/mailforge/infrastructure/campaign_store.py` | Legacy MAILER files are not imported automatically. |
| Content composition | `application/templates.py`, `composition.py` | Legacy `MAILER` templates/config remain compatibility inputs. |
| Recipient eligibility and outbound policy/preparation | `application/eligibility.py`, `campaigns.py`, `policy.py`, `scheduling.py`, `outbound.py` | `infrastructure/smtp.py` is a transport boundary; `ProviderService` supplies configuration without becoming campaign authority. |
| Inbound evidence | `application/inbound*.py`, `application/providers.py` | `ImapSource` acquires read-only UID evidence; sender authentication and provider trust remain external pending. |
| Workflow orchestration | `application.automation.AutomationService`, schema 8, `interfaces.web`, `interfaces.cli` | Visual definitions are local orchestration only; campaign, eligibility, segmentation, and inbound authorities remain canonical. |
| Operator commands | `interfaces/cli.py` | Typer and script wrappers remain supported. |

## Competitor comparison

| Product family | Evidence-backed strength | Mailforge position |
| --- | --- | --- |
| Gammadyne Mailer | Broad desktop send/receive, merge, lists/databases, scheduling, throttling, tracking, bounce and automation controls | Local sorting, preparation, durable synthetic state, and inbound evidence exist; desktop UX, provider execution, tracking, and production automation do not. |
| Mautic | Campaign workflows, lead nurturing, landing/forms, web tracking, analytics, integrations, API | No behavioral/web marketing platform, workflow builder, landing pages, or API service. |
| ActiveCampaign | Multi-step automation, segmentation, CRM/integrations, A/B testing, reporting, webhooks/API | No CRM/ecommerce integration, workflow engine, external event tracking, or dashboard. |
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
5. **Now — product expansion:** audience segmentation and the first local
   workflow automation slice are implemented; tracking/analytics, integrations,
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
