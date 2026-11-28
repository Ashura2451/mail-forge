# 0027 · Direct-manipulation productivity utility

**Status**: Accepted
**Date**: 2026-11-28

## Summary

Mailforge will become a direct-manipulation desktop productivity utility for
files, email addresses, lists, messages, and attachments. The main surfaces are
an address table, provider-group controls, a comparison table, and a familiar
email composer; persistence, provenance, progress, authorization, and activity
remain automatic supporting behavior. This specification preserves the
existing Python/FastAPI/SQLite authorities and is the implementation contract
for MAILFORGE-045 after the Business Owner’s required amendments.

## Context

The prior task-led, artifact-first workbench was rejected because it introduced
administrative work-item concepts, consumed excessive vertical space, repeated
explanations, and did not make the primary address data prominent enough. The
accepted replacement direction is an object-first split view:

- compact Files & lists library;
- task-specific main canvas for Addresses, Compare, or Compose;
- contextual inspector that can collapse or move below the main surface;
- automatic Activity, Accounts, Settings, progress, and recovery support.

The product remains three independently usable capabilities: Sort email, Compare
lists, and Send. Sorter and Compare are not mandatory prerequisites for Send.
No external SMTP operations are authorized by this feature.

The current product scope and backend boundaries in
[`0026-product-rationalization.md`](../0026-product-rationalization.md) remain
authoritative where this specification is silent. This specification replaces
only the rejected frontend interaction direction and adds the missing UX/data
invariants required to expose the retained capabilities correctly.

## Requirements

### AC-1 · Direct object manipulation

The rendered application shall present files, lists, addresses, messages, and
attachments as the primary objects. It shall not require users to create,
rename, or manage administrative work items before using Sort email, Compare
lists, or Send. Durable operational records remain automatic and contextual.

### AC-2 · Complete Sorter input and result workspace

The Sort email surface shall support native file import for the existing
supported formats, direct address-table inspection, search, classification
filters, individual row selection, provider-group selection, accurate counts,
output actions, and meaningful populated/empty/loading/error/recovery states.
The original input remains unchanged.

### AC-3 · Provider distribution and bulk selection

After import, the Sorter shall make provider distribution immediately legible
and support selecting one provider, multiple providers, all providers, or no
providers. Provider groups shall expose accurate counts and the resulting
selection scope. Provider-specific and combined exports shall be available.
Visible email-domain provider classification and optional actual mail-host
classification shall be presented as separate concepts; an unknown host is not
an invalid address.

### AC-4 · Exact selection and source integrity

Filtering visible rows shall not change the selected records. The interface
shall distinguish visible rows, selected records, unique sendable addresses,
duplicates, malformed rows, unknown-host classifications, and excluded rows.
Saving and reopening a list shall restore exactly the saved membership. A
selection handed to Compare or Send shall not expand, silently deduplicate
without explanation, or change source membership.

### AC-5 · Comparison without stale results

Compare shall be independently openable and support direct file/list inputs,
multiple reference lists, approved previous-send exclusion, and the existing
comparison operations. Changing either input, the operation, exclusion choice,
or result scope shall invalidate the previous result until recalculation. A
stale result shall never remain actionable for export or Compose.

### AC-6 · Direct sending

Send shall support pasted addresses, direct address entry, recipient-file
import, saved lists, and transferred results without requiring Sorter or Compare
first. It shall support sender-account selection, subject/body editing, plain
text, direct HTML paste, first-class HTML import/edit/preview, attachments,
embedded assets, optional personalization, scheduling/label options, a
separate one-recipient test, and an informed final review before consequential
sending. Large recipient sets shall use a summary with detailed inspection and
editing rather than rendering thousands of pills.

### AC-7 · Safety and canonical authority

The browser shall not implement a second classification, normalization,
comparison, policy, authorization, message-integrity, provider, or campaign
authority. Existing Python services remain authoritative. Internal authorization
grants, expiry calculations, database identifiers, credentials, and developer
controls shall not be exposed in ordinary operator flows. Provider acceptance
shall not be presented as inbox delivery.

### AC-8 · Complete retained supporting surfaces

Activity shall expose actual persisted sort, comparison, and send records with
meaningful next actions. Accounts shall provide straightforward account
configuration and contextual account selection. Settings shall retain local
storage, backup, integrity, recovery, and delivery-evidence capabilities.
Exports shall perform actual useful operations through the existing APIs, not
placeholder notifications.

