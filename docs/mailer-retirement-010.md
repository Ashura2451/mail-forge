# MAILER executable retirement

Task `MAILFORGE-MAILER-RETIREMENT-010` completes the local source migration
from the former `MAILER/main.py` runner into the canonical `src/mailforge`
package. The task is local-only: it does not read `MAILER/.env`, import the
existing MAILER lists/logs, connect to a provider, or send mail.

## Ownership and dispositions

| Legacy capability | Canonical owner | Disposition |
| --- | --- | --- |
| YAML and explicit environment references | `application.mailer_compat.LegacyConfigTranslator` | Migrated; no `.env` loading, secret-redacted report |
| TXT/CSV targets | `load_legacy_targets` → `CampaignService.import_candidates` | Migrated; normalized identity, source path/position, duplicate/rejection evidence |
| Campaign settings and worker lifecycle | `CampaignService`, `OutboundService`, `CampaignStore` | Migrated; one durable coordinator and state authority |
| Jinja/spintax/template rendering | `SafeTemplateRenderer`, `MessageComposer` | Migrated with bounded `target` compatibility context |
| MIME/header/message identity | `MessageComposer` and prepared-message records | Migrated; content hash and persisted MIME |
| SMTP connection/authentication/rotation | gated `SmtpTransport`, `SMTPConnectionPool` | Migrated as an adapter only; no live CLI/provider qualification |
| Rate/domain/day limits | `PolicyEngine`, `SenderPolicy`, durable reservations and campaign pacing | Migrated locally, including legacy hourly domain limits; provider quotas remain open |
| Retry/bounce classification | `CampaignService`, `migrate_legacy_state`, suppressions | Migrated; hard bounce suppresses, failure defers, sent history is ambiguous |
| Events/logs/state files | `CampaignStore` plus explicit evidence import | Migrated by rehearsal only; source files are never overwritten |
| Invisible text and client simulation | None | Deliberately not migrated: unsafe, non-essential behavior without a stable contract |
| Warmup/adaptive reputation | durable local warmup ramp, domain bounce cooldown, and sender failure-rate cooldown | Migrated as bounded local policy controls; provider qualification and deliverability evidence remain external |

## Operator sequence

1. Run `campaign translate-legacy` against a synthetic or reviewed config. The
   JSON report lists translated fields, changed defaults, deprecated behavior,
   manual review, unsupported sections, and validation failures. It never
   prints passwords.
2. Run `campaign import-legacy` with a new database and explicit campaign.
   This imports only the requested target file, creates a canonical list and
   campaign, and prepares MIME messages. It does not call SMTP.
3. Run `campaign rehearse-legacy-state` with explicit historical files when a
   snapshot has been approved. It maps sent/failed/hard-bounce records to
   ambiguous/deferred/permanent-failure states, adds hard-bounce suppression,
   records provenance, and preserves event/log evidence.
4. Run `campaign backup` and inspect the JSON report and database integrity
   before any future ownership decision. Keep source snapshots for rollback.

All import operations are additive and idempotent for identical records. A
second rehearsal reports `already_present` rather than adding another attempt;
conflicting records choose the fail-safe precedence hard-bounce > sent/ambiguous
> failed/deferred and report `conflicts`; ambiguous records are not eligible for
automatic retry. A failed rehearsal is
discarded by deleting only the new operator-selected database, never by
rewriting the source data.

## Validation evidence

- Synthetic migration, template preparation, event idempotency, duplicate and
  invalid target handling, suppression, secret redaction, pool reuse, sender
  policy, durable pacing, two-gate SMTP execution, backup/restore, inbound
  evidence, and repeat-execution tests are in
  `tests/test_mailer_retirement.py` and
  `tests/test_smtp.py`.
- `MAILER/` contains no Python source after retirement; preserved files are
  configuration, templates, data, logs, and `.env.example` only.
- The canonical CLI owns the migration commands; no package module imports
  `MAILER`, `MSORTER`, or `SORTER`.
- Live provider qualification, consent/retention policy, and production
  release remain explicit follow-up decisions rather than hidden behavior.
