# Architecture consolidation audit

**Task:** `MAILFORGE-ARCHITECTURE-CONSOLIDATION-008`
**Date:** 2026-10-27
**Scope:** repository structure, package boundaries, legacy compatibility, product capability claims, dependency manifest, and operational/security risks.

## Executive finding

Mailforge has a viable local-first modular-monolith direction, but it is not a
single finished mail platform. The packaged `src/mailforge/` tree is the
modern owner for sorting, campaign state, outbound preparation, SMTP adapter
boundaries, and inbound evidence. SORTER/MSORTER and MAILER executable source
are retired after bounded capability evidence; their data-only/resource paths
remain preserved. Consolidation is ownership and truth consolidation, not a
big-bang rewrite.

The audit found no import cycles in the modern package. It removed six declared
dependencies with no imports in source, tests, or retained entrypoints, and
added a regression test for the package's layer direction. It did not delete
legacy code, operational data, IDE history, logs, checkpoints, recipient
lists, or local secrets.

## Hygiene disposition

| Area | Disposition | Reason and action |
| --- | --- | --- |
| `src/mailforge/` | **KEEP / CONSOLIDATE** | Canonical package boundary. Keep domain/application/infrastructure/interfaces and enforce their dependency direction. |
| `MAILER/` | **RETIRE SOURCE / PRESERVE DATA** | `main.py` is retired after config/target/template/state rehearsal; YAML, templates, lists, logs, events, and `.env.example` remain data/resource inputs. |
| `MSORTER/` | **RETIRE SOURCE / PRESERVE DATA** | Package services and `mailforge` CLI own verified sorting, ingestion, DNS, disposable, and checkpoint behavior; retain configs, checkpoints, logs, and results. |
| `SORTER/` | **RETIRE SOURCE / PRESERVE DATA** | `mailforge transform` owns verified formatting, dedupe, list, and safe-count behavior; retain staged files. |
| `tests/` | **KEEP / IMPROVE** | Synthetic and fake-boundary coverage is the safety net. Add architecture-boundary regression coverage. |
| `pyproject.toml` / `uv.lock` | **CLEANED** | Removed unreferenced `pydantic`, `pydantic-settings`, `portalocker`, `structlog`, `tenacity`, and `tqdm`; retained dependencies used by modern or compatibility paths. |
| `.history/`, `.idea/`, `.DS_Store` | **PRESERVE** | Existing user working-tree changes and historical/IDE artifacts were not authored by this task. No destructive cleanup was performed. |
| `MAILER/.env`, recipient/state/log files | **PRESERVE / NEVER COMMIT** | Operational or secret-bearing artifacts. Not read, migrated, printed, or staged. |
| `__pycache__/`, `logs/` | **PRESERVE** | Generated untracked artifacts already present in the working tree; left untouched to avoid deleting user state. They are excluded from package artifacts. |

## Boundary and dependency findings

- `domain` imports no application, infrastructure, or interface code.
- `application` orchestrates domain and infrastructure services; it does not
  import interfaces.
- `infrastructure` depends on domain vocabulary and infrastructure adapters,
  not interfaces.
- `interfaces` is the outer CLI boundary and may compose the other layers.
- The modern package does not import `MAILER`, `MSORTER`, or `SORTER`; retired
  sorter source has no remaining runtime import consumers.
- The AST import graph has no cycles across the 33 modern modules inspected.
- `CampaignStore` is large, but it remains the single SQLite authority for the
  current synthetic campaign/inbound slices. Splitting it without an explicit
  repository contract would create competing persistence authorities, so that
  refactor is deferred.
- MAILER source no longer has a runtime consumer. The bounded translator keeps
  relative paths, provenance, deterministic identity, and safe dispositions
  explicit while deliberately rejecting unsafe invisible/client simulation.

## Risk register

| Risk | Current control | Remaining action |
| --- | --- | --- |
| Real SMTP or mailbox side effects | Modern transport is synthetic; legacy runner is fail-closed and dry-run is tested | Require a separately approved provider adapter, phase evidence, reconciliation, and release runbook. |
| Recipient consent and suppression authority | Modern policy records and suppressions are durable; legacy data is not auto-imported | Decide consent ownership, retention, and migration identity before real-data migration. |
| Untrusted inbound evidence | Bounded parser, trust states, review queue, and synthetic-only automatic actions | Add provider-qualified authentication and trust integration before automatic real-mail actions. |
| Legacy file/database divergence | Compatibility inventory and migration rehearsal contract | Define one migration cutover, rollback, and reconciliation procedure. |
| Local data loss | SQLite backup/restore paths and explicit local database ownership | Define operator backup cadence, retention, encryption, and recovery objectives. |
| Secret leakage | `.env` ignored and not loaded for authorization; secret scan in validation | Add release-time credential rotation evidence and keep secrets outside durable campaign state. |

## Follow-up disposition

1. Maintain the capability atlas as the product claim ledger.
2. Treat the modern package as the only place for new business rules.
3. Keep legacy adapters additive until compatibility tests and migration
   rehearsals prove a replacement.
4. Do not label simulated transport, local parsing, or synthetic suppression
   as provider-ready or inbox-delivery-ready.
5. Resolve the open decisions in the atlas before enabling any real recipient,
   SMTP, IMAP, automation, or analytics integration.
