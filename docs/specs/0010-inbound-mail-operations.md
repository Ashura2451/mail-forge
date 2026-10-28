# Inbound Mail Operations and Processing

**Status:** Accepted for local implementation
**Task:** `MAILFORGE-INBOUND-OPERATIONS-020`
**Date:** 2026-10-28

## Summary

Mailforge's inbound foundation becomes a durable, local-first operations
workbench. It supports EML, MBOX, synthetic, and authorized IMAP acquisition;
bounded parsing and inspection; deterministic search and filtering; typed
inbound rules; safe metadata extraction/export; reply and forward preparation;
and explicit mailbox-operation requests. It remains independent of campaign
creation and does not send or mutate live mailboxes without a separate,
durable authorization.

## Context and constraints

The existing SQLite `CampaignStore`, `InboundService`, MIME parser, provider
account service, Message Studio, Automation Service, suppression authority,
web security boundary, and CLI are the established authorities. This feature
extends those services. It does not create a second inbound parser, workflow
engine, composer, provider credential store, or outbound transport.

No live mailbox, recipient data, credential, or real send is used by local
acceptance. External-provider qualification remains a separate release gate.

## Requirements

1. Preserve source-specific identities and durable cursors. IMAP identity is
   account/folder/UIDVALIDITY/UID; POP3, when used, is UIDL-only and read-only;
   local sources retain source identity plus content hash.
2. Make acquired records searchable, paginated, inspectable, and safe to
   preview. Raw evidence is retained only under the existing retention policy.
3. Persist attachment metadata and extracted values without writing untrusted
   filenames or content to arbitrary paths.
4. Validate and evaluate bounded JSON rules with explicit priority,
   idempotency, dry-run, activation, pause, and audit state. Rules cannot
   execute Python, SQL, shell commands, or network requests.
5. Use Message Studio for reply/forward drafts. Drafts preserve original
   message provenance and threading headers; preparation never sends.
6. Keep policy-changing actions behind the existing review/trust authority.
   Extracted addresses do not imply consent; adding a contact to a list is an
   explicit operator action.
7. Emit durable inbound events consumable by the existing Automation Service,
   without duplicate enrollment on replay.
8. Keep mailbox acquisition read-only by default. Any server mutation is a
   separately authorized, durable request and is not part of import/sync.

## Decision

Use an in-place extension of the canonical inbound service and store. The
workbench query, rule, extraction, draft, and mailbox-action records are
additional projections in SQLite migration 12. A small `InboundOperations`
facade exposes those capabilities to API, CLI, and browser callers while
delegating acquisition/processing to `InboundService` and rendering to
`MessageStudioService`.

Alternatives considered:

- A replacement inbound application would duplicate parsing, review, and
  policy authority.
- A separate search database would increase retention and recovery risk for
  modest local workloads.
- An unrestricted rules language would make message text an execution input.

The selected design keeps metadata queries parameterized in SQLite, stores
rule definitions as typed JSON, and records every side effect with a stable
idempotency key.

## Durable model

Migration 12 adds source/account/folder projections to inbound messages,
attachment metadata, extracted values, versioned inbound rules and rule runs,
draft provenance, and explicit mailbox-action requests. Existing raw MIME
storage and retention flags remain authoritative. New records reference the
existing message IDs and operational events. Search uses bounded indexed
metadata fields rather than a second body index.

Message processing state is derived from durable acquisition, parse,
classification, correlation, rule-run, review, and processed timestamps. A
repeated source identity or rule idempotency key returns the original result.

## Rule contract

Rules contain `version`, `priority`, `conditions`, and typed `actions`.
Conditions support account/folder, sender/domain, recipient, subject/header,
classification, correlation state, and extracted values. Initial actions are
`record_event`, `extract`, `review`, `prepare_reply`, `prepare_forward`, and
`add_contact_to_list` only when explicitly invoked/approved. Matching is
deterministic, bounded, and ordered by descending priority then stable ID.

Automatic replies are preparation-only by default. Auto-submitted,
list-generated, null-sender, bounce, and repeated-event signals are excluded
from automatic response preparation. One response per source event and
bounded per-sender limits are enforced before any future external operation.

## API, CLI, and browser surface

The existing inbound import/review routes remain compatible. The workbench
adds bounded query parameters (`q`, sender, recipient, subject, category,
source, processed, limit, offset), message detail, extraction/export, rule
validation/preview/lifecycle, reply/forward draft preparation, and explicit
local-source synchronization. IMAP synchronization is exposed only through a
configured provider account and its operation grant. The CLI gains equivalent
inspect/search/extract/rule/draft operations. The browser presents search,
filters, detail, attachment metadata, rules, extraction, and draft
preparation using the existing design system and sandboxed preview patterns.

## Security and integrity invariants

- All source and rule values are size-limited, parameterized, and secret-free.
- HTML is displayed only as sanitized text or a sandboxed preview; no remote
  resources are fetched.
- Attachment metadata is safe to inspect; filenames are basename-validated.
- CSV export escapes formula-leading values.
- No import marks remote mail read, deletes, moves, or sends.
- Trust, correlation, suppression, and eligibility continue through the
  existing inbound review and policy authority.
- Rule replay, draft creation, extraction, list addition, and mailbox action
  requests are idempotent and auditable.
- Existing host/session/origin/CSRF boundaries remain mandatory for web state
  changes.

## Verification plan

Add migration, search/filter, rule validation/evaluation/replay, extraction and
CSV/JSON export, Message Studio draft, workflow event, POP3 UIDL, IMAP cursor,
retention, malformed MIME, security, backup/restore, and API/browser tests.
Run the complete Python suite, frontend lint/build, browser accessibility and
responsive checks, locked package validation, secret scan, and a synthetic
performance sample covering hundreds of messages and bounded search/rule
evaluation. Report local synthetic evidence separately from real-provider
qualification and independent review.

## Consequences and limitations

The workbench gains durable operational value without changing the outbound
engine, but raw content remains subject to local retention and database backup
policy. POP3 cannot provide mailbox-folder semantics and is read-only. Rich
arbitrary document extraction, malware scanning, automatic live replies, and
destructive mailbox mutation remain out of scope until separately qualified.

## References

- `docs/specs/0003-inbound-operations.md` — original inbound foundation.
- `docs/specs/0009-provider-operations.md` — provider accounts and grants.
- `src/mailforge/application/inbound.py` — acquisition and review authority.
- `src/mailforge/application/message_studio.py` — canonical composition.
- `src/mailforge/application/automation.py` — durable workflow authority.
- `src/mailforge/infrastructure/campaign_store.py` — SQLite migrations and
  operational events.
