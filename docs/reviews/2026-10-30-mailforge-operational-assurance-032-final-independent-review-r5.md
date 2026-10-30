# Operational assurance 032 final review, revision 5

**Verdict: BLOCKED.** Reviewed HEAD `6ed81a5ca47ed93e81f335bc8c3dfe3c9ddd6f82` against `74d264b79901abda8b43ae613e5d5232c181e516`. The sole working-tree change at the start was this untracked review artifact, initially written for `0a3d7f0` and updated for this revision. This reviewer did not implement the change and used a separate execution context; author model identity and cross-model independence are not authenticated. This review does not grant merge or business approval.

## Major findings

### Startup cleanup uses a different lock with a custom database

The r4 ordinary concurrent-backup issue is improved: initialization no longer deletes publication files, and hard-link publication rejects an existing destination atomically. CLI backup and normal server startup now lock the same database before cleanup. However, `serve` accepts both `--database` and `--data-dir` (`src/mailforge/interfaces/cli.py:891-894,1091-1096`), locks the supplied database, and cleans the data directory's backup files (`:1107-1112`); CLI backup locks that directory's fixed `mailforge.sqlite3` (`:909-912`). If the supplied database differs, the locks differ while the cleanup target is shared. At `0a3d7f0`, a disposable probe demonstrated that cleanup can remove an in-progress backup publication file and cause `FileNotFoundError`; the lock mismatch at this revision leaves the same race possible for the supported custom-database configuration. A live CLI/server concurrency probe was not run at this revision. Use a lock keyed to the shared backup directory or reject inconsistent paths.

## Resolved follow-up

At `0a3d7f0`, a disposable probe showed unknown storage evidence incorrectly resolved an incident. At `6ed81a5`, `operational_assurance.py:474-485` resolves a present check only when healthy, and the added focused test verifies that unknown storage retains the open incident. This resolves the previously reported case.

## R4 follow-up and evidence

- Source-evidence disappearance after actual recovery now records a resolution; the new focused test exercises that case. Acknowledgement of an unchanged active signature now remains acknowledged on refresh. Changes in signature reopen it. The unknown-state case above remains incorrect.
- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py` at `6ed81a5`: **29 passed**, one Starlette/httpx deprecation warning, 4.04 seconds. No application code or tests were edited.
- Inspected committed `docs/reviews/2026-10-30-mailforge-operational-assurance-032-qualification-evidence.md` and `docs/performance/operational-assurance-032-2026-10-30.md`. They report executor-performed synthetic process termination, staged restore, native packaged Keychain, Maintenance viewport checks, tests, and local performance. They do not provide replayable interruption commands, captured UI artifacts or per-measurement revision identity; those reported observations were not independently rerun here. They explicitly do not establish real provider quotas, production credentials, signed/notarized release, or production scale.
- Earlier review findings and limitations remain documented in r1–r4. This re-review focused on the follow-up diff, tests and recorded evidence; no fresh native Keychain, browser, crash/restart, full regression or package qualification was executed independently.
