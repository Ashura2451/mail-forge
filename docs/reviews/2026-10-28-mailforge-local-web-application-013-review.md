# Review, feat/mailforge-local-web-application-013, 2026-10-28

## Review disposition

The available reviewer returned an inline/degraded-independence review rather
than a separate model review. Its findings were used as a final defect pass;
no independent approval is claimed.

## Findings and resolution

- API reads initially bypassed the session boundary. Resolved: all `/api/v1`
  routes other than session bootstrap require the in-memory local session.
- Inbound data initially did not match the review UI contract. Resolved:
  `/api/v1/inbound` now exposes the pending review items alongside messages and
  stats.
- Worker exceptions and stale running campaigns were not observable across a
  restart. Resolved: worker failures transition running campaigns to durable
  `failed`; startup reconciles stale running/pausing work to `paused` through
  lease recovery so explicit resume remains available.
- Campaign composition fields were initially discarded. Resolved: sender,
  subject, and text are stored in the campaign configuration and used by
  preparation; explicit campaign preview returns prepared message data as
  text/structured JSON for the operator UI.
- Collection APIs were initially unbounded. Resolved: lists, contacts, and
  campaigns use bounded limit/offset parameters; activity already supports
  bounded pagination and aggregate filtering.
- IPv6 loopback Host parsing was initially incorrect. Resolved with URL-based
  Host parsing and bracketed-origin matching.

## Evidence

- `.venv/bin/pytest -q`: 152 passed, one upstream Starlette/httpx deprecation
  warning.
- `tests/e2e/test_browser_acceptance.py`: one real-browser test passed against
  a loopback Uvicorn process, using desktop and 390px narrow viewports.
- TypeScript production build and Oxlint passed.
- Ruff, lock verification, secret check, package build, and staged diff checks
  passed.
- Wheel inspection confirmed five compiled web asset entries and no `.env` or
  state directory contents.

## Qualifications

The missing unified 012-N lifecycle remains qualified: this milestone adds
the browser/API acceptance path, but does not claim one stateful SMTP/IMAP,
ambiguity, inbound, restart, and backup/restore scenario. 011-S also remains
unverified because the prior final independent rereview was unavailable. No
real provider, credential, mailbox, or recipient was contacted.
