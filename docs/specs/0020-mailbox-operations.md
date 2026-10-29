# 0020. Mailbox operations, synchronization and conversations

**Date**: 2026-10-29
**Status**: Implemented locally; fixture qualification only

## Summary

Mailforge's inbound workbench becomes a durable mailbox operator surface. It
adds folder-aware synchronization, safe message and conversation inspection,
and explicitly gated synthetic IMAP actions while preserving the existing
read-only default. Every sync and action outcome remains local, durable,
reconcilable, and honest about unsupported or uncertain remote state.

## Context

The current inbound implementation safely acquires EML, MBOX, synthetic, IMAP,
and POP3 evidence, but IMAP is read-only and mailbox-action rows stop at a
request record. Operators cannot browse folders, reconcile flags, or complete a
controlled copy/move workflow. Message detail, search, attachments, rules,
draft preparation, reporting, and the local packaged runtime already exist and
must remain authoritative.

Mailbox protocols expose unstable sequence numbers, UIDVALIDITY resets,
capability differences, partial failures, and timeouts after a command may have
reached the server. The local product must therefore preserve message evidence,
separate local observations from remote outcomes, and never turn a temporary
empty result or ambiguous mutation into silent deletion or replay.

No real mailbox, credential, recipient dataset, live reply, or external
mutation is authorized for this milestone. The browser remains loopback-only
and the packaged application remains unsigned and unnotarized.

## Requirements

**User stories**:

- As an operator, I want to select an account and folder, synchronize bounded
  mail, and inspect progress so that local evidence stays current.
- As an operator, I want to search, read, thread, inspect attachments, and
  prepare replies or forwards without executing untrusted content.
- As an operator, I want explicitly authorized synthetic flag/copy/move actions
  with durable outcomes so that mailbox changes are controlled and recoverable.

**Acceptance criteria**:

- **AC-1**: Existing EML/MBOX, synthetic, read-only IMAP, POP3 UIDL, MIME,
  review, rule, Message Studio, automation, reporting, backup, and web security
  behavior remains healthy.
- **AC-2**: Account and folder browsing exposes configured account readiness,
  folder hierarchy where available, capabilities, sync state, and truthful
  read-only/write-disabled status without contacting a provider during setup.
- **AC-3**: IMAP synchronization is bounded, incremental, restart-safe, and
  duplicate-resistant; it records UIDVALIDITY, UID, flags, cursor transitions,
  errors, and acquisition evidence without purging local history.
- **AC-4**: UIDVALIDITY changes invalidate stale cursors and block stale
  mutations while retaining prior local evidence for reconciliation.
- **AC-5**: Message inspection safely exposes metadata, bounded text/HTML
  previews, headers, MIME/attachment metadata, provenance, trust,
  classification, and related operational evidence.
- **AC-6**: Local search/filtering is paginated and supports account, folder,
  sender, recipient, subject, date, read/unread, flagged, attachment,
  classification, review, and conversation criteria where persisted.
- **AC-7**: Conversation grouping uses Message-ID, In-Reply-To, References,
  and account context; uncertain or malformed relationships remain inspectable
  rather than being merged by subject alone.
- **AC-8**: Reply, reply-all, and forward drafts reuse Message Studio,
  preserve threading and recipient safety, persist revision provenance, and
  never submit automatically.
- **AC-9**: Explicitly authorized synthetic IMAP actions support UID-scoped
  read/unread and supported flag changes, UID COPY, and UID MOVE where
  negotiated; unsupported or unsafe deletion/EXPUNGE is refused.
- **AC-10**: Action state distinguishes requested, validated, queued, in
  progress, confirmed, refused, ambiguous, failed, and requires-review; a
  timeout or partial move is reconciled before replay.
- **AC-11**: Rules, workflow, contacts, Data Studio, and Reporting handoffs
  retain existing trust and provenance boundaries and expose mailbox metrics
  without claiming delivery or external completion.
- **AC-12**: Browser, API, and CLI expose equivalent mailbox operations with
  keyboard-accessible responsive states for empty, populated, syncing, failed,
  confirmed, refused, and ambiguous outcomes.
