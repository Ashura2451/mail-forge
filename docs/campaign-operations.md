# Campaign operations

Campaign Operations is the shared authority behind the Send journey. It owns
draft CAS updates, list-backed recipient selection, policy preflight, message
preparation, schedule state, authorization, worker control, per-recipient
evidence, exact-scope external authorization, and bounded exports. It is not a
second persistence or sending engine.

## Boundaries

- Direct sending accepts pasted addresses or a supported file and creates
  private local list/message records automatically.
- Published message revisions and prepared recipient identities are immutable
  for a run.
- Simulation and controlled loopback SMTP require explicit authorization.
- External authorization is a server-issued, expiring, exact-scope grant; the
  browser review does not contact an external provider.
- Ambiguous outcomes are durable and are never retried blindly.
- Credentials stay behind the runtime credential resolver.

## Surfaces

The browser and CLI use the same application services for campaign creation,
preparation, authorization, execution, pause/resume, history, recipients,
attempts, and export. The active browser route is Send; historical campaign
URLs are not navigation aliases.

## Qualification

Synthetic regression, rendered browser acceptance, controlled loopback SMTP,
restart persistence, duplicate-send prevention, backup/restore, and outbound
proof checks are maintained in the current test suite and owner guide. SMTP
acceptance is not inbox delivery or production-provider readiness.
