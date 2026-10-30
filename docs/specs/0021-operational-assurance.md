# Spec 0021: operational assurance

**Status:** In Progress
**Date:** 2026-10-30

## Summary

Provide one local control surface for runtime health, background-operation
observations, durable incident history, safe diagnostics, and native macOS
credential-reference readiness. Existing campaign, provider, mailbox,
automation, data-processing, integration, scheduler, worker, and backup
authorities remain authoritative.

## Requirements

- Health must combine database integrity, storage, backup recency, and durable
  job state; HTTP availability alone is insufficient.
- Jobs and incidents must be bounded, privacy-safe, filterable, and linked to
  existing source identities without a second event bus or scheduler.
- Health checks and acknowledgments must persist through `operational_events`.
- Diagnostic exports must exclude credentials, message bodies, recipient
  datasets, and unrestricted local paths.
- `keychain:` references must resolve through the macOS Keychain adapter when
  available and fail closed when unavailable.
- Recovery and intervention controls must delegate to existing domain services;
  no ambiguous external operation may be replayed automatically.

## Decision

Add `OperationalAssuranceService` as a read-only aggregation and local-action
boundary over `CampaignStore`, `DataDirectory`, and existing supervisors. It
does not own scheduling, worker execution, campaign delivery, mailbox cursor
truth, workflow semantics, credential authorization, or policy decisions.
Expose the service through the existing FastAPI local session/CSRF boundary
and extend Runtime & Maintenance with progressive-disclosure health evidence
and canonical safe job controls. Use the existing `operational_events` table
instead of adding a second event store. Implement `KeychainCredentialResolver`
over Apple's Security.framework with opaque references and runtime-only
material; injected command runners are test-only.

Runtime ownership is intentionally application-scoped. The supported local
contract permits one Mailforge runtime owner per user installation, rather
than simultaneous independent databases. `DatabaseProcessLock` therefore
coordinates through a stable private application-data directory and holds
that directory's advisory lock for the full runtime or backup/restore
operation. The authority is not derived from the database inode, database
pathname, or a deletable marker, so canonical, relative, symlink, and
hard-link aliases cannot create a second owner. Creation, replacement, and
restore of the database remain covered by the same ownership boundary.

Operational health is conservative: active durable work without an
authoritative heartbeat or progress record is `unknown`, not `healthy`, and
`unknown` is included in overall-state precedence. Incident closure requires
an observed recovery state; missing or unavailable evidence leaves the
incident unresolved. Ambiguous external outcomes remain durable and are not
automatically replayed by recovery controls.

## Build plan

1. Add bounded operational observations and durable health-check/incident APIs.
2. Add Keychain reference resolution and redaction tests.
3. Connect Runtime & Maintenance to health, job, incident, and diagnostic
   evidence without replacing existing controls.
4. Exercise restart, backup/restore, concurrent runtime ownership, failure
   injection, browser journeys, and the packaged macOS runtime.

## Consequences

The local SQLite authority remains the only durable operational store. Health
states are truthful local observations and do not imply provider acceptance,
inbox delivery, signed release readiness, or production SLO compliance.
The single-runtime ownership decision is a deliberate compatibility boundary:
separate simultaneous database runtimes are rejected until a future product
decision defines an explicit multi-runtime coordination protocol.

## Follow-up

Signed/notarized Keychain continuity, real provider quotas, hosted monitoring,
automatic launch-at-login, and business-approved RPO/RTO remain separate
decisions.