- **AC-13**: Synthetic browser journeys verify persisted sync, threading,
  drafts, actions, refusal, workflow/data handoffs, and restart/backup recovery.
- **AC-14**: The macOS arm64 package runs the implemented fixture workflows
  without checkout-relative dependencies; no signed/notarized or real-provider
  qualification is claimed.
- **AC-15**: Bounded synthetic measurements report sync throughput, search
  latency, storage, action/reconciliation time, and resource limits with
  workload and environment details.
- **AC-16**: Architecture, repository truth, specification, changelog,
  release evidence, and independent review provenance describe only observed
  implementation and fixture evidence.

## Options considered

### Option 1: Extend the existing inbound/provider authorities

Add folder projections, mailbox identity, action lifecycle, and capability-aware
IMAP methods to the current services and SQLite store.

**Pros**:

- Preserves one acquisition, credential, message, worker, and reporting
  authority.
- Reuses existing migrations, security boundaries, and browser/API/CLI seams.

**Cons**:

- Requires careful compatibility work around the existing request-only action
  table and read-only adapter.
- The single modular monolith must carry more explicit protocol state.

### Option 2: Add a separate mailbox service and database

Create an independent mailbox engine with its own storage and synchronization
authority.

**Pros**:

- Isolates protocol-specific state and could evolve independently.
- Allows mailbox queries to be optimized separately.

**Cons**:

- Duplicates message identity, retention, audit, backup, and trust state.
- Makes Message Studio, automation, reporting, and recovery reconciliation
  materially harder.

### Option 3: Keep all remote actions as local requests

Continue recording requested actions without executing even controlled fixture
mutations.

**Pros**:

- Lowest immediate protocol and safety risk.
- Leaves the current read-only boundary unchanged.

**Cons**:

- Does not complete the required mailbox operator workflow.
- Cannot prove action lifecycle, reconciliation, or capability-aware refusal.

## Decision

**Chosen option**: Option 1: Extend the existing inbound/provider authorities.

Implement the narrow ADR `MAILFORGE-ADR-030-CONTROLLED-IMAP-MUTATIONS` through
additive SQLite migration 23, capability-aware IMAP adapters, durable mailbox
and action projections, and shared API/CLI/browser surfaces. Real accounts stay
mutation-disabled; only disposable loopback fixtures may execute mutations in
this milestone.

**Implementation skills**: `scope` (`project/.agents/skills/scope/`) · `architect` (`project/.agents/skills/architect/`) · `design` (`project/.agents/skills/design/`) · `security` (`project/.agents/skills/security/`) · `data-integrity` (`project/.agents/skills/data-integrity/`) · `performance` (`project/.agents/skills/performance/`) · `test` (`project/.agents/skills/test/`) · `check` (`project/.agents/skills/check/`) · `release` (`project/.agents/skills/release/`) · `sync` (`project/.agents/skills/sync/`) · `impact-analysis` (`project/.agents/skills/impact-analysis/`) · `develop` (`project/.agents/skills/develop/`)

## Rationale

The existing inbound service already owns acquisition, parsing, trust,
correlation, review, and durable evidence, while Provider Operations owns
accounts and credentials. Extending those boundaries avoids a second message
database and preserves the local-first recovery and backup model. The explicit
gate and fixture-only restriction address the materially different risk of
remote mailbox writes without pretending that read-only credentials imply write
authority.

The alternative request-only design cannot satisfy the milestone's completed
business operations, while a separate mailbox engine would multiply identity
and reconciliation risk. UID-scoped commands, capability checks, additive
migrations, and conservative ambiguity handling accept more implementation
complexity in exchange for truthful state and bounded failure behavior.

## Feature design

**Data model sketch**:

- `provider_inbound_settings`: existing account settings plus nullable sync
  enablement, mutation gate, last result, error, and negotiated capabilities.
- `inbound_mailboxes`: account/source folder identity, parent/delimiter,
  selectable/read-only/write capability, UIDVALIDITY, counts, sync cursor,
  timestamps, and last error. Unique `(account_ref, mailbox)`.
