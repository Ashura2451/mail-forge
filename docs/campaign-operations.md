# Campaign operations

Campaign Operations is the local operator workspace for creating, reviewing,
preparing, scheduling, and supervising a campaign. It is a composition layer,
not a second campaign engine: `CampaignService`, `SegmentationService`,
`MessageStudioService`, `OutboundService`, `ProviderOperationsService`,
`CampaignStore`, and `ReportingService` remain the authorities for lifecycle,
eligibility, content, transport, accounts, durable records, and reporting.

## Boundaries

- Campaigns are local drafts until an operator explicitly authorizes a
  synthetic run.
- Audience and published message revisions are pinned before preparation.
- Provider accounts contribute configuration, readiness, pacing, and
  concurrency facts; credentials and secret material never enter campaign
  configuration or exports.
- Schedules use IANA time zones and reject nonexistent local times. Ambiguous
  local times require an explicit `fold`.
- Pause, resume, cancel, preparation, and authorization are idempotent at the
  operator-surface boundary. Ambiguous delivery outcomes remain unresolved and
  are not replayed automatically.
- CSV/JSON exports are bounded, formula-safe, and exclude raw message bodies.
- The worker is synthetic only. SMTP acceptance, inbox delivery, provider
  qualification, and live recipient sending are not claimed.

## Surfaces

The browser API and CLI share `CampaignOperationsService`:

- `POST /api/v1/campaigns`, `PATCH /api/v1/campaigns/{id}`
- `GET /api/v1/campaigns/{id}/workspace`
- `POST /api/v1/campaigns/{id}/preflight`
- `PUT /api/v1/campaigns/{id}/schedule`
- prepare, authorize, synthetic, pause, resume, and cancel controls
- bounded recipient/attempt drilldowns and JSON/CSV export
- `mailforge campaign workspace|update|preflight|schedule|pause|resume|cancel|recipients|attempts|export`

The React console presents the same plan as an accessible responsive workspace:
audience, content, sender, preflight findings, schedule, progress, recipients,
attempts, controls, and the explicit no-network boundary.

## Validation status

The implementation is covered by focused service/API tests and the existing
Python regression suite. Local browser automation, connected 48-step synthetic
acceptance, independent post-fix review, measured render-failure rates, and
backup/restore reconciliation remain follow-up evidence rather than claims of
completion.
