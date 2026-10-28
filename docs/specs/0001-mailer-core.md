# Spec 0001: durable MAILER campaign core

**Status:** Assumed for implementation; Business Owner ratification remains
required before production migration or live delivery.

## Summary

Add a local-first durable campaign boundary around the former MAILER
capabilities. The slice owns contacts, provenance, lists, suppressions,
campaign lifecycle, delivery intents, attempt evidence, recovery, and
synthetic execution. Task 010 subsequently retires the legacy entry point;
live SMTP remains separately gated and disabled from the CLI.

## Requirements

- Preserve existing sorting behavior and legacy MAILER resource/configuration
  compatibility through an explicit translator.
- Give each normalized contact a stable local identity and source provenance.
- Make suppression policy durable and recheck it before every reservation.
- Model campaign transitions and delivery outcomes explicitly.
- Recover interrupted reservations deterministically.
- Keep ambiguous outcomes for reconciliation and never auto-retry them.
- Provide CLI preview, simulation, reporting, backup/restore, and migration
  rehearsal paths.
- Reject secret-bearing durable configuration and keep real SMTP out of scope.

## Decision

Use the Python standard-library `sqlite3` adapter with explicit SQL schema
versioning and transaction boundaries. The adapter is deliberately narrow:
application services own orchestration, domain types own vocabulary and
transition rules, and the CLI is an interface rather than a second policy
implementation. This is an evidence-backed implementation assumption based on
the repository's local-first target and existing temporary SQLite usage; it is
not yet a ratified business architecture decision.

The initial schema includes contacts and provenance, lists and memberships,
suppressions, campaigns and transition history, delivery intents and attempts,
and append-only operational events. Secrets and message bodies are excluded.

## State model

Campaigns move through draft, validating, ready, scheduled, running, pausing,
paused, completing, completed, failed, and canceled states. Delivery intents
distinguish eligibility, reservation, attempt start, accepted, transient,
permanent, deferred, ambiguous, suppressed, and canceled outcomes. State
changes are transactional and recorded with timestamps and reasons.

## Interface

The application service exposes import, prepare, preview, simulation
authorization, pause/cancel, execution, reporting, and legacy reconciliation.
The CLI surface is under `mailforge campaign`. Its transport is a deterministic
fake used only for synthetic evidence.

## Consequences and follow-up

This slice makes restart and reconciliation semantics testable without
touching live recipient or SMTP state. It does not decide consent ownership,
retention, production scheduler/worker topology, template migration, or live
transport authorization. Those decisions require explicit approval before the
SQLite core can become the authority for real campaigns.
