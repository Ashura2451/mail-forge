# Spec 0003: durable inbound email operations

**Status:** Assumed for implementation; operational/provider acceptance remains
required before real mailbox use.

## Summary

Add a local-first inbound operations boundary to the existing SQLite campaign
authority. It ingests local `.eml`, MBOX, and deterministic synthetic messages,
and exposes a future IMAP contract without opening external connections. It
parses untrusted RFC 5322/MIME data within explicit limits, classifies replies,
auto-replies, DSNs, complaints, unsubscribe requests, and unknown evidence,
correlates messages to existing outbound records, and applies only
policy-authorized suppression/reconciliation actions.

## Requirements

- Separate acquisition from processing, persist source identity/cursors/runs,
  and resume after interruption without duplicate consequential actions.
- Use maintained standard-library email parsing with bounded message size,
  headers, MIME depth/parts, decoded payload, and safe text previews.
- Parse multipart/report delivery-status and feedback-report structures,
  preserving per-recipient fields and unknown/malformed evidence.
- Distinguish observed signals, classification, correlation, trust, proposed
  action, applied action, and review state.
- Never treat From, Message-ID, Authentication-Results, subject keywords, or
  valid MIME alone as authenticated evidence.
- Route untrusted/ambiguous complaint and unsubscribe evidence to review;
  trusted synthetic exact evidence may apply an existing suppression through
  the campaign store, never through a second suppression authority.
- Add operator CLI workflows for import, inspect, review, decision, replay,
  statistics, and synthetic end-to-end acceptance.
- Keep IMAP read-only and disabled for real network use; model UIDVALIDITY,
  stable UIDs, cursor invalidation, repeated fetch, disappearance, and errors
  through a fake protocol endpoint.

## Decision

Use `email.policy.default` and `BytesParser`; do not implement a MIME parser.
Persist inbound metadata, hashes, optional raw bytes, parse/classification/
correlation evidence, DSN/feedback records, actions, reviews, source cursors,
and acquisition runs in schema version 3 of the existing campaign database.
Use content hashes plus source identity for idempotency. Reuse contacts,
campaigns, prepared messages, delivery attempts, suppressions, and
`operational_events` as the existing authorities.

Synthetic sources may be marked trusted for deterministic acceptance fixtures;
local files and future IMAP remain untrusted by default. No inbound path sends
automatic responses. Policy actions are idempotent and auditable.

## Build plan

Tracer bullet: process one synthetic message through acquisition, parsing,
classification, correlation, policy, and durable inspection. Thicken it with
RFC-aware DSN/ARF parsing, human/automatic reply signals, unsubscribe review,
EML/MBOX adapters, fake IMAP acquisition, bounded parsing failures, replay and
cursor recovery, concurrent processing, backup/restore, CLI workflows, and a
complete synthetic outbound-to-inbound scenario.

## Consequences and follow-up

Inbound evidence becomes durable and replayable without claiming authenticity
or inbox delivery. Raw retention is explicit and bounded; ordinary diagnostics
contain metadata and excerpts rather than complete message contents. Real IMAP
authentication, provider trust, DKIM/SPF/DMARC verification, mailbox
acknowledgment, automatic replies, and production retention policy remain
integration decisions.

## References

- [RFC 3464 — Delivery Status Notifications](https://www.rfc-editor.org/rfc/rfc3464)
- [RFC 3834 — Automatic Responses](https://www.rfc-editor.org/rfc/rfc3834)
- [RFC 5322 — Internet Message Format](https://www.rfc-editor.org/rfc/rfc5322)
- [RFC 5965 — Email Feedback Reports](https://www.rfc-editor.org/rfc/rfc5965)
