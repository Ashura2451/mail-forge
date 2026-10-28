# Audience segmentation 015

This document records the shipped local operator contract for
`MAILFORGE-AUDIENCE-SEGMENTATION-015`. It is synthetic/local evidence, not
provider qualification, consent evidence, inbox-delivery evidence, or hosted
service approval.

## Canonical behavior

- `SegmentationService` owns typed fields, structured import validation and
  reconciliation, static-list membership history, segment AST validation and
  compilation, previews, revisions, and campaign audience snapshots.
- `NUMBER` values are signed 64-bit integers scaled by 1,000,000. Decimal
  parsing rejects nonfinite values, overflow, more than six fractional digits,
  more than twelve integer digits, and silent rounding. Comparisons remain
  integer comparisons.
- A missing attribute evaluates as `UNKNOWN`; it does not match ordinary
  predicates. `NOT` and boolean combinations retain SQL three-valued behavior.
- A prepared campaign records its exact list/segment source and segment
  revision, rechecks mutable eligibility, and persists the selected recipients.
  Re-preparing the same source/revision is idempotent. A different source or
  revision for an existing run is an explicit conflict.
- Every recurring `run_key` gets an independent snapshot and recipient set.

## Interface contract

HTTP routes are under `/api/v1` and use the existing local session and CSRF
boundary:

- `GET/POST/PATCH /fields`, plus `/fields/{id}/archive`;
- `POST /imports/structured/preview` and `/imports/structured`;
- `GET/POST/PATCH /segments`, `/segments/{id}`, and `/segments/{id}/preview`;
- `POST/DELETE /lists/{list_id}/members/{contact_id}` and list history;
- campaign creation accepts exactly one list or segment source;
- `POST /campaigns/{id}/prepare` accepts `run_key`, and
  `GET /campaigns/{id}/audience?run_key=...` returns the durable snapshot.

CLI support is available through `mailforge field`, `mailforge segment`, and
`mailforge campaign create/prepare`; structured rows and mappings are JSON
files for bounded local operation. The React console provides a Segments route
with field selection, rule editing, saved segment creation, and preview. It
does not contain a second evaluator.

## Performance evidence

One local CPython 3.15.2 run against temporary SQLite databases measured:

| Synthetic contacts | Matched | Preview time | Query plan observation |
| ---: | ---: | ---: | --- |
| 1,000 | 500 | 2.32 ms | contact scan plus indexed `(contact_id, field_id)` attribute lookups |
| 10,000 | 5,000 | 9.46 ms | same bounded correlated lookup plan |

These values are reproducible engineering evidence, not production SLOs or a
claim about real-data scale. Larger qualification remains open.
