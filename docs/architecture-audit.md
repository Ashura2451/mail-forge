# Mailforge current architecture

## Product boundary

Mailforge is a local-first email utility with three maintained workflows:
Sort email, Compare lists, and Send. Accounts, saved lists, history, settings,
backup, and recovery support those workflows. No mailbox client, automation
engine, external data workbench, reporting product, programmable extension
runtime, or standalone standards workspace remains in the active product.

## Maintained ownership

- `src/mailforge/domain/` contains address, policy, campaign, provider, and
  outbound vocabulary.
- `src/mailforge/application/` contains ingestion, sorting, comparison,
  campaign, message, provider, policy, migration, and recovery workflows.
- `src/mailforge/infrastructure/campaign_store.py` is the sole SQLite
  authority. Historical schema migration SQL remains only to open and preserve
  existing databases; retired tables are not created for a new database.
- `src/mailforge/interfaces/web.py` is the loopback FastAPI boundary and serves
  compiled assets. `interfaces/cli.py` is the explicit command boundary.
- `web/src/` is the React operator console. Business rules remain in Python.
- `src/mailforge/resources/sorting/` contains the maintained provider and
  disposable-domain classification datasets.

## Data and runtime boundary

Mutable databases, backups, logs, runtime locks, list-processing artifacts,
sort outputs, and checkpoints belong under the platform Mailforge data
directory. The source checkout contains only maintained code, tests, resources,
documentation, development tooling, and the current local package deliverable.
Private governance is kept in `.aios/`, `.orchestra/`, and `.agents/`; private
qualification evidence is stored outside the source checkout.

## Compatibility

Legacy migration translation is intentionally narrow and explicit. It preserves
the ability to rehearse safe import of old campaign state without retaining
the old runners, routes, directories, or configuration tree. Existing SQLite
records and schema migration history remain readable/preserved; no retired
application service is registered for new work.
