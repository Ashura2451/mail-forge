# Review: durable inbound email operations

**Task:** `MAILFORGE-INBOUND-OPERATIONS-007`
**Date:** 2026-10-27
**Scope:** local-first inbound acquisition, bounded RFC 5322/MIME evidence,
correlation/trust/action policy, SQLite schema 3, recovery, CLI, tests, and
documentation.

## Independent review outcome

The independent read-only reviewer initially identified and reproduced several
issues: post-parse failures were marked processed too early; parser/source
limits were incomplete; complaint targeting could fall back to the reporter;
source-identity mutation was silently deduplicated; synthetic trust was
implicit; replay could reopen settled reviews and duplicate correlations; and
DSN/ARF helper traversal could exceed bounds.

Those findings were fixed and regression-tested. The reviewer’s final
follow-up approved the scoped change with no remaining findings. The reviewer
did not read `MAILER/.env`, use network access, or edit repository files.

## Evidence

- Full suite: 92 passed.
- Focused inbound suite: 12 passed, including post-parse restart recovery,
  content mutation, explicit synthetic trust, bounded MIME/ARF handling,
  DSN recipient preservation, replay, and backup/restore.
- Ruff lint and format checks passed.
- Locked sync, build, secret guard, CLI smoke, and staged whitespace checks
  passed locally.

## Remaining boundaries

This approval is for synthetic/local behavior only. It does not qualify a real
IMAP provider, mailbox acknowledgment, sender authentication, production
retention policy, inbox delivery, automatic replies, SMTP delivery, or real
recipient migration.
