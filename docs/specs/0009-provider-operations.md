# 0009. Provider accounts and controlled operations

**Date**: 2026-10-28
**Status**: Accepted for local implementation

## Summary

Provider Operations extends the existing secret-free provider profiles into a
usable account-management and diagnostics workflow. It keeps SQLite and the
current Python services authoritative, adds OAuth protocol handling and
durable diagnostic evidence, and gates every real network operation behind an
explicit narrow authorization. Synthetic SMTP, IMAP, and OAuth fixtures remain
the default acceptance environment; no live campaign is enabled.

## Context

The current provider boundary already validates explicit SMTP/IMAP profiles,
resolves symbolic credentials at runtime, performs loopback TLS protocol
checks, and preserves SMTP ambiguity. It does not yet support OAuth2, provider
presets/capabilities, account lifecycle, external diagnostics, inbound account
configuration, or a separately recorded single-recipient test submission.

Mailforge must support standards-compliant accounts and practical Google
Workspace/Gmail and Microsoft 365/Outlook setup without adding a second
delivery engine, database, hosted service, or browser-side secret store.
Provider connectivity is not delivery evidence, and SMTP acceptance is not
inbox evidence.

## Requirements

- **AC-1**: Existing simulation, campaign, Message Studio, inbound review,
  policy, backup/restore, web security, CLI, and operational recovery paths
  remain functional.
- **AC-2**: Operators can create, list, inspect, update, enable, disable,
  suspend, reauthorize, and remove named provider accounts with separate
  outbound and inbound configuration.
- **AC-3**: Account records contain no secret values; credential references are
  validated, redacted, and resolved only at runtime through environment,
  available OS-keychain, or synthetic test resolvers.
- **AC-4**: Generic SMTP and IMAP support implicit TLS and the implemented
  STARTTLS profile with certificate/hostname verification, TLS 1.2+, bounded
  timeouts, capability checks, and fail-closed downgrade behavior.
- **AC-5**: Provider presets describe Google, Microsoft, and generic standard
  protocol defaults, scopes, restrictions, and diagnostics without replacing
  explicit operator configuration.
- **AC-6**: OAuth authorization-code flows use state, PKCE, exact loopback
  redirect validation, minimal provider protocol scopes, safe callback errors,
  token expiry/refresh status, and account-identity checks. Tokens never enter
  browser storage, durable SQLite, or ordinary logs.
- **AC-7**: Configuration validation, credential readiness, connection/auth
  diagnostics, outbound readiness, inbound readiness, and controlled test send
  are distinct operations with truthful labels and persisted sanitized facts.
- **AC-8**: External diagnostics require an explicit operation authorization
  naming account/provider, endpoint, operation, sender/recipient where
  relevant, purpose, count, and time window. Without it, no external network
  operation can start.
- **AC-9**: A controlled test send requires a preview, explicit sender and
  recipient, canonical Message Studio rendering, confirmation, stable
  idempotency, one bounded submission, policy eligibility, and durable SMTP
  phase/response evidence. Post-DATA ambiguity is never automatically replayed.
- **AC-10**: Inbound operations use the canonical read-only IMAP source,
  explicit mailbox/folder, UID/UIDVALIDITY cursor, bounded fetch and parse
  limits, correlation/review/trust boundaries, and no setup-time full-mailbox
  download or remote mutation.
- **AC-11**: Campaign preparation may bind one explicit provider account; the
  selected identity and content revision are durable and deterministic. No
  automatic account rotation, restriction evasion, cross-account reassignment,
  suppression bypass, or live campaign activation is introduced.
- **AC-12**: Browser, `/api/v1`, and CLI expose the same account, validation,
  diagnostics, inbound configuration, suspension, history, and controlled-test
  semantics with existing local session, CSRF, Host/origin, upload, and
  loopback protections.
- **AC-13**: The browser provider workspace includes account list/add/details,
  diagnostics, test-message, inbound, and activity views with honest states:
  not configured, configuration validated, locally protocol-tested, external
  connected, authentication verified, operation-ready, qualification pending,
  failed, and reauthorization required.
- **AC-14**: Local synthetic acceptance covers two-account isolation, TLS SMTP
  and IMAP fixtures, OAuth state/PKCE/refresh simulations, Message Studio
  merge/conditional/CID/static/recipient attachment behavior, simulated
  temporary/permanent/ambiguous outcomes, inbound correlation and suppression,
  suspend/resume, restart/recovery, backup/restore, and integrity checks.
- **AC-15**: Security coverage includes secret redaction, OAuth mismatch and
  replay cases, wrong identity/cross-account access, TLS/certificate failure,
  arbitrary-host rejection, sender mismatch, duplicate/ambiguous submission,
  attachment isolation, session/CSRF, and worker/restart recovery.
- **AC-16**: Real-provider qualification is reported as not performed unless a
  separate owner authorization is supplied; local fixtures never imply inbox
  delivery or production readiness.

## Decision