### AC-9 · Progress and recovery truth

Large sorting and sending operations shall show phase, processed/total counts,
current state, supported controls, background/return behavior, and actionable
failure/recovery states derived from actual backend state. The UI shall not
promise pause, resume, cancellation, persistence, or retry semantics that the
backend does not support.

### AC-10 · Task-specific responsive interaction

The application shall not force a permanent three-pane layout on every surface.
Sorter shall prioritize provider groups and addresses; Compare shall prioritize
source lists and results; Send shall prioritize composition and review.
Supporting panels shall collapse or become contextual at narrow widths. Desktop,
laptop, and narrow layouts shall avoid page-level overflow and retain ordinary
mouse and keyboard access to essential controls.

### AC-11 · Runtime qualification

The actual packaged application shall be exercised through its normal Finder
launch path and browser environment with populated data, large-workload states,
cross-tool transitions, exact selection preservation, meaningful failures,
mouse and keyboard interactions, and narrow layouts. Independent clean-context
evaluation shall be used when available. Automated tests alone shall not be
reported as UX acceptance.

## Decision

Adopt an object-first, task-specific productivity utility over the existing
local React/FastAPI application. Keep the core navigation to Addresses,
Compare, and Compose; place Activity, Accounts, and Settings in supporting
controls and contextual entry points. Use a compact Files & lists library on
desktop, a dominant task-specific main canvas, and a contextual inspector that
collapses or moves below content when space is limited.

**Implementation skills**: `design` (`.agents/skills/design/`) · `impact-analysis` (`.agents/skills/impact-analysis/`) · `data-integrity` (`.agents/skills/data-integrity/`) · `test` (`.agents/skills/test/`) · `check` (`.agents/skills/check/`)

### Sorter design

The Sorter main canvas shall be composed in this order:

1. compact file/source toolbar with Open, source identity, safe default
   processing action, and output actions;
2. provider distribution strip or group panel with counts and explicit group
   selection controls for one, many, all, or none;
3. address-table toolbar with search, visible-row filters, and advanced
   classification/mail-host controls;
4. dense address table with email, associated fields, visible-domain provider,
   actual mail-host state, and classification/status;
5. selection scope bar showing visible count, selected record count, unique
   sendable count, excluded count, and valid next actions;
6. contextual inspector for a selected row, provider group, or selection.

Provider group selection is a first-class selection model, not a derived
checkbox convenience. The UI shall maintain the selected provider keys and
selection mode (`all`, `selected`, or `none`) through the existing handoff and
export requests. When individual rows are changed after provider selection, the
scope bar shall make the resulting row count authoritative.

### Compare design

Compare shall use a compact source toolbar with a main list, zero or more
reference lists, and a plain-language operation. A result table shall appear
only after the current input/operation/exclusion state has been calculated.
Changing any calculation input clears or marks the result stale and disables
result actions until recalculation. The result owns its own selection and
handoff scope.

Previous accepted-send exclusion remains opt-in and uses the existing campaign
history authority. It is not suppression, consent, or a permanent exclusion.

### Send design

Send shall open as an independent compose surface. Recipients appear as a
compact exact-scope summary with count, source, exclusions, search/detail access,
and an edit-selection action. A virtualized/paginated recipient detail view is
used for large selections; the primary compose surface never renders one pill
per recipient.

The composer provides account, recipient summary, subject, text/HTML editing,
HTML import, preview, attachments, embedded assets, personalization sample
selection, and optional settings. Test send is an inline separate operation.
The final review uses ordinary language and shows exact scope, message
integrity, account, attachments, and timing before invoking the canonical
authorization/execution boundary.

### Data and state invariants

- The browser uses API-provided canonical row identities and classifications.
- A saved list stores and reopens its exact member identity set; no reload path
  may replace it with the current source selection.
- `visibleRows` is a derived view of source rows and filters; it never mutates
  `selectedRows`.
- `selectedRows` is the only source for export, comparison handoff, and Send
  recipient preparation. Every downstream response echoes or can be read back
  to verify the selected identity set.
- Send scope is derived as unique, valid, policy-eligible identities from the
  selected rows, with excluded duplicates/malformed/unauthorized identities
  shown explicitly. No silent expansion is allowed.
- Provider group selection carries the selected provider keys and mode through
  export/handoff; visible-domain and mail-host classifications are not merged.
- Comparison result identity includes both source revisions/inputs and the
  requested operation. Any change invalidates the result and action buttons.
