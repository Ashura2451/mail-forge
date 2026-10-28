# Integration Hub

The Integration Hub is a local-first boundary for bounded external observations
and durable operational notifications. It does not become a generic proxy,
workflow engine, hosted callback service, or live-email path.

## Read-only HTTP sources

Data Connections owns HTTP profiles and snapshots. A profile stores an exact
HTTPS origin and absolute path, an optional bounded query, one of JSON, JSONL,
or CSV formats, and an explicit pagination mode. Reads are GET-only, bounded by
response size, page size, page count, timeout, field count, and record count.
Redirects, credentials in URLs, cross-origin pagination, private-address DNS
resolution, and arbitrary methods are rejected. Loopback HTTP is available only
to explicitly configured synthetic fixtures in a test-enabled service.

Credential values are resolved at request time from symbolic references. They
are never stored in profiles, snapshots, browser state, URLs, or reports.
Completed reads publish immutable JSONL snapshots with source identity,
retrieval time, page identity, an observation window, record provenance, and a
checksum. Data Studio receives completed snapshots; downstream campaign,
contact, message, and automation services retain their existing authority.

## Scoped clients and incoming events

Integration clients receive a token once. Only a SHA-256 hash and short token
prefix are durable. Clients have explicit capabilities, an expiry of at most
one year, rotation, revocation, and an audit trail. The event endpoint requires
an `INGEST_LOCAL_EVENT` bearer capability and accepts a bounded typed envelope:

```json
{
  "event_id": "source-event-1",
  "source": "approved.local.system",
  "type": "external_dataset_available",
  "time": "2026-10-28T12:00:00Z",
  "schema_version": "1",
  "payload": {"dataset_ref": "dataset-1"},
  "provenance": {"observation_ref": "obs-1"}
}
```

Repeated source/client/event identities are idempotent when the payload hash
matches and become visible conflicts when it does not. External observations
are never treated as authenticated SMTP, DKIM, SPF, consent, unsubscribe, or
business-approval evidence. Active Automation triggers may enroll through the
existing durable AutomationService; no second scheduler or workflow authority
is introduced.

## Signed outgoing webhooks

Subscriptions select registered operational event types and store only a
credential reference. Destinations require a fixed HTTPS URL with no URL
credentials, query, fragment, redirect, or private-address resolution. A
synthetic loopback fixture is an explicit test-only exception.

Payloads are minimal operational references. Delivery signs the exact raw JSON
body with HMAC-SHA256 over `timestamp + "." + body` and sends version, event,
delivery, timestamp, and `v1=` signature headers. Delivery is at-least-once:
2xx completes, bounded retryable statuses enter exponential backoff, permanent
4xx responses fail, and transport ambiguity remains visible. Pausing a
subscription suspends pending work; re-enabling makes incomplete work eligible
again. No exactly-once or remote business completion claim is made.

## Surfaces and limits

The browser uses `/integrations` and the Data Connections workspace. The API
and CLI expose client lifecycle, event catalog/submission, subscription state,
previews, deliveries, diagnostics, and the integration report. The existing
supervised automation runtime polls due webhook deliveries with a bounded batch;
there is no new scheduler.

All evidence is local or synthetic unless a separately approved credentialed
qualification says otherwise. The public-hosted, unrestricted-live-send,
arbitrary-HTTP, arbitrary-code, and cross-origin callback boundaries remain
closed.
