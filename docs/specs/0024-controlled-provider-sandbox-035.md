# 0024. Controlled provider sandbox qualification

**Date:** 2026-11-01
**Status:** In Progress
**Task:** `MAILFORGE-CONTROLLED-PROVIDER-SANDBOX-035`

## Summary

Extend the existing provider-operation workflow with an explicit, server-verified
capture-only sandbox classification. The milestone qualifies one bounded
synthetic submission from a published Message Studio revision through the
existing eligibility, proof, grant, SMTP, persistence, and reporting
authorities. It does not enable unrestricted live campaigns or contact an
external provider.

## Decision

Use the existing provider profile metadata field for a narrowly allowlisted
`local_capture_only` classification. The server accepts the classification only
when the process is explicitly enabled for local fixtures, the profile is SMTP,
the endpoint is a loopback IP literal, and the exact host/port is registered by
the trusted fixture harness through `MAILFORGE_LOCAL_CAPTURE_ENDPOINT`. A
user-controlled display name, arbitrary hostname, metadata value, or port
cannot establish sandbox authority.
The existing server-issued grant, canonical outbound proof, idempotency
reservation, SMTP outcome taxonomy, and durable provider-test row remain the
authorities.

Sandbox qualification uses purpose `sandbox_qualification`, explicitly owned
synthetic identities under `sandbox.example.com` (or reserved test domains
where the local validator permits them), and one-shot
grants. External provider credentials, real recipients, and external network
qualification remain blocked pending separate Business Owner authorization.

## Requirements

- Approved capture-only sandbox state is visible through API, CLI, and browser
  surfaces without exposing secrets.
- Sandbox grants bind provider, purpose, sender, synthetic recipient, published
  revision, proof hash, expiry, and one submission.
- Server-side checks reject non-loopback or unclassified endpoints, non-synthetic
  recipients, stale or replayed grants, mismatched proof/content, and suppressed
  recipients before transport contact.
- Provider outcomes remain distinct: prepared, authorized, attempted, accepted,
  refused, ambiguous, and inbox delivery not verified.
- Ambiguous outcomes are durable and never automatically replayed.
- Restart, idempotency, and concurrent-submission behavior are covered by
  focused tests and the existing operational authorities.
- The current browser workflow remains the single operator surface.
- Sandbox-classified accounts require `sandbox_qualification` and synthetic
  identities at both grant issuance and submission; process-gated readiness is
  reported separately from stored classification metadata.
- Controlled-test grants store a secret-free fingerprint of security-relevant
  provider configuration. Credential-reference, TLS, CA, endpoint, or related
  drift rejects transport before reservation.
- The normal single-process database lock is the recovery authority. A
  `reserved` provider-test row with no durable transport-start boundary becomes
  `abandoned_before_transport` with `not_started` phase; an `attempted` row or
  any row with a recorded transport-start boundary becomes durable,
  non-retryable `ambiguous`. Both retain the consumed grant and attempt
  identity and neither is automatically replayed.
- Operators explicitly select a published revision; the browser does not
  silently choose the first available revision.

## Explicit exclusions

Bulk live campaigns, external provider contact, real-recipient migration,
inbox-placement claims, tracking, provider-specific SDKs, public callbacks,
Windows packaging, signing/notarization, and hosted multi-user execution.

## Acceptance evidence

Local qualification must include focused unit/API tests, existing regression
tests, browser evidence for the controlled journey, CLI/API parity, security
tampering cases, process-level interruption and restart/idempotency coverage,
frontend build/lint, Ruff, package integrity before and after normal operation,
and a fresh packaged browser journey. Any external qualification is reported as
`BLOCKED` unless separately authorized and actually exercised.

The local trust assumption is limited to the Mailforge process, the registered
fixture endpoint, the non-forwarding fixture implementation, and the local
test environment. An arbitrary loopback SMTP listener is not thereby trusted.
