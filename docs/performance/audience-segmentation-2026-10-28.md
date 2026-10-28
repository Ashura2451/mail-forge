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
