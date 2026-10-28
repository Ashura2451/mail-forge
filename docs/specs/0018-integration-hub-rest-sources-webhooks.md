# Integration Hub, REST Sources and Durable Webhooks

**Status**: In Progress
**Date**: 2026-10-28
**Milestone**: MAILFORGE-INTEGRATION-HUB-028

## Requirements

Mailforge provides a local-first Integration Hub for bounded read-only HTTP
sources, scoped local API clients, structured external observations, and
durable signed webhook delivery. It remains a loopback application and does
not create a hosted callback service, a second workflow engine, or an
unrestricted email path.

Acceptance criteria:

- **AC-A** `IntegrationHubService` is the canonical client/event/webhook and
  integration-diagnostics authority.
- **AC-B** `DataConnectionsService` supports fixed-origin, read-only HTTP
  profiles with JSON object/array, JSONL, and CSV extraction.
- **AC-C** HTTP requests enforce approved origin/path, TLS, credential,
  redirect, response, record, page, timeout, retry, and destination bounds.
- **AC-D** HTTP records retain page/row provenance, retrieval time, source
  identity, bounded-observation consistency, checksum, and immutable snapshot
  history; incomplete artifacts are not published.
- **AC-E** completed HTTP snapshots use the existing Data Studio handoff.
- **AC-F** local integration clients have explicit non-wildcard scopes,
  one-time token display, hashed verifiers, expiry, rotation, revocation,
  last-used metadata, and audit records.
- **AC-G** client lifecycle operations are bounded and do not expose secrets,
  cookies, URLs with tokens, or plaintext durable credentials.
- **AC-H** incoming events use a bounded typed envelope with event identity,
  source, type, timestamp, schema version, payload, provenance, and auth
  result.
- **AC-I** event intake requires an authenticated client and the
  `INGEST_LOCAL_EVENT` capability; browser session and bearer-token auth stay
  separate.
- **AC-J** exact event duplicates are idempotent, conflicting same-identity
  payloads are retained as conflicts, and invalid/expired/unauthorized
  outcomes are durable.
- **AC-K** external events are labelled observations and cannot impersonate
  trusted Mailforge evidence or select executable code.
- **AC-L** matching events may enroll only preconfigured Automation triggers,
  through `AutomationService`, with stable source identity and durable
  idempotency.
- **AC-M** webhook subscriptions have fixed destinations, selected registered
  event types, version, secret reference, scope, explicit enablement, preview,
  and history.
- **AC-N** payloads contain stable references and minimal operational data;
  email bodies, full recipient lists, credentials, private paths, and
  unnecessary personal data are excluded.
- **AC-O** deliveries sign the raw versioned JSON body with per-subscription
  HMAC-SHA256, timestamp, event identity, delivery identity, and constant-time
  verification examples; secrets never enter logs or responses.
- **AC-P** webhook destinations use TLS and fixed-origin/private-address/
  redirect checks; test-only loopback transport is explicitly injected.
- **AC-Q** delivery attempts, retry state, HTTP outcomes, errors, and
  completion are durable and recovered by the existing worker.
- **AC-R** delivery is explicitly at-least-once; ambiguous post-send outcomes
  remain visible and no exactly-once claim is made.
- **AC-S** diagnostics expose bounded source, client, event, subscription,
  delivery, retry, and authentication outcomes without secrets.
- **AC-T** `ReportingService` reports integration inventory and evidence-based
  source/event/webhook counts and last-success/error state.
- **AC-U** the browser has an accessible Integration Hub surface for source,
  client, event, webhook, delivery, and diagnostic operations.
- **AC-V** API and CLI operations reuse the same application services and
  reject generic HTTP execution or shell/code payloads.
- **AC-W** the packaged macOS runtime includes schema, dependencies, worker,
  compiled frontend, and credential-reference behavior.
- **AC-X** backup/restore preserves nonsecret integration configuration and
  history without reactivating unverified destinations or replaying completed
  deliveries.
- **AC-Y** existing Mailforge capabilities, including Email Standards and
  unsubscribe safeguards, remain intact.
- **AC-Z** security and resource-limit tests cover auth, SSRF, redirects,
  replay, malformed input, pagination loops, signature failures, retries,
  restart, backup, and cross-profile exposure.
- **AC-AA** a local synthetic REST → snapshot → Data Studio → event →
  Automation → signed webhook scenario is runnable with canonical services.
- **AC-AB** bounded synthetic performance measurements are recorded without
  claiming internet-scale throughput.
- **AC-AC** full engineering validation is run and reported truthfully.
- **AC-AD** independent review status is reported as observed, not fabricated.
- **AC-AE** architecture, capability, security, runtime, and retry semantics
  are synchronized into durable project truth.
- **AC-AF** feature publication is verified through the normal branch/PR path.
- **AC-AG** `origin/main` integration state is verified after publication.

## Decision

The implementation remains in the Python modular monolith. `CampaignStore`
remains the only SQLite authority and receives one additive migration (schema
22). `DataConnectionsService` owns source profiles, HTTP acquisition, and
immutable snapshots. `IntegrationHubService` owns local client credentials,
incoming event envelopes, webhook subscriptions/delivery state, signing, and
diagnostics. `AutomationService` owns workflow definitions/enrollment and the
existing supervisor remains the only background scheduler. `ReportingService`
owns aggregate reporting. `EnvironmentCredentialResolver` (or an injected
test resolver) remains the only secret-value boundary.

