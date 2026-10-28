# Mailforge Operational Intelligence

Operational Intelligence is a read-only reporting surface over the existing
Mailforge SQLite authority. It does not create a second analytics database,
publish telemetry, contact providers, or infer inbox delivery from SMTP
acceptance.

## Evidence model

Every response includes the metric-definition version, selected range,
generation timestamp, filters, evidence class, and limitations. The registry
in `src/mailforge/application/reporting.py` records the source tables, time
field, grain, numerator/denominator, filters, and missing-data behavior for
each metric.

The most important distinctions are:

- recipient intents are not delivery attempts;
- an accepted transport submission is not inbox delivery;
- ambiguous attempts remain unresolved until explicit reconciliation;
- untrusted inbound messages do not count as confirmed delivery failure;
- missing source facts are reported as not measured rather than estimated.

## Surfaces

The browser Reports & Insights route provides range/timezone controls, summary
cards, deterministic evidence-linked insights, drilldown tables, and bounded
CSV links. The API exposes the same service at:

```text
GET /api/v1/reports/definitions
GET /api/v1/reports/summary
GET /api/v1/reports/campaigns
GET /api/v1/reports/campaigns/{campaign_id}
GET /api/v1/reports/providers
GET /api/v1/reports/workflows
GET /api/v1/reports/inbound
GET /api/v1/reports/jobs
GET /api/v1/reports/export
```

All report ranges are converted from the requested IANA timezone to UTC before
parameterized SQL executes. Presets include today, yesterday, last 7 days,
last 30 days, and custom ranges. Custom ranges are limited to 366 elapsed
days; detail limits and offsets are bounded by the service.

The CLI uses the same service:

```text
mailforge reports summary --database state/mailforge.sqlite3
mailforge reports export --database state/mailforge.sqlite3 --report-type campaigns \
  --format csv --output reports/campaigns.csv
```

Exports are bounded to 5,000 rows. JSON, CSV, and HTML include report metadata;
CSV values are guarded against spreadsheet formula execution. Aggregate
reports do not include raw message bodies, credential values, or unrestricted
filesystem paths.

## Qualification boundary

The implementation is local and synthetic. It is suitable for inspecting
durable preparation, simulation, provider diagnostics, inbound evidence,
workflow state, List Tools, Data Studio, and Message Studio activity. It is
not a provider qualification, production monitoring system, tracking system,
or inbox-delivery claim. Larger workload measurements and connected browser
acceptance remain the next qualification step.

## Local measurement

An isolated synthetic SQLite benchmark with 1,000 campaigns, 10,000 recipient
intents, and 10,000 delivery attempts produced a full summary in 0.059 seconds
with approximately 0.13 MiB peak Python allocation under `tracemalloc`. The
attempt time query used the existing covering policy-time index and completed
as an index scan; no reporting-only index was added because this workload did
not justify another schema migration. This is a local engineering measurement,
not a production SLO or a qualification of larger histories.
