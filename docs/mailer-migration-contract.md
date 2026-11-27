# Legacy campaign migration contract

This is the only maintained guidance for translating explicitly supplied
legacy campaign files into the canonical package. The former MAILER runner and
its directory are removed. No `.env` is loaded, no source file is overwritten,
and no provider operation is performed.

## Boundary

The new SQLite database is authoritative for imported state. Caller-supplied
legacy files remain outside the repository and are never auto-imported. A
translation report redacts secret-looking values and identifies unsupported or
manual-review settings.

## Safe sequence

1. Snapshot and checksum the source files outside the repository.
2. Rehearse identity, duplicate, suppression, retention, and rollback behavior
   with synthetic data.
3. Import into a new database; never mutate the source files in place.
4. Run integrity and backup/restore checks and compare the resulting report.
5. Keep the source snapshot until the owner approves any ownership change.

## Commands

```text
mailforge campaign translate-legacy --config /path/to/config.yaml
mailforge campaign import-legacy --database /tmp/campaign.sqlite3 \
  --config /path/to/config.yaml --campaign invoice
mailforge campaign rehearse-legacy-state --database /tmp/campaign.sqlite3 \
  --config /path/to/config.yaml --campaign invoice
```

Historical sent records remain ambiguous, failed records remain deferred, and
hard-bounce records create suppressions. No historical record is treated as a
successful new send or retried automatically.
