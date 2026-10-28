# Mailforge Data Connections

Data Connections is the local, read-only bridge from a bounded external data
source into Data Studio. It supports SQLite files, PostgreSQL profiles, and
normal `.xlsx` workbooks. Mailforge never writes to a connected source and
does not accept arbitrary SQL.

## Quick start

Use the browser’s **Data Connections** workspace or the CLI:

```bash
mailforge data-connections create \
  --name "Local contacts" \
  --type sqlite \
  --source ./contacts.sqlite3
mailforge data-connections schema PROFILE_ID
mailforge data-connections preview PROFILE_ID --selection '{"table":"contacts"}'
mailforge data-connections snapshot PROFILE_ID \
  --selection '{"table":"contacts"}' \
  --idempotency-key contacts-refresh-1 \
  --wait
```

PostgreSQL profiles require `host`, `database`, and `user` configuration plus
a symbolic credential reference such as `env:MAILFORGE_POSTGRES_PASSWORD`.
Credential values are resolved only at runtime and are never stored in the
profile or returned by the API.

## Selection and safety

Selections name a table/view or workbook worksheet, optional columns, typed
filters, and optional ordering. Preview is capped at 500 rows; a published
snapshot is capped at 100,000 rows. SQLite and PostgreSQL reads use bounded
parameterized queries. PostgreSQL connections default to TLS verification and
read-only transactions. XLSX workbooks are opened in read-only mode without
macro execution; formulas use cached values when available and otherwise emit
conversion evidence.

## Snapshots and Data Studio

Snapshots are durable, revisioned, immutable JSONL artifacts under the local
application data directory. A snapshot is published only after the complete
bounded read succeeds, its SHA-256 is recorded, and its provenance is stored.
Interrupted or canceled work never becomes a usable artifact. Repeating an
idempotency key returns the same snapshot; a new refresh creates a new revision.

Completed snapshots can be handed to Data Studio for the existing recipe,
preview, contact-import, message, campaign, or workflow authorities. The
connection layer does not duplicate those downstream rules.

No real provider, credential, mailbox, recipient, or private dataset is needed
for local qualification. A real PostgreSQL deployment should be qualified
separately with controlled credentials and a disposable database.
