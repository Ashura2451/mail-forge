# 0019. Unified operator workspace and connected UX

**Status**: Implemented locally; acceptance pending independent review and merge

## Summary

Mailforge's existing local services and React workbenches need a coherent
operator shell so people can move between data, audiences, communication,
incoming work, automation, connectivity, insights, and maintenance without
losing context. This specification is intentionally an enhancement of the
existing local browser application, not a new frontend architecture.

## Authority and constraints

- Python application services remain authoritative for business rules,
  durable jobs, revisions, eligibility, credentials, and evidence.
- FastAPI remains the HTTP boundary and CampaignStore remains the modern local
  SQLite authority.
- React owns presentation, navigation state, and interaction feedback only;
  it does not create a second record store or scheduler.
- Existing feature routes and deep links remain available. No mandatory cloud
  service, new database, or live-provider capability is introduced.
- Browser state may retain a non-sensitive view identity, but never tokens,
  credentials, private message data, or recipient data in local storage.

## Decision

Use a shared shell with:

1. stable view IDs mapped to browser paths and `history`/`popstate`;
2. task-oriented navigation groups with labels that remain discoverable on
   narrow screens through a responsive menu;
3. page metadata and related-work links that guide supported cross-feature
   handoffs without copying records;
4. common retryable loading/error/status feedback;
5. semantic focus and responsive layout behavior defined by `DESIGN.md`.

The shell is implemented incrementally in the existing `web/src/App.tsx` and
`web/src/App.css` boundary. Feature-specific actions continue to call their
existing API routes and pass canonical IDs or revisions where those contracts
already exist.

## Acceptance direction

- A supported workbench can be opened at its stable browser path, refreshed,
  and reached with Back/Forward while retaining the selected view.
- Major areas remain discoverable under Data, Audiences, Communication,
  Incoming, Automation, Connectivity, Insights, and Application groups.
- Narrow layouts expose a labelled navigation control and keep primary actions
  reachable without hover or horizontal page overflow.
- A failed shell load or action states what failed and offers a retry without
  inventing success or mutating durable state.
- Related-work links navigate to existing workbenches and do not duplicate
  backend records.
- Existing API/CLI services and feature behavior remain unchanged.

## Validation plan

- TypeScript/Vite build and Oxlint.
- Existing browser and accessibility suites.
- Focused browser checks for direct route loading, Back/Forward, responsive
  navigation, visible focus, and retryable failure presentation where the
  local browser harness permits.
- Python regression suite to confirm the FastAPI contract is unchanged.

## Closure evidence — 2026-10-29

The local implementation now has current rendered-screen evidence for the
dashboard, activity, data, audience, communication, incoming, automation,
connectivity, insights, email-standards, and maintenance workbenches. The
browser acceptance suite also exercises durable local business operations:
scoped client authorization and revocation, typed event idempotency, signed
loopback webhook delivery, SQLite source preview/snapshot and Data Studio
handoff, sender diagnostics, unsubscribe readiness, and staged backup restore.

The arm64 macOS bundle was launched, exercised, stopped, relaunched against
the same data directory, and verified to retain a published Message Studio
revision and a staged restore. Evidence is local and synthetic: no external
provider, public callback, mailbox, recipient, or live send was used. The
independent review, signing/notarization, external-provider qualification, and
merge remain separate obligations.

Current evidence index: `.orchestra/evidence/029/index.html`.
