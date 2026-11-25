# Audience segmentation performance evidence — 2026-10-28

The benchmark used a temporary SQLite database, one registered NUMBER field,
synthetic contacts, and a segment predicate `score >= N/2`. The service
preview was limited to the first 100 rows while counting all matches.

| Contacts | Matches | Preview elapsed |
| ---: | ---: | ---: |
| 1,000 | 500 | 2.32 ms |
| 10,000 | 5,000 | 9.46 ms |

`EXPLAIN QUERY PLAN` showed a normalized-email contact index scan and indexed
attribute lookups by `(contact_id, field_id)`. The run was local and synthetic
under CPython 3.15.2; it is not a production SLO, provider claim, or
real-recipient qualification.

## MAILFORGE-036 follow-up measurement — 2026-11-25

The audience evaluator was exercised against 100,000 synthetic contacts and
typed NUMBER values in a temporary SQLite database. The preview remained
bounded to 100 returned rows while counting all matches.

| Contacts | Matches | Returned | Preview elapsed | Database size |
| ---: | ---: | ---: | ---: | ---: |
| 100,000 | 50,000 | 100 | 0.2089 s | ~92.6 MiB |

This is a local engineering measurement for the current schema and predicate;
it does not establish a production capacity target or cover concurrent imports,
browser rendering of 100,000 rows, or real recipient data.

## MAILFORGE-036 connected workflow qualification — 2026-11-25

The newly introduced audience workflows were exercised through the local HTTP
application boundary against a temporary SQLite database. The workload used
10,000 valid synthetic contacts, a 2,000-row structured reconciliation, a
100-contact bounded policy operation, and a 10,000-contact list readiness
evaluation. The import and reconciliation paths recorded durable batch ledger
rows; reversal was then executed against the reconciliation batch.

| Operation | Rows/contacts | Elapsed | Result |
| --- | ---: | ---: | --- |
| Import preview | 10,000 | 0.2649 s | 10,000 accepted |
| Direct import + ledger | 10,000 | 79.9342 s | 10,000 unique contacts |
| Structured import preview | 2,000 | 3.9283 s | 2,000 records |
| Structured reconciliation + ledger | 2,000 | 17.7906 s | 2,000 values filled |
| Contact search/filter | 1 exact match | 0.0092 s | 1 result |
| Bounded bulk preference operation | 100 | 0.3863 s | 100 requested |
| Audience readiness | 10,000 | 29.5970 s | 9,900 unknown, 100 excluded |
| Compensating reversal | 2,000 | 0.0742 s | fully reversed |

The SQLite file measured 13,815,808 bytes after the 10,000-row import,
16,809,984 bytes after reconciliation, and 17,219,584 bytes after reversal.
All correctness checks matched expected counts. This is a single local,
serial synthetic run: it is not a production capacity/SLO claim, does not
measure concurrent operators, and does not qualify real-provider or real-
recipient behavior.
