# MAILFORGE-044 · Completion report

Status: completed on canonical `main` after local qualification.

## Product and architecture outcome

Mailforge is now a focused local email utility with three independent tools:

- Sort email: bounded TXT/CSV/JSON/JSONL ingestion, validation, duplicate
  removal, provider/domain grouping, and flexible artifact export.
- Compare lists: read-only file, saved-list, and selected successful-history
  comparison with subtraction, intersection, union, duplicate, and CSV export.
- Send: pasted addresses or recipient files, optional HTML and personalization,
  immutable message preparation, local simulation, controlled loopback SMTP,
  scheduling controls, per-recipient evidence, and restart-safe history.

Shared authorities remain in the FastAPI/SQLite application services: address
normalization and ingestion, sorting, list comparison, campaigns and policy,
message composition, outbound preparation, provider safety, SMTP transport,
credentials, migration, backup, and operational assurance. Segmentation remains
only where it is a shared recipient/list compatibility authority; its former
standalone product surface is retired.

## Removed implementation

The retired standalone systems and their dedicated tests were removed after
route, import, persistence, and compatibility inspection:

- automation and programmable operations;
- external data connections and Data Studio;
- email standards and reporting services;
- inbound acquisition/parsing/operations and mailbox operations;
- extension runner and obsolete operational acceptance suites;
- IMAP interoperability coverage and obsolete packaged browser coverage.

The CLI and web boundary were reduced to supported workflows and recovery
operations. `openpyxl` and `psycopg[binary]` were removed from the locked
runtime because no retained source path imports them.

Historical SQLite tables, schema migrations, legacy MAILER/SORTER data, and
bounded delivery-event compatibility records remain preserved where deleting
them would risk user-data loss or make backup/restore/migration unsafe. They are
not active product routes or new implementation authorities. The retired
mailbox API returns an explicit 410 response.

## Qualification evidence

- Python regression: 246 passed, 1 intentional packaged-test skip, 1 existing
  Starlette/httpx deprecation warning.
- Large workload qualification: 13,000 sorter records with 1,000 duplicates;
  20,000 comparison records against a 10,000-record reference. Both passed.
- Frontend: Node 24 build and oxlint passed; compiled assets were regenerated.
- Rendered browser: Sort, Compare, Send/HTML, History, narrow navigation, and
  axe accessibility checks passed locally.
- Packaged qualification: the replacement packaged test exercises a fresh
  capture-only TLS SMTP fixture, real controlled submission, recorded accepted
  outcome, restart persistence, and duplicate-send prevention. It also keeps
  external authorization review exact-scope and no-connect by default.
- Migration, backup/restore, provider safety, credentials, transport proof,
  and package identity checks are covered by the retained focused regression
  suites and the package build process.

## Genuine limitations

The package is an unsigned/notarized macOS arm64 local artifact. External SMTP,
inbox delivery, hosted identity, production capacity, cross-platform secret
stores, and real-recipient migration remain outside this completion. The local
capture fixture is synthetic and bounded; SMTP acceptance is not inbox delivery.

The owner acceptance steps are in
[`MAILFORGE-044-OWNER-ACCEPTANCE.md`](./MAILFORGE-044-OWNER-ACCEPTANCE.md).