**Chosen approach**: extend the existing `ProviderProfile`, `ProviderService`,
`SMTPConnectionPool`, `ImapSource`, `CampaignStore`, `MessageStudioService`,
`CampaignService`, and web/CLI boundaries with one canonical
`ProviderOperationsService`. Additive schema migration 11 stores account
metadata, operation authorizations, diagnostic/test records, OAuth flow
correlation, and inbound settings; secret material remains outside SQLite.

**Implementation skills**: `security` (`.agents/skills/security/`) ·
`data-integrity` (`.agents/skills/data-integrity/`) · `test`
(`.agents/skills/test/`) · `design` (`.agents/skills/design/`)

### Provider registry and account model

The registry provides explicit descriptors for `generic_smtp`, `generic_imap`,
`google_workspace`, and `microsoft_365`: protocols, TLS/ports, auth methods,
scopes, defaults, restrictions, and documentation references. A provider
account is the existing profile identity plus display name, sender identity,
limits, lifecycle, capability/readiness state, and separate inbound settings.
Presets only seed fields and never cause a connection.

### Authentication and credential boundary

`CredentialResolver` remains the runtime boundary. Environment references and
synthetic mappings remain supported; a macOS keychain adapter is optional and
reports unavailable rather than silently falling back to plaintext. OAuth
flow state stores only a short-lived opaque state, PKCE verifier, exact
redirect, account/profile reference, requested scopes, and expiry metadata.
Token exchange is injected behind an OAuth transport for local fixtures; live
token requests require the external authorization gate. XOAUTH2 SASL payloads
are created only in the transport adapter and are never returned to the UI.

Google uses the provider's documented XOAUTH2 SMTP/IMAP mechanism and the
minimum supported mail scope for the selected protocol. Microsoft uses the
documented OAuth scope for SMTP or IMAP plus `offline_access` when refresh is
requested; tenant and redirect are explicit. Basic authentication is not
treated as a modern-provider default.

### Operation authorization and diagnostics

All network-capable methods receive an explicit immutable authorization grant
containing account ref, normalized endpoint, operation, sender/recipient,
purpose, maximum submissions, and an expiry window. Configuration validation
and synthetic fixtures do not require the grant. External connection,
authentication, inbound readiness, and test send do. The service records
operation type, account, timestamp, result, phase, sanitized detail,
capabilities, external-access flag, correlation/idempotency key, and provider
response metadata. Diagnostic redaction rejects secret-bearing values before
persistence.

### Test send, inbound, and campaign binding

The controlled test path uses Message Studio's canonical revision renderer and
existing MIME composer, then invokes the selected account's SMTP adapter once.
Recipient eligibility and sender policy are rechecked at the operation
boundary. A duplicate idempotency key returns the prior durable result;
ambiguous post-DATA results are retained and non-retryable. SMTP acceptance is
named `provider_accepted`, never `delivered`.

Inbound readiness uses `ImapSource` with explicit mailbox and bounded
read-only selection. Existing parsing, trust, correlation, review, and
suppression services remain the sole inbound authorities. Campaigns gain a
nullable provider-account binding and snapshots retain that identity; unbound
synthetic campaigns continue to work.

### API, CLI, and UI

The API adds account CRUD/lifecycle, provider registry, credential-reference
status, configuration validation, OAuth start/callback status, diagnostics,
inbound configuration/readiness, controlled-test preparation/confirmation,
and history under `/api/v1/providers`. The CLI adds corresponding
`provider account`, `provider capabilities`, `provider authorize`,
`provider diagnostic`, and `provider test-send` commands. Both call the same
application service and reject arbitrary relay destinations. The existing
Providers route becomes an account workspace following `DESIGN.md`; synthetic
and external evidence are visibly distinct.

### Value sourcing

| Value | Source |
| --- | --- |
| Account identity and lifecycle | `provider_profiles` plus migration-11 account columns |
| Endpoint/TLS/auth/mailbox | explicit operator input or registry preset |
| Secret readiness | injected resolver against the stored symbolic reference |
| OAuth state/PKCE/redirect | server-generated flow record and registered provider config |
| Capability evidence | SMTP EHLO/STARTTLS or IMAP CAPABILITY/SELECT response |
| Diagnostic outcome | transport phase/result classifier and sanitized exception mapping |
| Test message bytes | pinned Message Studio revision and canonical MIME composer |
| Sender/recipient authorization | explicit operation grant and current eligibility/policy services |
| Campaign account identity | campaign provider-account binding and immutable execution snapshot |
| Inbound cursor | IMAP UIDVALIDITY plus UID in canonical inbound acquisition state |
| UI readiness label | persisted configuration, credential, diagnostic, and external-access facts |

## Build plan

1. Add migration 11, provider registry descriptors, account lifecycle fields,
   operation grants, diagnostic/test records, OAuth flow metadata, inbound
   settings, and campaign account binding. Preserve old rows and migration
   repeatability. Covers **AC-1–AC-3**, **AC-7**, **AC-11**, **AC-14**.