- `inbound_messages`: existing message evidence plus remote flags, remote
  visibility/state, and last observed timestamp. Existing source identity remains
  unique and is the immutable acquisition key.
- `inbound_mailbox_actions`: existing request identity plus account, mailbox,
  UIDVALIDITY, UID, expected/observed state, command/response evidence, attempt
  count, reconciliation timestamp, and a new lifecycle state. Existing status
  values remain compatible.
- `inbound_action_events`: append-only action transition evidence keyed to the
  action, with state, detail, and timestamp.

Foreign keys link mailbox actions to the configured provider account and local
message when known. No raw remote body is added to a second store.

**State transitions**:

`requested → validated → queued → in_progress → confirmed`; refusal and
validation failures end in `refused` or `failed`; command timeout/partial
response ends in `ambiguous → requires_review → confirmed|failed` only after
reconciliation. A duplicate idempotency key returns the original action.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/providers/{ref}/mailboxes` | GET | account ref | folders, capabilities, readiness | loopback session | account unavailable |
| `/inbound/mailboxes/{ref}/sync` | POST | folder, batch, retention, optional grant | run, cursor, counts, state | loopback + sync grant where required | stale identity, auth, bounded source error |
| `/inbound` | GET | filters, limit, offset | stable message rows and totals | loopback session | invalid filter |
| `/inbound/mailboxes/{ref}/actions` | POST | message, UID identity, action, target, grant, idempotency | durable action state | loopback + explicit action grant | unsupported, disabled, stale UIDVALIDITY |
| `/inbound/actions/{id}` | GET | action id | lifecycle, evidence, reconciliation | loopback session | not found |
| `/inbound/messages/{id}/conversation` | GET | message id | grouped messages and confidence | loopback session | not found |
| `mailforge inbound mailbox` | CLI | account/folder/action filters | JSON projections | local operator | bounded validation errors |

Existing `/inbound`, `/inbound/messages/{id}`, draft, rule, review, and provider
routes remain compatible and delegate to the same authorities.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Account view | account identity, protocol, readiness, mutation status | provider profile/account and inbound settings columns |
| Folder view | name, hierarchy, flags, UIDVALIDITY, counts, capability | IMAP LIST/STATUS/SELECT observation persisted in `inbound_mailboxes` |
| Sync | run state, counts, cursor, duplicate count, error | `inbound_runs`, `inbound_cursors`, source fetch result, and operational events |
| Message list/detail | metadata, flags, provenance, trust, classification, attachments | `inbound_messages`, parser projections, correlations, and attachment rows |
| Conversation | members and confidence | parsed Message-ID/In-Reply-To/References plus account/folder context |
| Action | requested/observed state and remote response | `inbound_mailbox_actions` and `inbound_action_events` |
| Draft | recipients, subject, threading, revision | source headers and Message Studio document/revision records |
| Reporting | sync/action/review aggregates | durable inbound runs, mailbox actions/events, and existing ReportingService |

**Key invariants**:

- Read-only and mutation-disabled are the default for every account.
- Every mutation names one account, mailbox, UIDVALIDITY, UID, action, grant,
  target, and idempotency key.
- Stale UIDVALIDITY blocks mutation and preserves old evidence.
- MOVE is used only when advertised; no broad EXPUNGE fallback is issued.
- A timeout is ambiguous until remote state is reconciled; it is never blindly
  retried.
- Temporary absence from a sync does not mark a local message deleted.
- POP3 remains UIDL/RETR read-only and does not gain folder or flag semantics.
- Untrusted message content never chooses a remote action or executable work.

**Security model**:

The loopback web session and existing CSRF/Host/origin protections authorize
local operator requests. Account credentials remain symbolic references and are
resolved only at the adapter boundary. Remote mutations additionally require a
matching expiring grant, enabled account-specific fixture mode, current mailbox
identity, and supported capability. Real non-loopback accounts are refused.
Message HTML and attachments remain inert metadata/sandboxed previews; no
private paths, secrets, or full raw bodies enter ordinary diagnostics.

**Configuration required**:

- No new secret environment variables. Existing credential references remain
  the only runtime secret boundary.
- Account mutation remains disabled unless explicitly enabled for a loopback
  fixture through the durable account setting.

**Critical test scenarios**:

- Incremental fixture sync and duplicate prevention, verifies **AC-3**.
- UIDVALIDITY change preserves history and blocks stale actions, verifies
  **AC-4** and **AC-9**.
- Successful UID STORE/COPY/MOVE fixture action and persisted reconciliation,
  verifies **AC-9**, **AC-10**, and **AC-13**.
- Timeout/partial MOVE and duplicate request remain ambiguous/reconciled,
  verifies **AC-10**.
- Missing grant, non-loopback account, unsupported capability, and deletion
  attempt are refused without a command, verifies **AC-9** and **AC-12**.
- Malicious HTML, attachment filename traversal, and forged threading headers
  stay inert and uncertain, verifies **AC-5** and **AC-7**.
- Browser, API, CLI, package restart, and backup/restore preserve the same
  durable identifiers, verifies **AC-1**, **AC-12**, **AC-13**, and **AC-14**.

## Build plan

The repository has no recorded scope delivery approach; use a tracer-bullet
sequence so each slice is exercised through persistence, service, API/CLI, and
browser before additional protocol breadth is added.

1. Record ADR 030 and add migration 23 for mailbox identity, remote message
   observations, action lifecycle, and account write/sync settings; preserve
   v22 rows and backup/restore compatibility. Satisfies **AC-1**, **AC-2**,
   **AC-3**, **AC-4**, **AC-10**, and **AC-16**.
2. Extend IMAP and synthetic protocol seams for folder discovery, flags,
   UID-scoped STORE/COPY/MOVE, capability refusal, bounded batching, and
   reconciliation. Satisfies **AC-3**, **AC-4**, **AC-9**, and **AC-10**.
3. Extend Inbound Operations, search, conversation grouping, action service,
   Message Studio handoffs, automation/reporting projections, and API/CLI
   parity. Satisfies **AC-5** through **AC-12**.
4. Build the mailbox browser workbench on the existing shell and DESIGN.md,
   including responsive/keyboard states and honest action readiness. Satisfies
   **AC-2**, **AC-5**, **AC-6**, **AC-8**, **AC-12**, and **AC-13**.
5. Add fixture security, concurrency, migration, restart, backup/restore,
   performance, browser, and packaged-runtime evidence; update documentation,
   changelog, truth, and review records. Satisfies **AC-1**, **AC-13** through
   **AC-16**.

## Consequences

**Positive**:

- Mailbox operations become usable without introducing a second database or
  transport authority.
- Unsupported and uncertain protocol outcomes remain visible and recoverable.
- Existing local-first safety and synthetic qualification boundaries remain.

**Negative / tradeoffs**:

- Migration and adapter code must carry compatibility between request-only and
  executable fixture actions.
- Folder and protocol capability variation makes the UI and test matrix larger.
- Real-provider qualification remains a separate future authorization gate.

**Neutral**:

- The package remains unsigned/unnotarized and fixture-only for mailbox writes.
- Full-text indexing, malware scanning, and permanent remote deletion remain
  outside this milestone.

## Follow-up

- [ ] Qualify a real provider only with separately recorded Business Owner and
  operator authorization covering exact account, folders, actions, limits, and
  stop conditions.
- [ ] Add provider-specific capability qualification before claiming broad
  Gmail, Outlook, or universal IMAP interoperability.

## References

**Project sources**:

- `AGENTS.md`, `DESIGN.md`, and `web/AGENTS.md`.
- Specs 0004, 0009, 0010, 0012, and 0019.
- Decisions 0004, 0006, and task decision 0008.
- `InboundService`, `InboundOperations`, `ProviderOperationsService`,
  `MessageStudioService`, `ReportingService`, and `CampaignStore`.

**Practices & standards**:

- UID/UIDVALIDITY mailbox identity and capability negotiation.
- Idempotency keys and conservative reconciliation for ambiguous effects.
- Additive SQLite migration and strangler-style extension of live authorities.
- RFC 9051 and RFC 6851 semantics as named in the task-bound packet.
