# 0022. Canonical outbound message proof

**Date:** 2026-10-30
**Status:** Accepted
**Task:** `MAILFORGE-CANONICAL-OUTBOUND-PROOF-033`

## Summary

Add one durable, versioned proof boundary between Message Studio composition,
campaign preparation, controlled-test authorization, and SMTP execution. The
proof records the authoritative revision, operation identity, sender,
recipient, frozen personalization/eligibility evidence, exact MIME digest,
authorization binding, timestamps, and idempotency provenance without making
the proof digest an authorization decision.

The implementation remains local-first and synthetic. It reuses
`MessageStudioService`, `MessageComposer`, `EligibilityService`,
`ProviderOperationsService`, and phase-aware SMTP outcomes. It does not enable
real-provider access or create a second renderer, eligibility engine, or send
engine.

## Requirements

- Every new campaign prepared from Message Studio has a versioned proof tied to
  its immutable published revision, campaign/run/intent identity, sender,
  recipient, personalization context digest, eligibility evidence, policy
  revision, prepared timestamp, MIME digest, and prepared artifact.
- A controlled test proof is prepared from a published Message Studio
  revision and recipient, then stored before a grant can bind to its digest.
  Caller-supplied MIME is not an authority; if supplied for compatibility it
  must byte-for-byte match the stored proof artifact.
- Controlled grants bind the account, sender, recipient, proof hash, operation
  identity, expiry, and one-submission limit. Wrong or stale proof, grant,
  sender, recipient, or idempotency identity fails before SMTP activity.
- Existing prepared artifacts remain immutable for their operation. Editing a
  document or changing a recipient after preparation creates a new proof or
  causes the current eligibility authority to block execution; it never
  silently rewrites an earlier proof.
- Proof verification is restart-safe and idempotent. Ambiguous SMTP outcomes
  retain the original proof and are not automatically replayed.
- Legacy prepared rows without proof are compatibility history only and are
  not treated as newly authorized proof-bearing operations.

## Decision

Create `outbound_message_proofs` as the canonical proof metadata record. The
record uses canonical JSON (`ensure_ascii=false`, sorted keys, compact
separators) and SHA-256 over the proof JSON; its `content_hash` is the SHA-256
of the exact MIME bytes. Campaign proofs reference the existing
`prepared_messages` artifact rather than duplicating MIME bytes. Controlled
test proofs retain their bounded MIME bytes in the proof record because no
campaign intent exists to own that artifact.

Add nullable proof references to prepared messages and provider test
submissions, and add a proof hash to provider grants. Use additive migration
25; existing records remain readable but are classified as legacy/unverified
until a new preparation creates proof evidence. The proof service owns
serialization and verification; domain services continue to own authorization,
eligibility, MIME composition, and transport outcomes.

The browser and CLI expose proof metadata and safe explanations, not raw
private bodies or hashes as the only operator language. The existing local
session/CSRF, loopback, credential, live-send gate, and provider grant
boundaries remain authoritative.

## Build plan

1. Add proof serialization, additive schema migration, persistence, and
   verification helpers.
2. Attach proofs to Message Studio campaign preparation and verify them before
   simulation/provider dispatch.
3. Add controlled-test proof preparation and bind provider grants/submission
   reservations to exact proof hashes, rejecting raw-content substitution.
4. Extend API/CLI/UI proof and refusal evidence while preserving current
   routes and no-live-send defaults.
5. Add adversarial, concurrency, restart, MIME, eligibility, browser, package,
   and synthetic SMTP fixture evidence.

## Consequences

Proof metadata makes the source revision, recipient decision, authorization,
and exact prepared bytes inspectable across restart and outcome reconciliation.
The additive record increases local SQLite metadata and keeps bounded
controlled-test MIME bytes, while campaign MIME remains owned by the existing
prepared artifact.

This milestone proves local synthetic composition, controlled fixture
submission, and authority binding only. It does not prove inbox delivery,
consent policy approval, external provider quotas, universal email-client
rendering, signing/notarization, or production-scale reliability.

## Follow-up

- Qualify representative MIME in approved external clients and providers only
  under a separate authorization.
- Define retention and privacy policy for proof-linked prepared artifacts before
  production deployment.
