# Mailforge product capability atlas

## Current product

Mailforge is a local email utility, not a hosted marketing, CRM, mailbox,
automation, integration, or analytics platform.

| Workflow | Outcome | Evidence |
| --- | --- | --- |
| Sort email | Classify providers/domains, validate, deduplicate, and export useful list artifacts. | Sorter regression, large-file workload, rendered browser checks. |
| Compare lists | Compare files, saved lists, and selected successful submissions without mutation. | Comparison regression, large-file workload, rendered browser checks. |
| Send | Prepare pasted/file recipients, HTML or text, optional personalization, controlled simulation, and loopback SMTP. | Campaign/message/provider regression, browser acceptance, packaged qualification. |

## Shared architecture

`CampaignStore` remains the single SQLite authority. Python application
services own ingestion, sorting, comparison, policy, campaigns, content,
provider safety, SMTP transport, migration, backup, and recovery. FastAPI is a
loopback-only boundary; the React console is a client of that boundary.

Typed recipient/list compatibility remains where it supports existing durable
campaigns. It is not a separate operator product.

## Retired scope

The following are removed from active source, routes, CLI, workers, tests,
resources, and maintained documentation: Data Studio, external data
connections, automation, Integration Hub, programmable extensions, email
standards workspace, reporting workspace, inbound/mailbox operations, IMAP
provider operations, and the former MAILER/SORTER/MSORTER runners.

Existing database tables and migration SQL for those areas may remain only as
read/preservation compatibility for old user databases. Fresh databases do
not create retired tables. No new implementation may depend on them.

## Owner and release boundaries

No real-recipient migration, external SMTP operation, inbox-delivery claim,
hosted identity, signed distribution, or production capacity claim is implied.
The practical acceptance procedure is
[`MAILFORGE-045-OWNER-ACCEPTANCE.md`](../MAILFORGE-045-OWNER-ACCEPTANCE.md);
the completion evidence is
[`MAILFORGE-044-COMPLETION.md`](../MAILFORGE-044-COMPLETION.md).
