# Operational assurance 032 final review, revision 6

**Verdict: BLOCKED.** Reviewed HEAD `aed1f6f570cf28e2e20c77948f33cc6d057d68a1` against `74d264b79901abda8b43ae613e5d5232c181e516`. The pre-existing untracked r5 review was the only working-tree change at start; this r6 artifact is added afterward. This reviewer did not implement the change and used a separate execution context; author model and cross-model independence are not authenticated. This review does not grant merge or business approval.

## Major: custom database can be owned by two server processes

`src/mailforge/interfaces/cli.py:1091-1096,1108-1115` permits independent `--database` and `--data-dir` arguments, but the new server lock is derived solely from the data directory. Two servers using the same database and different data directories can now acquire distinct locks and both open the same database, defeating the existing single-owner runtime boundary and risking duplicate scheduling or recovery. A disposable lock probe acquired both distinct directory locks simultaneously; actual dual-server operations were not run. The prior implementation locked the selected database. Preserve both database ownership and data-directory cleanup serialization, with consistent lock ordering and deduplication, or enforce a single canonical pairing.

## Follow-up evidence and limitations

- The r5 custom-database backup cleanup race is addressed by inspection: server startup and CLI backup now lock `DataDirectory.from_path(data_directory).database` before cleaning the shared backup directory. This correction creates the separate database-ownership regression above.
- Unknown storage evidence no longer resolves an existing incident, and the added test checks that behavior. Source-evidence recovery and unchanged acknowledged-state behavior remain as assessed in r5.
- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py`: **29 passed**, one Starlette/httpx deprecation warning, 3.81 seconds. No application code or tests were edited.
- The committed qualification and performance documents report executor-performed local synthetic Keychain, crash/restart, backup/restore, UI and performance observations. They were not independently rerun in this review; real provider quota, signed release, production credentials and scale remain outside their evidence. See r5 for the provenance limits and earlier findings.