2. Implement OAuth state/PKCE/token-refresh protocol seams, XOAUTH2 transport
   support, provider-aware capability discovery, resolver adapters, strict
   destination/TLS/auth classification, and synthetic fixtures. Covers
   **AC-4–AC-6**, **AC-15**.
3. Implement `ProviderOperationsService` for account lifecycle, diagnostics,
   readiness, inbound configuration, and one-shot idempotent test send wired to
   existing policy, Message Studio, SMTP, IMAP, inbound, and recovery
   authorities. Covers **AC-7–AC-11**, **AC-14–AC-16**.
4. Add canonical API/CLI operations and replace the Providers screen with the
   account/diagnostics/test/inbound/activity workspace. Validate copy,
   responsive behavior, focus, and honest evidence labels. Covers **AC-12**,
   **AC-13**.
5. Add unit, migration, protocol-fixture, security, API, CLI, browser,
   recovery, backup/restore, and bounded performance evidence; refresh docs,
   packaged assets, capability status, and review packet. Covers **AC-1**,
   **AC-14–AC-16**.

## Consequences

**Positive**:

- Provider configuration, authentication, transport, diagnostics, policy, and
  UI remain separate while sharing one local authority.
- Real provider work has a visible authorization boundary and truthful evidence
  instead of being inferred from simulation.
- Existing synthetic and legacy-compatible paths remain available during
  qualification.

**Tradeoffs**:

- OAuth provider setup still requires operator-created app registrations and
  provider consent; local implementation cannot prove a commercial provider.
- SQLite stores more operational metadata but never token bytes or message
  secrets; an OS keychain is platform-dependent.
- Live campaign operation remains a separate release decision and is not part
  of this slice.

## Migration plan

**Strategy**: strangler, additive migration.

**Phases**:

1. Add nullable account/diagnostic/auth/test tables and campaign binding while
   preserving schema-10 behavior and existing provider CLI/API responses.
2. Route new account and controlled-test operations through the canonical
   service; keep existing loopback diagnostics as a compatibility wrapper.
3. Use the new account selection and evidence in browser/API/CLI acceptance;
   only a separately approved release can enable external qualification.

**Rollback**: revert the application commit while retaining additive schema-11
   tables; schema-10 provider and campaign paths remain readable. Restore a
   pre-migration SQLite backup if a database-level rollback is required.

**Risks**: token persistence, endpoint confusion, duplicate post-DATA sends,
and account reassignment are controlled by runtime-only secrets, explicit
grants, idempotent records, policy rechecks, and non-retryable ambiguity.

## Follow-up

- [ ] Obtain separate owner authorization before any commercial-provider
  connection or recipient test; then run the narrow qualification procedure.
- [ ] Qualify provider-specific app-registration, consent, quota, sender
  authorization, inbox placement, and mailbox retention behavior externally.
- [ ] Decide whether a maintained cross-platform OS keychain dependency is
  warranted after local credential usage is measured.

## Context references

- Existing project sources: `docs/provider-interoperability-012.md`,
  `docs/specs/0002-outbound-engine.md`, `docs/specs/0003-inbound-operations.md`,
  `docs/specs/0008-message-studio.md`, `DESIGN.md`.
- External research (official documentation checked 2026-10-28): [Google
  Gmail IMAP/POP/SMTP](https://developers.google.com/workspace/gmail/imap/imap-smtp),
  [Google XOAUTH2](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol),
  [Microsoft OAuth for IMAP/POP/SMTP](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
  [Microsoft authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow),
  and [Microsoft SMTP AUTH guidance](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission).

## Options considered

### Extend the existing provider boundary — chosen

Adds the missing account/auth/diagnostic contracts around the existing
transport and persistence owners. It preserves compatibility and makes the
minimum number of new authorities.

### Add provider-specific SDK adapters everywhere

Could expose richer provider APIs, but would duplicate SMTP/IMAP semantics,
increase credentials/dependency surface, and make local qualification harder.
Use focused provider adapters only if a required provider capability cannot be
represented through documented standard protocols.

### Create a hosted provider service or separate delivery worker

Could centralize OAuth and operations but violates local-first operation,
introduces another durable state owner, and expands the authorization and
deployment boundary without evidence of a need.

## Rationale

The existing modular monolith already owns the difficult invariants: secret-free
profiles, TLS transport, SMTP ambiguity, canonical MIME, inbound trust/review,
policy eligibility, and SQLite recovery. Extending those seams keeps one source
of truth and makes the new external capability auditable. Official provider
documentation confirms that Gmail and Microsoft 365 use OAuth/XOAUTH2 for
modern SMTP/IMAP access, with provider-specific scopes and configuration
restrictions; this supports a protocol adapter plus explicit OAuth contract,
not a password-only default. The main remaining uncertainty is provider-side
qualification, which is intentionally left outside local evidence.
