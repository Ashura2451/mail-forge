# Spec 0002: policy-governed outbound engine

**Status:** Assumed for implementation; Business Owner and operational
security review remain required before production delivery.

## Summary

Add a modern outbound boundary that prepares deterministic personalized MIME
messages, applies sender and recipient policy, evaluates UTC/IANA schedules,
persists exact prepared bytes, and executes through either an explicitly
authorized synthetic transport or the separately gated canonical SMTP adapter.
The preserved MAILER resources remain additive and fail-closed.

## Requirements

- Render subject, text, and optional HTML with strict, sandboxed Jinja and
  bounded deterministic spintax.
- Validate addresses and headers; reject injection, unsafe assets, and
  oversized attachments.
- Persist templates, sender policies, schedules, prepared-message hashes and
  bytes, leases, policy reservations, attempt phase, and recovery evidence in
  the versioned campaign database.
- Support plain/HTML alternatives, regular attachments, inline CID assets,
  Reply-To, Message-ID, and List-Unsubscribe preparation.
- Treat RFC 8058 one-click as a gated header contract requiring HTTPS and an
  explicitly DKIM-signed message; do not imply that infrastructure exists.
- Enforce suppression at preparation and dispatch, deterministic sender policy
  limits, explicit schedule due-ness, and no automatic retry of ambiguity.
- Preserve useful MAILER compatibility surfaces without reading `.env` or
  migrating real legacy data; no live SMTP CLI is provided.

## Decision

Use `email.message.EmailMessage` with the SMTP policy and local asset
confinement for composition. Use `SandboxedEnvironment` with `StrictUndefined`,
primitive-only context, bounded expansion, and deterministic seeds for
templates. Use UTC for durable timestamps and IANA zoneinfo for local schedule
interpretation, rejecting nonexistent times and requiring a fold for
ambiguous DST times. Use SQLite immediate transactions for leases and policy
reservations. Keep transport behind a protocol; 006 supplies a parser-backed
prepared-message simulation transport only.

## Build plan and state model

The tracer bullet is the synthetic CLI demo: import contacts, suppress one,
prepare exact MIME, preview, authorize simulation, and dispatch through the
fake transport. The implementation then thickens that path with attachment
confinement, schedule evaluation, policy capacity, concurrent worker leases,
ambiguous recovery, and reporting.

Campaign state remains explicit: draft → validating → ready/scheduled →
running → completing → completed or paused. Delivery intent state separates
prepared, reserved, attempt-started, accepted, deferred, permanent failure,
suppressed, canceled, and ambiguous outcomes. Ambiguous outcomes require an
operator/provider decision and are not requeued by default.

## Consequences and follow-up

Preparation is reproducible and reviewable before dispatch, and a worker can
reacquire expired reservations without losing the distinction between a send
that never began and one whose provider outcome is unknown. The package does
not provide SMTP, DKIM signing, unsubscribe endpoint hosting, provider health
signals, inbox delivery confirmation, warmup/reputation behavior, or a
production scheduler. Those require separate approved decisions and tests.

## References

- [RFC 2369 — List-Unsubscribe](https://www.rfc-editor.org/info/rfc2369/)
- [RFC 8058 — One-Click Unsubscribe](https://www.rfc-editor.org/info/rfc8058/)
