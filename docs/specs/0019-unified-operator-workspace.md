# 0019. Unified operator workspace and connected UX

**Status**: Proposed

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

## Open evidence

This is a proposed build specification. Packaged macOS qualification,
application-wide connected journeys, independent review, and milestone-level
acceptance remain separate evidence obligations and must not be inferred from
the shell implementation.
