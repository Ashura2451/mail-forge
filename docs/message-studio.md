# Message Studio

Message Studio is the local composition surface for safe, reusable email
content. The browser edits a bounded structured document; it does not persist
arbitrary HTML as the business authority.

## Canonical path

`MessageStudioService` validates the document, resolves registered contact
fields and segment revisions, renders conservative email HTML and plain text,
and supplies `RenderedTemplate` plus bounded `ResolvedAttachment` records to
the existing `OutboundService` and `MessageComposer`. Browser preview, CLI
rendering, campaign preparation, and workflow preparation all use this path.

Documents are durable SQLite records in schema 10:

- `message_documents` identifies a message or reusable template.
- `message_revisions` stores immutable JSON revisions and content hashes.
- `message_assets` stores bounded app-owned image and attachment bytes.
- `message_recipient_attachments` maps a revision/contact to an explicit
  attachment identity.
- `message_campaign_snapshots` preserves the exact revision used by a run.

The structured vocabulary includes paragraphs, headings, lists, buttons,
images, dividers, spacing, columns, and segment-backed conditional blocks.
Merge fields are limited to `{{ email }}`, `{{ name }}`, and registered
`{{ contact.machine_key }}` references. Missing values use a declared fallback
or render as an empty value with a visible warning. Conditional UNKNOWN is
treated as false.

## Interfaces

The local API is under `/api/v1/message-studio/`. The main browser route is
Message Studio in the packaged React console. Practical CLI inspection uses:

```text
mailforge message list --database PATH
mailforge message validate --database PATH --document message.json
mailforge message render --database PATH --revision-id REVISION_ID
```

Campaign creation may pin a published `message_revision_id`; preparation then
creates an immutable content snapshot and uses the canonical renderer. A
workflow `PREPARE_CAMPAIGN` action follows that same pin when present.

## Safety and limits

Scripts, event-handler attributes, dangerous URL schemes, arbitrary template
code, filesystem paths, and unbounded documents are rejected. Assets are
bounded to 10 MiB each and 25 MiB per prepared message. Inline images use CID
parts; static and recipient-specific attachments are explicit MIME records.
Preview HTML is shown in a blank sandboxed iframe, and no preview or browser
action contacts a provider or recipient.

Local acceptance proves deterministic composition and MIME construction only.
It does not claim universal email-client fidelity, inbox placement, consent,
real-provider qualification, hosted operation, or live delivery readiness.