HTTP source profiles contain an operator-approved HTTPS origin and normalized
resource path, static query parameters, a selected response format, pagination
configuration, and a symbolic credential reference. Production HTTP uses
certificate/hostname verification and refuses redirects across the approved
origin. A test-only injected transport may use loopback fixtures; that mode is
not accepted as a normal persisted production profile.

Local integration tokens are random bearer secrets returned once at issuance
or rotation. SQLite stores only a SHA-256 verifier and a display prefix.
Scopes are `READ_EXTERNAL_DATA`, `INGEST_LOCAL_EVENT`, `DELIVER_WEBHOOK`,
`INSPECT_INTEGRATION`, and `START_APPROVED_JOB`; no wildcard scope is accepted.

Incoming events use a CloudEvents-inspired but explicitly non-conformant local
envelope. Registered event types are observations, not trusted evidence. A
matching Automation trigger uses a pinned active workflow revision and the
existing enrollment idempotency key. Webhook deliveries are planned from
existing `operational_events`, persist stable event/delivery identities, and
use bounded exponential retries with visible ambiguous outcomes.

## Feature design

### Durable data model

Schema 22 adds:

- `integration_clients` and append-only `integration_client_audits` for
  client identity, scope JSON, verifier hash, expiry/revocation/rotation, and
  last-use metadata;
- `integration_events` and `integration_event_conflicts` for envelope,
  authentication result, observation trust, exact duplicate status, and
  immutable conflict evidence;
- `integration_webhook_subscriptions` for fixed endpoint, event catalog,
  payload version, secret reference, scope, and explicit enabled state;
- `integration_webhook_deliveries` for operational event identity, stable
  delivery identity, attempt number, due time, HTTP result, retry disposition,
  ambiguity, and terminal error evidence.

Secret values, bearer tokens, message bodies, full recipient datasets, and
private filesystem paths are not durable in these tables.

### Service and API surface

`IntegrationHubService` exposes typed methods for client issue/list/rotate/
revoke, event catalog/intake/list, subscription create/update/preview,
delivery planning/processing/history, diagnostics, and HMAC verification
examples. Browser routes under `/api/v1/integrations/*` use the existing local
session/CSRF boundary. Only `/api/v1/integrations/events` accepts a bearer
client token, and it still requires loopback Host validation and a declared
scope. API list responses are bounded and paginated.

CLI commands under `mailforge integrations` call the same service. No route or
command accepts arbitrary method, arbitrary URL, shell, Python, or executable
payloads.

### Value sourcing

| Value | Source |
| --- | --- |
| HTTP destination | persisted normalized profile origin/path, validated at create and connection time |
| HTTP credential | symbolic profile `credential_ref`, resolved only by the credential resolver |
| snapshot provenance | adapter page identity, row number, source identity, retrieval timestamp, and response checksum |
| client authorization | persisted client scope/expiry/revocation plus bearer verifier match |
| event identity/outcome | typed request envelope plus durable client/source/event identity and payload hash |
| Automation enrollment | active workflow revision and matching registered trigger in `AutomationService` |
| webhook payload | selected existing `operational_events` record and minimal allowlisted projection |
| webhook signature | raw serialized versioned payload, subscription secret reference, timestamp, event and delivery IDs |
| retry disposition | durable HTTP/transport outcome and bounded attempt policy |
| reporting counts | direct aggregate queries over integration and existing operational records |

## Security model and invariants

- The local app remains loopback-only; no wildcard CORS, public callback, or
  `0.0.0.0` binding is added.
- All external destinations are fixed and checked against DNS-resolved address
  policy at connection time. Redirects are refused or same-origin bounded;
  credentials are never forwarded to a changed origin.
- Every integration mutation is bounded, audited where it changes access or
  external delivery, and idempotent where it can cause a consequential effect.
- Event identity is scoped to authorized client/source. Exact duplicates never
  re-enroll a workflow; conflicting payloads never overwrite prior evidence.
- A webhook 2xx is only a receiving HTTP observation, not proof of downstream
  business completion. Ambiguous outcomes remain ambiguous.
- Restored subscriptions remain disabled when destination/secret readiness is
  not revalidated; completed deliveries are never replayed by restore.

## Build plan

- [ ] Add schema 22 tables and migration tests. (AC-A, AC-F, AC-H, AC-M, AC-Q)
- [ ] Extend Data Connections with the bounded HTTP adapter, pagination,
  provenance, snapshots, and Data Studio handoff tests. (AC-B–E, AC-Z)
- [ ] Implement `IntegrationHubService` client/event/webhook lifecycle,
  signing, retries, diagnostics, and Automation/Reporting integration.
  (AC-A, AC-F–T, AC-X)
- [ ] Add secured API/CLI and accessible Integration Hub browser surface.
  (AC-I, AC-U–V, AC-Y)
- [ ] Run synthetic connected, security, migration, package, performance, and
  full validation; update truth and publish normally. (AC-W, AC-AA–AG)

## Consequences

This slice supports useful HTTP integrations and durable local event/webhook
workflows without making Mailforge a hosted integration service. It does not
claim full CloudEvents conformance, provider-specific OAuth compatibility,
exactly-once external delivery, point-in-time consistency for ordinary remote
pagination, public inbound webhooks, internet-scale performance, real provider
qualification, or live email sending.
