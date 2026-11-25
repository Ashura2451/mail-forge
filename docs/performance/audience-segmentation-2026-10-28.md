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
