# Mailforge capability baseline

This is an evidence register, not a parity claim. Status reflects repository
inspection and synthetic checks through audience segmentation task
`MAILFORGE-AUDIENCE-SEGMENTATION-015`. The detailed product claim ledger
is [`docs/product/capability-atlas.md`](product/capability-atlas.md); this
historical baseline remains for continuity.

| Capability | Status | Current evidence | Intended behavior / acceptance direction | Source |
| --- | --- | --- | --- | --- |
| Contacts and lists | Implemented locally / synthetic | Canonical SQLite contacts, lists, source provenance, duplicate-safe import, correction, export, and deactivation | Real-data ownership and migration approval remain external | `CampaignStore`, policy-authority tests |
| Campaign composition | Existing but unverified | MAILER has campaign config and file templates | Typed campaign model with safe preview and approval boundary | MAILER sources; packet |
| Personalization and templates | Existing but unverified | Jinja/spintax rendering exists; synthetic template test passes | Deterministic preview, bounded rendering, escaping and template diagnostics | MAILER sources; test suite |
| Sending | Partial | MAILER has SMTP delivery logic; foundation blocks ordinary and future-authorized legacy execution | Phase-aware authorization, outcome classification, reconciliation, no delivery guarantee claims | MAILER sources; SMTP boundary tests |
| Incoming mail acquisition | Implemented locally / simulated provider | EML, MBOX, deterministic synthetic, and fake IMAP adapters; durable source identity, runs, cursors, replay, and interruption recovery are tested | Qualify a real provider adapter with authentication, mailbox acknowledgment, UID policy, and operational retention | `tests/test_inbound_operations.py`; no network |
| Incoming RFC5322/MIME parsing | Implemented and tested | Bounded stdlib parser, encoded headers/charsets, safe text/HTML previews, DSN/ARF evidence, attachment non-extraction, malformed/oversize quarantine | Broaden corpus and operational limits with provider samples | inbound parser tests; RFC references in spec 0003 |
| Inbound interpretation/correlation | Implemented synthetically | Human/automatic/DSN/complaint/unsubscribe/unknown classification; exact/probable/ambiguous/unmatched correlation; trust and review separation | Authentication/DKIM/SPF/DMARC and provider-specific trust remain pending | inbound service tests |
| Inbound actions and suppression integration | Implemented for trusted synthetic exact evidence | Idempotent reviewable actions; hard bounce/complaint/unsubscribe suppression uses existing SQLite authority; temporary DSNs do not suppress | Operator policy, retention, and real mailbox acknowledgment remain pending | inbound service/store tests |
| Scheduling and automation | Existing but unverified | MAILER loops daily and has sending-window settings | Durable schedule, pause/resume, cancellation, recovery, and audit evidence | MAILER sources |
| Database integration | Implemented locally / synthetic | CampaignStore schema 8 provides SQLite contacts, typed attributes, lists, immutable segment revisions, audience snapshots, policy evidence/preferences/declarations, lifecycle plans, campaigns, outbound, inbound, provider profiles/diagnostics, visual workflow revisions/runs, events, transactional pacing, backup/restore, and migration rehearsal | SQLite is authoritative only for explicitly supplied modern databases; real-data migration requires approval | Campaign, segmentation, provider, policy, outbound, inbound, workflow, pacing, and migration tests; specs 0001–0007 |
| Audience segmentation | Implemented locally / synthetic | Typed custom fields, bounded structured import, static membership history, parameterized nested AST evaluation, previews, immutable revisions, eligibility recheck, and recurring snapshots | No real recipient/provider data or external readiness is claimed | `tests/test_audience_segmentation.py`, web API acceptance, fixed-point boundaries, and local benchmark evidence |
| Reporting and diagnostics | Implemented locally / synthetic | Campaign reports plus secret-safe provider profile, credential-readiness, and loopback protocol diagnostics | External provider health, quota, inbox, and monitoring claims remain pending | Provider interoperability tests and canonical CLI |
| Desktop usability | Missing | No desktop UI or runtime found | Shared application services behind a future desktop interface | Repository inspection |
| Data management | Partial | Checkpoints and flat state files exist; recovery contract is undocumented | Backup, restore, migration, retention, and tested rollback | Existing files; data-integrity review |
| Security and recovery | Partial | Secrets use `.env` references; unsafe `eval` removed and SMTP is fail-closed | Secret-safe logs, import safety, authorization, recovery, and dependency review | Source inspection; tests |

Competitor references supplied in the execution packet are domain context only;
they were not used to declare parity. Any capability that conflicts with the
approved user-provided SMTP model requires an explicit Business Owner decision.
