# 0026 · Product rationalization and list utility consolidation

**Status**: Ready for Business Owner acceptance
**Outcome**: MAILFORGE-044
**Date**: 2026-11-27

## Decision

Mailforge is a local-first email utility organized around three independently
useful operations:

1. Sort email — import, normalize, validate, classify, deduplicate, filter,
   and export address data without an account or sending operation.
2. Compare lists — calculate intersection, subtraction, union, unique groups,
   and duplicates across files, saved lists, and explicitly selected
   successful submission history.
3. Send — accept pasted addresses, supported recipient files, or prepared local
   results; compose plain text or direct HTML; preview and prepare the exact
   scope; then authorize immediate, scheduled, paused, resumed, or canceled
   controlled execution through the existing campaign authorities.

History, saved lists, SMTP accounts, settings, suppression, authorization,
recovery, and evidence remain supporting capabilities. Users do not manually
create campaigns, segments, contact databases, or templates before the direct
file-to-email path can begin.

## Architecture

The existing Python modular monolith and React/Vite local web boundary remain.
`EmailSorter` is the only address-processing authority. The new
`ListComparisonService` is read-only and delegates identity to
`mailforge.domain.email`; it does not import contacts or create a second
deduplication implementation. The direct sender creates internal recipient and
message records, then delegates to the existing published-message,
`CampaignOperationsService`, `OutboundService`, policy, authorization,
scheduling, worker, and SQLite persistence boundaries.

Legacy backend modules and tables are retained only where they protect
historical data, migration, suppression/evidence compatibility, or existing
contracts. They are no longer ordinary frontend destinations. No user-owned
data, credentials, backups, or operational history are deleted.

## Capability disposition

| Capability | Disposition | Evidence / boundary |
| --- | --- | --- |
| Email sorting, provider/domain classification, invalid and duplicate handling | Retain and improve | Existing `EmailSorter` and `ListProcessingService`; direct Sort email UI |
| Cross-list comparison and successful-send exclusion | Add and retain | `ListComparisonService`, `/api/v1/list-comparison/preview` |
| Direct file/paste-to-send with plain text or HTML | Consolidate | New sender UI over existing campaign/message authorities |
| Sending history and account setup | Retain and simplify | Existing campaign/provider APIs, compact frontend utility views |
| Contacts and saved lists | Retain as optional support | Existing records remain readable; saved lists are not required for sending |
| Data Studio, integrations, programmable functions, visual automations | Retire from ordinary product surface | Existing compatibility code remains only where consumers/data require it |
| Inbound mailbox management and reply workflow | Retire from active surface | Outbound-only boundary and historical delivery evidence remain |
| Marketing segmentation, campaign naming as a prerequisite, CRM assumptions | Remove from primary experience | No replacement marketing platform introduced |

## Research applied

- Repository inspection at the starting checkpoint showed a 1,294-line
  `web/src/App.tsx`, 17 frontend destinations, and multiple data/automation/
  inbound surfaces competing with the retained workflows.
- The existing design contract, backend routes, schema, list sorter, campaign
  lifecycle, and 358-test baseline were inspected before implementation.
- GOV.UK Design System task-list guidance was inspected at
  `https://design-system.service.gov.uk/components/task-list/`; its short
  task names, visible sentence-case statuses, and user-controlled order
  informed the task-first home and independent tools.
- WCAG 2.2 label guidance was inspected at
  `https://www.w3.org/WAI/WCAG22/Understanding/labels-or-instructions.html`;
  it informed persistent visible labels and instructions.
- Primer navigation guidance was inspected at `https://primer.style/`; its
  skip/navigation conventions informed the compact labeled rail and mobile
  focus behavior.

## Data and safety boundaries

- Comparison is non-destructive and returns explicit counts for invalid,
  duplicate, unique, reference, and result records.
- Historical exclusion is opt-in per selected campaign and counts only
  accepted provider submissions. It is not suppression and does not impose a
  permanent exclusion.
- Unknown mail-host classifications remain unknown. Format/DNS checks do not
  claim mailbox existence or inbox delivery.
- Direct sending retains preparation integrity, eligibility/suppression
  checks, authorization, scheduling, durable outcomes, pause/resume, recovery,
  and ambiguity handling.

## Acceptance

The owner acceptance path is documented in
`docs/MAILFORGE-044-OWNER-ACCEPTANCE.md`. Local Python tests and a Node 24
source build/lint are supporting evidence; they do not establish human
usability, real-provider delivery, packaged qualification, or production
distribution.
