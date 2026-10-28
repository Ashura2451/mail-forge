# Legacy source retirement and naming policy

Task `MAILFORGE-LEGACY-RETIREMENT-009` retires source only where the canonical
package has verified ownership. It does not delete operational data, migrate
real recipients, or authorize mail delivery.

## Disposition

- `SORTER/*.py`: retired. `mailforge transform format|dedupe|list|count` owns
  formatting, first-occurrence dedupe, historical list output, and safe list
  counting. Existing staged files remain untouched.
- `MSORTER/*.py`: retired. `mailforge sort`, `mailforge summary`,
  `mailforge clean`, `mailforge interactive`, `mailforge extract-jsonl`, and
  `mailforge refresh-disposable` own the verified capabilities. Existing
  configs, checkpoints, logs, results, and input data remain untouched.
- `MAILER/main.py`: retired by task 010 after synthetic config, target,
  template, state, SMTP-pool, and security evidence. MAILER YAML, templates,
  lists, logs, events, and `.env.example` remain preserved resources; see
  `docs/mailer-retirement-010.md` for the explicit mapping and rollback rules.

## Naming policy

New runtime source uses descriptive snake_case module names under
`src/mailforge/` and capability names owned by the canonical layer. Legacy
class names such as `FileEngine`, `EmailValidator`, `ProviderEngine`, and
`DNSEngine` are not compatibility APIs; the canonical sorting implementation
uses `InputReader`, `AddressValidator`, `ProviderCatalog`, and `DNSService`.
`Checkpoint` remains because it is the application resume boundary, not a
legacy import shim.

## Command migration

| Historical source | Canonical command |
| --- | --- |
| `MSORTER/main.py sort` | `mailforge sort INPUT...` |
| `MSORTER/main.py summary` | `mailforge summary` |
| `MSORTER/main.py clean` | `mailforge clean` |
| `MSORTER/main.py interactive` | `mailforge interactive` |
| `MSORTER/extract_emails.py` | `mailforge extract-jsonl INPUT OUTPUT` |
| `MSORTER/disposable.py` | `mailforge refresh-disposable` |
| SORTER formatting script | `mailforge transform format INPUT OUTPUT` |
| SORTER duplicate script | `mailforge transform dedupe FILE` |
| SORTER list script | `mailforge transform list INPUT OUTPUT` |
| SORTER count script | `mailforge transform count FILE` |

For an existing MSORTER data layout, pass preserved paths explicitly rather
than relying on the repository-root defaults:

```bash
mailforge sort INPUT.txt \
  --config-dir MSORTER/config \
  --results-dir MSORTER/results \
  --checkpoint MSORTER/checkpoint.json \
  --dns-mode offline
mailforge summary --results-dir MSORTER/results
mailforge refresh-disposable \
  --path MSORTER/config/disposable.json \
  --url https://example.invalid/synthetic-list.json
```

Review the checkpoint and results before `--resume`; the canonical checkpoint
fingerprint rejects changed inputs or configuration. The `MSORTER/config`,
`MSORTER/checkpoint*.json`, `MSORTER/logs`, and existing input/result files are
preserved data, not automatically rewritten or migrated by this task.
If cleanup is later required, back up and review the results first, then run
`mailforge clean --results-dir MSORTER/results` interactively; do not use
`--yes` as part of a preservation or migration sequence.

No command in this table reads `MAILER/.env`, opens a live mailbox, or sends
real mail as part of validation.