- A result, saved list, or send preview cannot be shown as current after the
  underlying source or selection has changed without recalculation.
- UI state is reload-safe through existing durable IDs and APIs; browser-only
  state is not the source of truth for persisted membership or operation state.

### Existing authorities and integration surface

Reuse the existing APIs and authorities before adding routes:

- `/list-tools/preview`, `/list-tools/jobs`, job actions, handoff, artifacts,
  and selected export for Sorter;
- `/lists`, imports, list membership, contacts, and export for saved lists and
  exact membership;
- `/list-comparison/history`, comparison preview/export/handoff, and existing
  campaign-history exclusion for Compare;
- `/campaigns`, workspace/prepare/authorize/execute/progress/outcomes/attempts,
  `/message-studio/*`, and `/providers` for Send;
- `/activity`, `/runtime`, runtime backups/integrity, and provider diagnostics
  for supporting records and recovery.

Backend changes are allowed only where an existing response or mutation cannot
support an explicit acceptance criterion, especially exact membership readback,
provider-group scope, stale-result invalidation, or truthful progress/recovery.

## Build plan

The repository has no recorded scope build approach. Use a coherent end-to-end
journey sequence: establish shared selection/source contracts first, then wire
each task-specific canvas to real services before removing the old console.

1. **Inventory and contract tests** — document current UI/API consumers and add
   focused regression coverage for exact saved-list membership, provider-group
   selection, stale comparison invalidation, duplicate/malformed/unknown-host
   distinctions, and recipient-scope preservation. Satisfies AC-2–AC-5 and
   AC-7.
2. **Shared direct-manipulation shell** — replace route-centric shell composition
   with compact Files & lists, three core views, contextual support entry points,
   responsive collapse, and a shared selection/scope model. Satisfies AC-1,
   AC-10.
3. **Sorter canvas** — implement provider distribution and group selection,
   address table/filter/inspection, exact scope bar, provider-specific/combined
   exports, and truthful progress/recovery wiring. Satisfies AC-2, AC-3, AC-4,
   AC-9.
4. **Compare canvas** — implement multiple references, previous-send exclusion,
   operation/input invalidation, result selection, and exact handoff. Satisfies
   AC-4, AC-5, AC-8.
5. **Independent Send canvas** — implement direct recipients, compact scope
   summary/detail editing, plain text/HTML compose and import/preview, assets,
   attachments, personalization, account selection, test operation, informed
   review, and truthful outcomes. Satisfies AC-4, AC-6, AC-7.
6. **Supporting surfaces and cleanup** — wire Activity, Accounts, Settings,
   real exports, runtime recovery, keyboard/narrow layout behavior, remove
   obsolete route-centric components and rejected prototype material, then
   rebuild packaged assets. Satisfies AC-8–AC-10.
7. **Qualification** — run frontend build/lint, focused Python regressions,
   browser journeys, accessibility checks, packaged Finder launch, large
   populated workloads, restart/recovery, and independent clean-context review.
   Satisfies AC-11.

## Consequences

Positive consequences:

- The user’s primary data occupies the main workspace instead of administrative
  cards or work-item metadata.
- Provider distribution, exact selection, comparison scope, and send scope are
  visible and testable at each transition.
- Send becomes independently useful while still accepting Sorter/Compare
  results.
- Existing service authorities, credential boundaries, and operational safety
  remain intact.

Costs and risks:

- The frontend needs a real shared selection model and likely focused API
  readback fields rather than purely presentational changes.
- Large-table performance requires measurement and may need pagination or
  virtualization; do not add it without confirming the actual workload path.
- The old single-file `App.tsx` composition is a replacement target and must be
  removed rather than maintained beside a second frontend architecture.
- The design is desktop-productivity-first; narrow layouts require explicit
  prioritization and cannot display every dense column at once.

## Follow-up

- [ ] Confirm whether the current backend exposes enough provider-group metadata
  and exact membership readback for all ACs before adding any endpoint.
- [ ] Measure the largest supported address-table workload in the packaged
  environment before choosing pagination versus virtualization.
- [ ] After implementation, conduct an actual unfamiliar-human usability study
  if the Business Owner can provide participants; model/browser evaluation is
  not a substitute.

## Rationale

Reasoning, research sources, runtime inventory, rejected prototype findings,
and the direct-manipulation comparison are recorded in
[`rationale.md`](rationale.md).
