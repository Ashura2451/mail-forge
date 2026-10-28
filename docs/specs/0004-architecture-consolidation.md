# Spec 0004: architecture consolidation and capability baseline

**Status:** Accepted
**Date:** 2026-10-27

## Summary

Mailforge is consolidated around one local-first Python modular monolith while
retaining MAILER data/resources as an explicit compatibility boundary. Verified
SORTER/MSORTER behavior is now owned by the package and their source wrappers
are retired; their operational data paths remain preserved. The
capability atlas is the product claim ledger: every capability has an owner,
evidence level, dependency, security/operations notes, and an acceptance path.
This keeps synthetic foundations useful without presenting them as live email
or mailbox infrastructure.

## Context

The repository contains a modern package and three older file-oriented tools.
Several prior slices already established sorting parity, a durable synthetic
campaign core, outbound preparation, and inbound evidence processing. The
remaining architecture risk is not lack of code; it is ambiguity about which
implementation owns a behavior and whether “implemented” means local,
simulated, provider-ready, or production-operated.

## Requirements

- **AC-1:** The repository names `src/mailforge/` as the modern owner for new
  business rules, retires verified SORTER/MSORTER source, and documents MAILER
  as a deferred legacy subsystem.
- **AC-2:** Each capability in the product atlas has a status, owner,
  dependency, acceptance evidence, security/operations note, and next action.
- **AC-3:** The modern package has automated checks for layer direction, no
  legacy imports, and no import cycles.
- **AC-4:** Unreferenced dependencies are removed only after repository-wide
  import evidence and the lockfile is regenerated.
- **AC-5:** No real recipient migration, credential use, SMTP transmission,
  mailbox connection, or destructive cleanup occurs as part of consolidation.

## Decision

**Chosen option:** Option 1 — additive modular-monolith consolidation, followed
by the bounded SORTER/MSORTER source-retirement exception in task 009.

Keep one installable package with `domain`, `application`, `infrastructure`,
and `interfaces` layers. Keep `MAILER/` resources available after its
capability-specific migration contract, compatibility evidence, and rollback
plan are proven. Preserve
MSORTER/SORTER data and configuration without retaining their retired source.
Use the atlas, architecture audit, and legacy-retirement document as durable
decision and claim records.

## Rationale

An additive boundary preserves the only compatibility evidence available for
legacy relative paths, state files, and live-send gates. A rewrite or service
split would make the most consequential behavior harder to test while adding
operational infrastructure the repository does not yet need. The package
already provides the right seam for future CLI, desktop, or service interfaces.

## Standard definition

**Canonical pattern:**

```text
interface → application service → domain policy
                         ↘ infrastructure adapter
legacy entrypoint → package service (only where an adapter is proven)
```

**Replaces:**

- New business rules added directly to legacy scripts.
- Interfaces duplicating sorting, campaign, suppression, or inbound policy.
- Claims that conflate synthetic simulation with provider or inbox delivery.

**Enforcement:**

`tests/test_architecture_boundaries.py` checks import direction, rejects
modern-package imports of legacy areas, and detects package import cycles.
Ruff, the locked dependency graph, and the capability atlas provide the
remaining automated and review-time controls.

**Rollout:**

New package code follows the standard immediately. Existing legacy violations
remain compatibility debt and are migrated only through a capability-specific
contract.

**Exceptions:**

MAILER may retain its historical file/state behavior until its migration
contract is accepted. Retired SORTER/MSORTER source is not a runtime boundary;
their data files remain preserved. No modern domain or application code may
depend on legacy areas.

## Build plan

1. Record the audit disposition and capability atlas, satisfying **AC-1** and
   **AC-2**.
2. Add package-boundary regression tests, satisfying **AC-3**.
3. Remove only repository-wide unreferenced dependencies and regenerate
   `uv.lock`, satisfying **AC-4**.
4. Run tests, static checks, build, secret guard, and compatibility smoke
   checks without accessing live data or networks, satisfying **AC-5**.

## Consequences

**Positive:** ownership is explicit; compatibility remains available; future
work can be planned by capability and dependency; synthetic/local claims are
separated from production readiness.

**Tradeoffs:** legacy data/resources remain; some capabilities require explicit
operator migration; the atlas requires maintenance; a real provider and
operator workflow still require later decisions.

## Follow-up

- [ ] Decide consent, suppression ownership, retention, and legal/operator
  approval before real recipient migration.
- [ ] Specify provider adapters, SMTP phase evidence, retry/reconciliation,
  rate limits, and release rollback before live delivery.
- [ ] Specify IMAP authentication, trust verification, acknowledgment, and
  retention before real mailbox operations.
- [ ] Decide whether a scheduler/worker service, desktop interface, or API is
  the next product boundary based on an actual operator workflow.

## References

**Project sources:** `.aios/architecture.md`, `.aios/repo_truth.md`,
`docs/architecture-audit.md`, `docs/product/capability-atlas.md`,
`docs/sorting-capability-matrix.md`, `docs/mailer-capability-inventory.md`,
`docs/mailer-migration-contract.md`, and specs 0001–0003.

**Practices:** additive strangler migration, explicit trust boundaries,
fail-closed consequential operations, dependency direction, and evidence-based
capability claims.
