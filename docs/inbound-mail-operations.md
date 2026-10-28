# Inbound mail operations

Mailforge's Inbound workspace is a local evidence workbench. It can import
EML/MBOX files and deterministic fixtures, acquire from a configured IMAP
account through the existing provider authority, and use the bounded POP3S
adapter when a local caller supplies a POP3 credential reference. Acquisition
is read-only: imports do not mark, move, delete, or send mailbox messages.

## Operator workflow

1. Import or synchronize a bounded source.
2. Search by sender, recipient, subject, body preview, source, and
   classification.
3. Open a message to inspect parsed metadata, attachment metadata,
   classification/correlation, review actions, rule runs, and draft provenance.
4. Extract selected headers/addresses and export JSON or formula-safe CSV.
5. Add an extracted address to a list only as an explicit operator action.
   Extraction is not consent evidence.
6. Prepare a reply or forward in the canonical Message Studio. Preparation
   creates a durable draft and preserves threading provenance; it does not send.

## Rules

Rules are versioned JSON with bounded conditions and allowlisted actions. The
initial conditions cover source/folder, sender/recipient, subject,
classification, and correlation. Actions can record a durable event, extract
metadata, create review work, or prepare a draft. Rules cannot execute code,
SQL, shell commands, network calls, or filesystem paths. Rule runs use a
stable `(rule, message, revision)` idempotency key.

Automatic-response preparation is loop-protected. Automatic replies,
delivery reports, complaint reports, null-sender signals, and repeated source
events do not generate automatic reply drafts. Real external automatic replies
remain separately qualified and authorized.

## Mailbox actions

Move, copy, and mark-read are represented as explicit durable requests with an
authorization reference and idempotency key. The local 020 implementation
records the request and audit event; acquisition never executes it. A provider
mutation adapter requires a later capability and ambiguity qualification.

## Retention and safety

The existing inbound raw-evidence retention flag remains authoritative. MIME
parsing is bounded and non-executing. HTML is reduced to a safe text preview;
the browser does not inject message HTML into the application DOM or fetch
remote resources. Attachment filenames are stored as metadata only and are
basename-normalized. No malware-scanning claim is made.

## Local commands

```text
mailforge inbound import-eml --database state/mailforge.sqlite3 --source-ref local --path message.eml
mailforge inbound import-mbox --database state/mailforge.sqlite3 --source-ref archive --path archive.mbox
mailforge inbound search --database state/mailforge.sqlite3 --query invoice
mailforge inbound extract --database state/mailforge.sqlite3 --message-id MESSAGE_ID
mailforge inbound export --database state/mailforge.sqlite3 --format csv
mailforge inbound rule list --database state/mailforge.sqlite3
mailforge inbound draft --database state/mailforge.sqlite3 --message-id MESSAGE_ID --kind reply
```

All examples operate on local SQLite and synthetic/disposable evidence. No
provider qualification or real-message delivery is implied.
