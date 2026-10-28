# 0008. Message Studio and deterministic rendering

**Date**: 2026-10-28
**Status**: Proposed

## Summary

Message Studio gives local operators one durable place to compose, validate,
preview, reuse, and pin email content. A versioned structured document is the
editable source; the existing safe template renderer and MIME composer remain
the only final rendering authorities. Campaigns and workflows use an
immutable revision snapshot, so later template edits cannot change prepared
messages.

## Context

Mailforge already has safe Jinja rendering, standard-library MIME composition,
contact eligibility, segmentation, campaigns, and a local React console. It
does not yet have a durable composition surface that joins those authorities.
Adding a browser-only HTML editor or another delivery engine would make
preview, campaign preparation, and workflow execution diverge.

The feature is local-first and synthetic by default. Stored assets are
application-owned SQLite records, browser previews are isolated, and no new
provider, cloud renderer, credential, or network delivery path is introduced.

## Requirements

**User stories**:
- As an operator, I want to edit a professional message without writing HTML,
  so that reusable campaign content is practical and safe.
- As an operator, I want recipient-specific previews and warnings, so that I
  can inspect personalization, conditional branches, MIME parts, and assets.
- As an operator, I want to pin a revision to a campaign or workflow, so that
  approved content remains reproducible.

**Acceptance criteria**:
- **AC-1**: A browser operator can create, reopen, edit, reorder, save,
  duplicate, publish, archive, and preview a structured message document.
- **AC-2**: A published revision stores subject, preheader, structured blocks,
  plain text, merge fields, conditional blocks, and asset references as
  versioned JSON; concurrent saves reject a stale revision.
- **AC-3**: The canonical Python renderer produces escaped responsive HTML,
  useful plain text, deterministic merge/conditional output, warnings, and a
  size/attachment manifest for synthetic and selected contacts.
- **AC-4**: Unsafe tags, attributes, schemes, template expressions, oversized
  documents, unbounded expansion, invalid assets, and unsafe filenames are
  rejected without executing imported content.
- **AC-5**: Stored images can render as CID inline MIME parts and static
  attachments can be reused; recipient-specific mappings are isolated and a
  required missing mapping fails only the affected preparation.
- **AC-6**: Campaign preparation pins an immutable message snapshot and uses
  the same renderer as preview; later edits do not mutate that snapshot.
- **AC-7**: A published workflow campaign reference uses the pinned message
  revision through the existing automation authority.
- **AC-8**: `/api/v1` and the CLI expose listing, validation, preview, revision,
  asset, and campaign-assignment operations with existing local session,
  CSRF, host, upload, and no-live-send protections.
- **AC-9**: The Message Studio route is keyboard usable, responsive, has clear
  validation and focus states, and its preview isolates untrusted output.
- **AC-10**: Fresh databases, upgrades, repeated migrations, backup/restore,
  full Python/frontend checks, browser acceptance, security regression tests,
  and representative synthetic rendering benchmarks pass.

## Options considered

### Option 1: Canonical structured document with Python runtime rendering

The editor writes a bounded block AST. Python converts that AST into a
conservative email HTML/text template, then the existing safe renderer and MIME
composer finish the message.

**Pros**:
- One authority works in browser, API, CLI, campaigns, and workflows.
- The packaged runtime does not require Node or a hosted rendering service.
- Validation and recipient-specific attachment isolation are explicit.

**Cons**:
- The first editor is intentionally structured rather than arbitrary HTML.
- Email-client rendering still needs external-client qualification later.

### Option 2: Client-side rich-text framework with persisted HTML

Use a maintained editor to persist HTML and sanitize it before delivery.

**Pros**:
- Familiar rich-text editing and broad formatting controls.

**Cons**:
- HTML is a poor durable authority for email compatibility and conditional
  blocks; preview and backend behavior can diverge.
- Sanitization, paste handling, and migration are harder to make deterministic.

### Option 3: MJML or hosted email rendering service

Compile a source format through a Node or remote renderer.

**Pros**:
- More prebuilt email-layout primitives.

**Cons**:
- Adds runtime/build or service coupling and a second output authority.
- Conflicts with the packaged local-first and no-required-cloud direction.

## Decision

**Chosen option**: Option 1: Canonical structured document with Python runtime rendering.

Use a React structured block editor backed by migration 10 tables and one
`MessageStudioService`. Reuse `SafeTemplateRenderer`, `MessageComposer`,
`CampaignService`, `OutboundService`, `SegmentationService`, eligibility, and
SQLite. The editor supports headings, paragraphs, emphasis, links, lists,
images, buttons, dividers, spacing, columns, and conditional sections through
safe controls; arbitrary HTML is not the editable authority.

## Feature design

**Document model**: `message_documents` owns identity and lifecycle;
`message_revisions` owns immutable structured JSON and content hashes;
`message_assets` owns bounded image/attachment bytes and metadata;
`message_recipient_attachments` maps stable contacts to assets;
`message_campaign_snapshots` preserves the exact campaign source. Campaigns
gain nullable `message_revision_id`.

**Document JSON**: `{version, name, subject, preheader, plain_text_override,
fallbacks, blocks, attachments}`. Blocks use typed records (`text`, `heading`,
`button`, `image`, `divider`, `spacer`, `columns`, `conditional`). Merge fields
use `{{ email }}`, `{{ name }}`, and `{{ contact.machine_key }}`; only registered
identifiers and bounded Jinja filters are accepted. Conditions reference a
saved segment revision and treat UNKNOWN as false.

