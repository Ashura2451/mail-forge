# MAILFORGE-LOCAL-PYTHON-SORTING-003 review

Date: 2026-10-27
Branch: `feat/mailforge-local-python-sorting-003`
Status: ready for local handoff; publication remains blocked by the tracked
`MAILER/.env` policy item.

## Scope

- Normal login-shell `python`, `python3`, and `python3.15` now resolve to
  uv-managed CPython 3.15.2 on the arm64 Mac. `uv` is 0.13.6.
- The unified sorter adds deterministic multi-source processing, structured
  explainable JSONL, quarantine output, profiles, dry-run, progress reporting,
  formula-safe CSV output, source/configuration fingerprints, and recoverable
  disposable-data provenance.
- Existing MSORTER and SORTER adapters, legacy output columns, and the live-send
  safety boundary remain in place.

## Verification

- Fresh login shell: `python` and `python3` report 3.15.2 with no active
  virtualenv; project commands use the locked uv environment.
- `uv lock --check`, locked sync, `47 passed`, Ruff check, Ruff format check,
  source/wheel build, and artifact audit passed.
- CLI help checks passed for Mailforge, module entrypoint, MSORTER, and MAILER.
- Synthetic multi-source dry-run passed with structured and quarantine paths;
  default MAILER invocation exited 2 with live-send refusal.
- Synthetic performance evidence is in
  `docs/performance/sorting-2026-10-27.md`. Enhanced processing traded wall
  time for explainability and durable output while using less peak memory in
  the measured workload.
- No live DNS, HTTP refresh, SMTP, or recipient campaign was exercised.

## Independent review disposition

An independent staged-diff review identified checkpoint flushing, fingerprint
validation, structured-only dedupe recovery, source-record provenance, and
disposable sidecar rollback/redaction gaps. These were fixed and covered by
regression tests:

- `test_structured_resume_restores_dedupe_and_output_boundary`
- `test_configuration_change_is_not_resumed_silently`
- `test_single_source_provenance_uses_record_position`
- `test_disposable_refresh_redacts_url_and_rolls_back_sidecar_failure`

The corrected tree passes the complete verification gates above. The reviewer’s
follow-up message was unavailable before handoff; no unresolved finding is
being represented as independently re-approved.

## Local Python rollback

Backups are outside the repository:

- `~/.zshrc.mailforge-python-backup-20261027`
- `~/.zprofile.mailforge-python-backup-20261027`

Restore those files carefully, then run `exec zsh -l` to revert the task’s PATH
changes while preserving the existing interpreter installations.

## Publication boundary

No push was performed. The tracked `MAILER/.env` was not read, modified, or
staged; its unresolved exposure remains an owner-led remediation item before
publication.
