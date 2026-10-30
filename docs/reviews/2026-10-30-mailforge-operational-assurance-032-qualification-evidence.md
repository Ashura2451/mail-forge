# Operational assurance 032 qualification evidence

Date: 2026-10-30
Scope: local synthetic qualification on branch `feat/mailforge-operational-assurance-032`.

Latest source qualification revision: `33c7529`.

## Commands and results

- `PYTHONPATH=src .venv/bin/pytest -q --ignore=tests/e2e`: 273 passed, one existing Starlette/httpx deprecation warning.
- `PYTHONPATH=src .venv/bin/pytest -q tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py`: 30 passed, one existing Starlette/httpx deprecation warning.
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

## UI and package observations

- The Maintenance workbench was checked at desktop, short desktop, tablet, and mobile viewports. Health-check and diagnostic-export controls were visible; the responsive rail/drawer remained usable without observed clipping.
- Bundled direct checks confirmed cheap liveness does not perform SQLite integrity work, detailed status reports integrity, and packaged Keychain store/resolve/rotate/revoke completed with synthetic identifiers.

## Limits

This is local evidence only. It does not qualify real providers, mailbox access, production credentials, signing/notarization, release promotion, production scale, or provider quota. Screenshots were inspected from temporary local captures and are not retained as repository artifacts. The package is unsigned and unnotarized by design.
