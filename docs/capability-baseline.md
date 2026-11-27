# Mailforge capability baseline

This is the concise current capability register. Superseded implementation
records are intentionally removed from the maintained documentation tree;
repository history retains their provenance.

| Capability | Current status | Canonical owner | Boundary |
| --- | --- | --- | --- |
| Sort email | Implemented and qualified | `application/sorting.py`, `list_processing.py`, `domain/email.py` | Provider/domain classification, validation, dedupe, and export are local; DNS is optional and offline by default. |
| Compare lists | Implemented and qualified | `application/list_comparison.py` | Read-only file, saved-list, and selected successful-history comparisons; no mutation. |
| Send and message preparation | Implemented and qualified | `campaign_operations.py`, `message_studio.py`, `outbound.py` | Pasted/file recipients, plain text, HTML, personalization, immutable preparation, policy checks, and controlled execution. |
| SMTP provider boundary | Implemented and loopback-qualified | `provider_operations.py`, `providers.py`, `infrastructure/smtp.py` | SMTP only; credentials are runtime references; external access remains explicitly gated. |
| Recipient policy and recovery | Implemented locally | `campaigns.py`, `eligibility.py`, `policy.py`, `local_application.py` | SQLite is authoritative; backup, restore, lifecycle, suppression, and migration boundaries remain active. |
| Legacy migration | Narrow compatibility | `mailer_compat.py`, `legacy_migration.py`, schema migration SQL | Explicit caller-supplied inputs only; retired runners and legacy directories are absent. |

Historical SQLite tables may remain in existing user databases so records can
be preserved and migrated safely. They are not created for new databases and
have no active browser, CLI, worker, or provider authority.
