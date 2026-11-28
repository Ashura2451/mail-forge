# Rationale · Direct-manipulation productivity utility

## Context

MAILFORGE-045 was opened after Business Owner rejection of the existing
frontend despite passing automated browser/accessibility and functional checks.
Runtime discovery found the retained product surface: Home, Sort email, Compare
lists, Send, History, Email accounts, Saved lists, and Settings. The initial
task-led, artifact-first workbench proposal was rejected because it retained
administrative complexity and did not make the address data itself the dominant
workspace.

The Business Owner then accepted the direct-manipulation direction with required
amendments: provider-group selection must be first-class; selection and source
integrity must be exact; Sorter, Compare, and Send must remain independently
usable; the interface must adapt by task; complete retained functionality must
remain available; and runtime qualification must use actual packaged behavior.

## Options considered

### Task-led, artifact-first workbench — rejected

This approach used named durable work items and a persistent work queue to
connect Sorter, Compare, and Send. Runtime and prototype review found too much
vertical explanation, administration, and repeated status content. It hid the
objects users actually manipulate behind a new product abstraction.

### Spreadsheet-only address canvas — rejected

This would maximize one table’s density and put all other capabilities in
menus. It handles one open file well, but makes source switching, saved lists,
message composition, account context, and recovery too implicit.

### Direct-manipulation split view — selected

This uses familiar desktop utility conventions: a compact file/list library, a
dominant object-specific canvas, and a contextual inspector. Addresses and
comparison results remain dense tables; Compose uses normal email fields and
file actions; Activity, Accounts, and Settings are supporting views rather than
the product’s organizing abstraction.

## Research evidence

The design pass inspected the actual packaged Mailforge UI and a populated
sorting state, then compared the revised direction with primary design guidance:

- Apple’s [File management HIG](https://developer.apple.com/design/human-interface-guidelines/file-management) informed native open/recent behavior, automatic preservation, and recognizable filenames.
- Apple’s [Lists and tables HIG](https://developer.apple.com/design/human-interface-guidelines/lists-and-tables) informed dense row-based data, visible selection feedback, sortable columns, and table use for productivity data.
- Apple’s [Split views HIG](https://developer.apple.com/design/human-interface-guidelines/split-views) informed source/canvas/inspector composition.
- Apple’s [Sidebars HIG](https://developer.apple.com/design/human-interface-guidelines/sidebars) informed a compact, shallow library rather than a large competing navigation rail.
- [Microsoft Excel filter guidance](https://support.microsoft.com/en-us/excel/get-started/filter-data-in-a-range-or-table-in-excel) informed filters attached to table headers/context and the separation of visible filtering from underlying data selection.
- [Carbon File uploader guidance](https://www.carbondesignsystem.com/building-blocks/core/components/file-uploader/guidelines) informed ordinary chooser/drop interactions, explicit uploaded-file state, removal, and loading/error behavior.
- [Carbon Data table guidance](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/guidelines) informed toolbar actions, row selection, batch actions, and avoiding dashboard-card treatment for complex data.
- Apple’s [Message UI documentation](https://developer.apple.com/documentation/messageui/mfmailcomposeviewcontroller) and [Thunderbird attachment guidance](https://support.mozilla.org/en-US/kb/how-use-attachments) informed familiar recipient/body/attachment composition and explicit user approval before sending.

These sources support interaction patterns, not copied branding. Mailforge keeps
the existing calm local-utility palette and safety language in `DESIGN.md` while
replacing its route/card composition.

## Runtime and prototype findings

Direct packaged inspection found:

- primary navigation controls that did not reliably change routes under ordinary
  interaction and a mobile drawer that reported open while remaining off-screen;
- Sorter forcing processing/profile choices before showing the useful data,
  combining input, live job, artifacts, groups, handoffs, and history in one
  long surface;
- a populated 27,000+ record run exposing only a status word and changing
  counts, without a truthful progress/recovery model;
- technical set-operation language in Compare;
- hidden handoff provenance and a configuration-heavy Send form;
- History omitting sorting and comparison records.

The revised rendered prototype was then exercised with synthetic data and real
file chooser actions. It established that a direct table/library/compose model
could support file import, row filtering, independent selection, comparison,
HTML import, attachments, test-send, review, activity, accounts, settings,
pause/resume, and narrow layout handling. Independent clean-context evaluation
verified the core count/filter/comparison/compose paths and found two prototype
defects. Those were corrected in the production implementation:

1. Duplicate-only comparison results are now excluded from any sendable Compose
   scope; the user sees an explicit exclusion and cannot send duplicates as if
   they were usable recipients.
2. Imported HTML is read and rendered in a sandboxed preview iframe using the
   selected recipient’s personalization values, rather than merely being listed
   as an imported attachment.

The one-off prototype was removed after implementation. The production bundle,
browser journeys, accessibility sweep, and packaged launch are the runtime
qualification evidence; no human product acceptance is inferred from those
automated checks.

## Invariant inventory

| Invariant | Authority/source | Regression surface |
| --- | --- | --- |
| Provider and mail-host classification | `EmailSorter` and existing sort job responses | Sort provider groups, unknown-host filter, export/handoff |
| Address identity/normalization | `mailforge.domain.email` and list-processing services | dedupe, saved membership, comparison, send recipients |
| Saved-list membership | existing `/lists`, import/membership authorities | save selection, reopen, compare, send |
| Comparison result | `ListComparisonService` and comparison preview response | source/operation changes, multi-reference, accepted-send exclusion |
| Message/rendered content | Message Studio authorities | plain text, custom HTML, assets, preview, prepared message |
| Recipient policy/eligibility | campaign/policy services | preparation, exact scope, test separation, send review |
| Authorization/execution | campaign operations/outbound/provider services | review, authorize, execute, progress, pause/resume/recovery |
| Durable history/recovery | CampaignStore, list-tool job store, runtime services | Activity, restart, settings, packaged qualification |

The browser may derive display filters and selection summaries, but must not
reimplement these authorities.

## Known risks and mitigations

- **Stale result risk:** clear result state client-side when any input/operation/
  exclusion/selection changes and reject action when server revision no longer
  matches; cover with browser and API regression tests.
- **Selection expansion risk:** carry explicit identity sets and read back saved
  membership/result scope; never repopulate from the current source after save.
- **Provider classification drift:** render canonical group keys/counts and keep
  visible-domain and mail-host fields separate; add adversarial domains such as
  `proton.me`, `notgmail.example`, and custom domains to tests.
- **Large-table performance:** measure the real packaged workload before adding
  pagination/virtualization; preserve a usable selection model across pages.
- **Safety leakage:** keep credentials, private message bodies, database paths,
  and internal authorization mechanics out of ordinary UI and diagnostics.

## Decision boundary

The Business Owner’s latest instruction is treated as approval of this design
direction with the listed amendments and authorization to implement. It does
not constitute final product/UX acceptance. Final acceptance remains a human
Business Owner decision after the packaged application is exercised.