**State transitions**: document `draft → active → archived`; a document may
  have one editable draft and many immutable revisions, with one published
  revision. Campaign snapshots are immutable and rendered messages remain under
  existing prepared-intent state transitions.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth/errors |
|---|---|---|---|---|
| `/message-studio/documents` | GET/POST | pagination, name, document | documents or id | local session; 400 |
| `/message-studio/documents/{id}` | GET | id | document, draft, revisions | 404 |
| `/message-studio/documents/{id}/save` | POST | JSON, expected revision | draft revision | 400/409 |
| `/message-studio/documents/{id}/publish` | POST | expected revision | published revision | 400/409 |
| `/message-studio/documents/{id}/duplicate` | POST | name | new document | 400 |
| `/message-studio/validate` | POST | document JSON | errors/warnings | 400 |
| `/message-studio/preview` | POST | revision/document, contact, branch | subject/html/text/manifest | 400 |
| `/message-studio/assets` | POST | bounded multipart upload | asset metadata | 400/413 |
| `/message-studio/assets/{id}` | GET | id | bytes/metadata | 404 |
| `/message-studio/templates` | GET | pagination/search | published documents | local session |

Campaign create accepts `message_revision_id`; campaign prepare resolves the
pinned revision through Message Studio. Existing campaign inputs remain
supported for compatibility. The CLI adds `message list`, `message validate`,
and `message render` using the same service.

**Key invariants**:
- Stored source is bounded canonical JSON, never rendered HTML alone.
- Published revisions and campaign snapshots are immutable and content-hashed.
- All output is escaped by context; scripts, event handlers, dangerous schemes,
  filesystem paths, and arbitrary template code are rejected.
- Asset uploads are bounded (10 MiB each, 25 MiB per prepared message), MIME
  types are validated, and historical references prevent unsafe deletion.
- A recipient attachment map is keyed by contact identity; no recipient can
  inherit another recipient's asset.
- Existing eligibility, authorization, and synthetic-only delivery guards stay
  in force.

**Security model**: the existing local session, Host/origin, CSRF, CSP, and
loopback boundary protect API access. Stored preview HTML is rendered only in a
blank sandboxed iframe. Uploads are app-owned blobs and are never interpreted
as paths. No live recipient/provider data is stored in fixtures or contacted.

**Configuration required**: none.

**Critical test scenarios**:
- Create/save/publish/reopen and stale revision conflict, verifying **AC-1**,
  **AC-2**.
- Merge escaping, fallback warnings, conditional true/false/unknown, and
  deterministic repeated render, verifying **AC-3**, **AC-4**.
- CID MIME, static attachment, mapped recipient attachment, and missing-map
  failure, verifying **AC-5**.
- Edit after campaign snapshot and prepare through the workflow action,
  verifying **AC-6**, **AC-7**.
- API security, upload bounds, sandboxed browser preview, keyboard route, and
  fresh/restarted database migration checks, verifying **AC-8**, **AC-9**,
  **AC-10**.

## Build plan

1. Add migration 10, document/revision/asset/mapping/snapshot persistence, and
   campaign revision pinning, satisfying **AC-2**, **AC-6**, **AC-10**.
2. Implement validation, deterministic AST rendering, asset handling, and
   recipient attachment resolution on the existing services, satisfying
   **AC-3**, **AC-4**, **AC-5**.
3. Integrate campaign and workflow preparation with immutable snapshots,
   satisfying **AC-6**, **AC-7**.
4. Add secured API and practical CLI operations, satisfying **AC-8**.
5. Add the responsive structured Message Studio route, accessible preview,
   upload and revision controls, satisfying **AC-1**, **AC-9**.
6. Add unit, API, security, browser, migration, and performance evidence;
   refresh packaged assets and review documentation, satisfying **AC-10**.

## Consequences

**Positive**:
- Browser preview, CLI output, campaign preparation, and automation share one
  deterministic authority.
- Durable revisions and snapshots make approvals and recovery inspectable.

**Negative / tradeoffs**:
- Rich formatting is limited to the supported email-safe block vocabulary until
  more blocks are deliberately added.
- SQLite blobs are bounded and local; large media storage is a future decision.

**Neutral**:
- Existing legacy template/campaign inputs continue to work during adoption.
- External email-client qualification remains outside local acceptance.

## Migration plan

**Strategy**: strangler, additive migration.
**Phases**:
1. Add nullable campaign linkage and new tables without changing existing
   campaign preparation.
2. Route new Message Studio-linked campaigns and workflows through snapshots;
   retain the legacy template path for unlinked campaigns.
3. Qualify and expand supported blocks based on local evidence.
**Rollback**: revert the application commit while retaining additive tables;
   existing campaigns continue through the legacy path. Restore a pre-migration
   SQLite backup if a database-level rollback is required.
**Risks**: large or hostile documents, stale draft writers, and MIME size
limits are controlled by validation, optimistic concurrency, and bounded
assets.

## Follow-up

- [ ] Qualify representative output in real external email clients before any
  hosted or real-provider approval.
- [ ] Decide whether larger assets need private filesystem/object storage after
  measuring local SQLite usage.
