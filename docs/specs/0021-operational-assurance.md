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
and extend Runtime & Maintenance with progressive-disclosure health evidence.
Use the existing `operational_events` table instead of adding a second event
store. Implement `KeychainCredentialResolver` over Apple's `/usr/bin/security`
utility with opaque references and runtime-only material.

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

## Follow-up

Signed/notarized Keychain continuity, real provider quotas, hosted monitoring,
automatic launch-at-login, and business-approved RPO/RTO remain separate
decisions.
