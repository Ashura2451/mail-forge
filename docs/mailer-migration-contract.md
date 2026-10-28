# MAILER migration contract

This contract defines the local compatibility boundary between the preserved
`MAILER/` resources and the canonical package. The former `MAILER/main.py`
runner is retired. The commands below operate only on explicitly supplied
paths; no `.env` is loaded and no real MAILER data is imported by repository
validation.

## Authority boundary

The SQLite core is authoritative for newly created synthetic campaigns and
their contacts, suppressions, delivery intents, attempts, transitions, and
operational events. The legacy files remain authoritative for existing MAILER
runs until a separately approved migration selects a source snapshot and
rollback point. Neither authority silently overwrites the other.

## Mapping contract

| Legacy input | Core destination | State mapping | Required evidence |
| --- | --- | --- | --- |
| TXT/CSV target records | `contacts`, `contact_sources`, `list_memberships` | New contacts are `eligible_pending` only after campaign preparation | Normalized identity, source path, record position, and import count. |
| `sent.txt` | `delivery_attempts`, `delivery_intents` | `LEGACY_RECORDED` → `ambiguous` | Historical evidence is retained; no automatic resend. |
| `failed.txt` | `delivery_attempts`, `delivery_intents` | `TRANSIENT_FAILURE` → `deferred` | Retry is opt-in and never the default. |
| `hard_bounces.txt` | `delivery_attempts`, `delivery_intents`, `suppressions` | `PERMANENT_FAILURE` → `permanent_failure` plus hard-bounce suppression | Suppression scope and reason are durable. |
| `events.json`, logs, HTML ledger | Compatibility evidence | No delivery-state inference without an explicit parser and review | Source checksum, parser version, and reconciliation report required in a future migration. |
| YAML/templates/environment | `LegacyConfiguration`, typed campaign/template/sender/schedule records | No secret values in SQLite or reports | `translate-legacy` report plus explicit operator-supplied environment mapping. |

## Required migration sequence

1. Freeze or snapshot the legacy source files and record their checksums.
2. Validate the proposed identity, duplicate, consent, retention, and
   suppression policy against a synthetic fixture and a reviewed sample.
3. Import contacts and provenance into a new database, never in place.
4. Reconcile historical sent/failed/bounce evidence using the mapping above.
5. Run integrity checks, backup/restore checks, and a report comparison.
6. Keep the legacy snapshot available for rollback; only then can ownership of
   a selected campaign be changed by an explicit operator decision.

The canonical rehearsal implements validation, additive import, integrity
checks, and backup/restore with synthetic data. `import-legacy` creates a new
SQLite campaign and prepares messages; `rehearse-legacy-state` imports explicit
historical files idempotently. These commands do not freeze, copy, or mutate
the source files.

## Canonical commands

```text
mailforge campaign translate-legacy --config /path/to/config.yaml
mailforge campaign import-legacy --database /tmp/campaign.sqlite3 \
  --config /path/to/config.yaml --campaign invoice --sender-email sender@example.com
mailforge campaign rehearse-legacy-state --database /tmp/campaign.sqlite3 \
  --list-id LIST --campaign-id CAMPAIGN --sent sent.txt --failed failed.txt \
  --hard-bounces hard_bounces.txt --events events.json --log email_campaign.log
```

The report distinguishes translated fields, changed defaults, deprecated
behavior, manual-review items, invalid records, duplicates, already-present
records, and preserved log evidence. A second state rehearsal is a no-op for
identical attempts/events. Historical `sent.txt` entries remain `ambiguous`
and are never automatically retried.

## Recovery and safety rules

- `reserved` work can be returned to `eligible_pending` after a process crash.
- Work that reached `attempt_started` but lacks a durable final result becomes
  `ambiguous` and is never blindly retried.
- A suppression added after preparation is rechecked before reservation and
  wins over an otherwise eligible intent.
- Durable state rejects keys that look like passwords, tokens, API keys,
  private keys, or credentials.
- SQLite backup and restore are explicit operations and are integrity-checked.
- SMTP is available only through the canonical explicit-gate adapter and
  connection pool. `CampaignService` remains the sole delivery coordinator;
  the compatibility layer never creates a second worker engine. No live-send
  CLI or provider credential loading is enabled.
