# Email Standards and Sender Diagnostics

**Status**: Accepted
**Date**: 2026-10-28
**Milestone**: MAILFORGE-EMAIL-STANDARDS-027

## Outcome

Mailforge provides one local-first Email Standards authority for sender
identity inspection, bounded DNS/SPF/DKIM/DMARC diagnostics, MIME and content
validation, truthful unsubscribe readiness, safe local one-click processing,
offline DMARC aggregate import, campaign preflight findings, and durable
diagnostic history. It reports evidence quality explicitly and never equates
configuration inspection with provider acceptance or inbox delivery.

## Architecture

`EmailStandardsService` is the canonical application authority. It owns
standards rules, deterministic findings, bounded DNS inspection, MIME/content
checks, unsubscribe endpoint/token contracts, aggregate-report import, and
diagnostic history. It reuses:

- `ProviderOperationsService` for provider accounts, capabilities, and
  credentials; it never stores provider secrets.
- `MessageStudioService` and `MessageComposer` for message content and MIME
  construction; it never creates a second renderer.
- `EligibilityService` and `CampaignStore` suppression/policy methods for
  unsubscribe effects.
- `InboundService` trust boundaries for imported authentication evidence.
- `CampaignOperationsService` for preflight aggregation and execution gates.
- `ReportingService` for aggregate reporting over durable diagnostic records.

DNS inspection is explicit, bounded, and injectable with deterministic local
fixtures. Startup performs no DNS or provider requests. Python's maintained
`dnspython` resolver and the existing `tldextract` public-suffix data are used
when live inspection is explicitly requested.

## Evidence model

Every finding carries a severity (`pass`, `warning`, `blocked`, or `unknown`),
rule version, evidence class, source, observation time, and actionable next
step. Configuration facts, DNS observations, message observations, claimed
authentication headers, trusted receiver evidence, and locally verified
cryptographic evidence remain distinct.

The implementation validates DKIM record/signature structure and reports
cryptographic verification as unverified unless a complete local verification
path is available. It does not claim provider qualification, reputation,
consent, inbox placement, or delivery from local diagnostics.

## Persistence

Schema 21 adds sender identity references, diagnostic runs, unsubscribe
endpoint/token state, and deduplicated DMARC aggregate report metadata. The
database remains the authority; migrations are additive and backup-compatible.
Opaque unsubscribe tokens are stored as hashes, are scoped to one recipient
and list, expire, and apply suppression idempotently through the existing
policy authority.

## Security and limits

- Header injection, unsafe URLs, malformed MIME, untrusted authentication
  headers, invalid XML, external entities, path traversal, zip bombs, DNS
  recursion, and cross-list token use are rejected or reported unknown.
- Browser/API inputs are bounded; arbitrary DNS targets, network probes,
  private keys, provider credentials, raw message bodies, and live sends are
  not exposed.
- The local one-click endpoint accepts POST only. GET never changes policy.
  A configured local fixture is labelled local verification; no public hosted
  endpoint is claimed.
- Marketing/newsletter/product-announcement checks require unsubscribe
  readiness; transactional/service notifications do not receive a false
  promotional blocker.

## Standards and guidance

Rules are based on RFC 5322, RFC 7208, RFC 6376, RFC 7489, and RFC 8058.
Provider guidance is versioned as diagnostic context, not universal law. The
current implementation records Google sender guidance (reviewed 2026-10-28)
and Yahoo Sender Hub best practices (reviewed 2026-10-28) as scoped reference
profiles; neither is evidence of provider acceptance.

## Acceptance evidence

Focused tests cover sender identity facts, SPF dependency limits and failures,
DKIM/DMARC evidence distinctions and alignment, MIME/content diagnostics,
unsubscribe POST/replay/security behavior, bounded DMARC import and dedupe,
API/CLI parity, campaign preflight, reporting, migration/restart/backup
behavior, and browser interaction. Connected acceptance uses only synthetic
DNS/HTTP fixtures and local SQLite; no real provider, mailbox, recipient, or
live send is used.
