# Review, Mailforge audience segmentation 015 resolution, 2026-10-28

**Task:** `MAILFORGE-AUDIENCE-SEGMENTATION-015`
**Branch:** `feat/mailforge-audience-segmentation-015`
**Result:** IMPLEMENTED AND LOCALLY VERIFIED

## Evidence

- CampaignStore schema 7 is additive, retry-safe, foreign-key enabled, and
  migration 7 executes within the caller transaction. An injected migration
  failure rolled back created tables; a subsequent retry upgraded cleanly.
- NUMBER values use signed 64-bit scaled integers at scale 1,000,000. Decimal
  scientific-notation overflow, binary float inputs, excessive precision,
  silent rounding, and range violations are rejected.
- Segment definitions are registry-bound and parameterized. Missing attributes
  remain `UNKNOWN`; archived fields remain readable by historical revisions but
  are not selectable for new definitions. Cross-segment revision IDs are
  rejected.
- Structured imports are bounded to 10,000 rows, preflight the target list,
  preserve populated values by default, audit explicit overwrites, and replay
  the same source row idempotently.
- Campaign preparation evaluates source selection and eligibility under one
  immediate SQLite snapshot. Primary delivery intents and recurring run-key
  audience recipients are independent durable records; list and segment
  recurring runs report their own recipients.
- The HTTP API and CLI expose field creation/update/archive, structured import
  preview/write, list history, segment edit/archive, and campaign audience run
  keys. The React console exposes typed fields, structured import preview/write,
  list history, nested bounded audience conditions, current revision pinning,
  and segment previews. Outbound remains synthetic/local.

## Validation

- `.venv/bin/pytest -q` — 167 passed, one existing Starlette/httpx warning.
- `.venv/bin/pytest -q tests/e2e` — 3 passed, including responsive and
  accessibility/browser operational acceptance.
- Frontend lint and build passed; the packaged assets were refreshed.
- Ruff check and format check passed.
- `uv lock --check`, `uv build`, `bash scripts/check-secrets.sh`, and
  `git diff --check` passed.
- Synthetic preview benchmark: 1,000 contacts / 500 matches in 2.32 ms;
  10,000 contacts / 5,000 matches in 9.46 ms in one CPython 3.15.2 run.

## Review limitations

- The available independent reviewer was the same-model inline reviewer, so
  independence is degraded and no independent approval is claimed. Its
  backend findings were reproduced and corrected. The browser remains a local
  operator console and does not qualify hosted identity, provider readiness,
  consent, inbox placement, or real-recipient execution.
- No provider, mailbox, credential, real recipient, or real audience data was
  accessed.
