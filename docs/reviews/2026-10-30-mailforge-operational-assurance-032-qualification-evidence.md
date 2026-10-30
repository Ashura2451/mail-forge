# Operational assurance 032 qualification evidence

Date: 2026-10-30
Scope: local synthetic qualification on branch `feat/mailforge-operational-assurance-032`.

Latest source qualification revision: `327a98b`.

## Commands and results

- `PYTHONPATH=src .venv/bin/pytest -q --ignore=tests/e2e`: 274 passed, one existing Starlette/httpx deprecation warning.
- `PYTHONPATH=src .venv/bin/pytest -q tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py`: 31 passed, one existing Starlette/httpx deprecation warning.
- `PYTHONPATH=src .venv/bin/pytest -q tests/e2e`: 8 passed.
- `.venv/bin/ruff check src tests`: passed.
- `npm run build` and the configured frontend lint/oxlint command: passed; existing React warnings only.
- `bash scripts/package-macos.sh`: passed; unsigned, unnotarized bundle only.

## Recovery and integrity observations

- A forcibly terminated Campaign process recovered an in-flight attempt as `ambiguous` and did not retry it.
- A forcibly terminated Data Studio process recovered a `running` job as `pending`, restarted it, and completed the final artifact.
- The existing stale-owner lease test passed; a second worker could not advance the leased operation.
- Interrupted backup publication left no destination archive or temporary publication file. Concurrent same-destination backups produced one successful archive and one explicit destination-exists result.
- Backup validation, staged restore readback, SQLite integrity, incident persistence, and unavailable Keychain-reference behavior were exercised with synthetic data.
- Source query failures appeared as `source_evidence` action-required health evidence and resolved to a durable `incident_resolved` event after recovery. Acknowledged active incidents remain acknowledged until the condition changes.
- Active durable jobs without an authoritative heartbeat are classified `unknown`; stale `updated_at` activity is reported as stale activity, not proof of liveness or progress.

## UI and package observations

- The Maintenance workbench was checked at desktop, short desktop, tablet, and mobile viewports. Health-check and diagnostic-export controls were visible; the responsive rail/drawer remained usable without observed clipping.
- Bundled direct checks confirmed cheap liveness does not perform SQLite integrity work, detailed status reports integrity, and packaged Keychain store/resolve/rotate/revoke completed with synthetic identifiers.

## Limits

This is local evidence only. It does not qualify real providers, mailbox access, production credentials, signing/notarization, release promotion, production scale, or provider quota. Screenshots were inspected from temporary local captures and are not retained as repository artifacts. The package is unsigned and unnotarized by design.

## Portable evidence manifest

| Evidence | Procedure / command | Result | Scope and redaction |
| --- | --- | --- | --- |
| Lock identity | `tests/test_web.py::test_database_process_lock_resolves_database_aliases` | Canonical, symlink, and hard-link aliases cannot acquire a second lock | Disposable paths; no database contents retained |
| Runtime ownership | Lock probe for distinct data directories and selected database paths | Same database authority is rejected; same-directory backup lock is rejected | Lock primitives, not live dual-server scheduling |
| Process recovery | Disposable child-process termination probes for Campaigns and Data Studio | Campaign `started → ambiguous`; Data Studio `running → pending → completed` | Synthetic rows/artifacts only |
| Ambiguous effect | Campaign recovery assertion | No automatic retry after ambiguous external effect | No provider contacted |
| Backup/restore | Interrupted publication, concurrent destination, staged restore and integrity checks | No partial destination; one winner; restored artifact and incident persisted | Synthetic local archive only |
| Keychain | Native and packaged synthetic store/resolve/rotate/revoke cycle | Lifecycle passed | No real credentials; unsigned package |
| Health semantics | Source failure/recovery and active-job-without-heartbeat tests | Failures are action-required; recovery resolves; active durable work is `unknown` without heartbeat | No production telemetry |
| Maintenance UI | Playwright viewport assertions and Maintenance health/diagnostic/backup journey | Desktop, short viewport, tablet, mobile checks passed | Screenshots were temporary local captures |
