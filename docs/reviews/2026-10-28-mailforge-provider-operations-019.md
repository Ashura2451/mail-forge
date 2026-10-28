# Review, Mailforge provider operations 019, 2026-10-28

**Task:** `MAILFORGE-PROVIDER-OPERATIONS-019`
**Branch:** `feat/mailforge-provider-operations-019`
**Base:** verified 018 feature HEAD `16ad2b1`
**Commit:** to be recorded after final publication

## Result

Provider Operations is implemented on the existing local-first Python/FastAPI,
React/Vite, SQLite, SMTP, IMAP, inbound-review, policy, and Message Studio
authorities. The implementation preserves synthetic execution and does not
enable unrestricted live campaigns.

The result is local synthetic and protocol-fixture evidence. No real provider,
credential, mailbox, recipient, or private list data was accessed.

`REAL_PROVIDER_QUALIFICATION_NOT_PERFORMED`: no owner authorization, real
provider credential, or owner-controlled recipient mailbox was supplied.

## Architecture and implementation

- Accepted architecture: [0009 provider operations](../specs/0009-provider-operations.md).
- One canonical `ProviderOperationsService` owns account lifecycle, capability
  registry, OAuth protocol state, diagnostics, grants, inbound settings, and
  bounded test-send behavior.
- Migration 11 adds named accounts, inbound settings, expiring operation-grant
  evidence, OAuth-flow metadata, test submissions, and campaign account binding.
- Credential values remain runtime-only. Environment and synthetic resolvers
  are supported; OAuth token bytes are not returned to the browser or stored in
  SQLite.
- Generic SMTP and IMAP support password and XOAUTH2 authentication at the
  existing transport/source seams. TLS verification, bounded timeouts, and
  fail-closed authentication remain in the canonical adapters.
- Google and Microsoft presets describe current documented protocol hosts,
  scopes, TLS defaults, restrictions, and documentation links. Provider-side
  consent, tenant settings, verification, quotas, and delivery remain
  qualification concerns.
- Campaigns can bind one explicit account; simulation remains account-optional.
  No account rotation, suppression bypass, or live bulk campaign endpoint was
  added.

## Acceptance evidence

| Criterion | Status | Evidence |
| --- | --- | --- |
| A. 016–018 integration reconciled | PASS | Branch starts at verified 018 HEAD; PR #1 and stacked ancestry inspected |
| B. Provider account management | IMPLEMENTED | Migration 11, account service, API, CLI, and Providers workspace |
| C. Multiple accounts | IMPLEMENTED / LOCALLY TESTED | Named account rows, deterministic list, account-isolation service tests |
| D. Credential references | PASS | Symbolic refs, runtime resolvers, redaction tests |
| E. OAuth | LOCALLY PROTOCOL TESTED | State, PKCE, exact loopback redirect, single-use callback, refresh/identity fixtures |
| F. Generic SMTP TLS | LOCALLY PROTOCOL TESTED | Existing TLS fixtures plus XOAUTH2 transport test |
| G. Generic IMAP TLS | LOCALLY PROTOCOL TESTED | Existing implicit-TLS/read-only UID fixture plus XOAUTH2 seam |
| H. Capability discovery | IMPLEMENTED / LOCALLY TESTED | Four provider descriptors and observed SMTP/IMAP capability fields |
| I. Truthful diagnostics | IMPLEMENTED / LOCALLY TESTED | Distinct configuration/local/external records and sanitized outcomes |
| J. Configuration vs connection | PASS | Validation performs no network; external diagnostic requires a grant |
| K. Controlled test send | IMPLEMENTED / LOCALLY TESTED | One-shot durable submission, stable idempotency, phase evidence |
| L. External authorization gate | PASS | Endpoint/operation/purpose/count/time/sender/recipient grant validation |
| M. Safe simulation | PASS | Full suite and browser synthetic campaigns pass; no live campaign route |
| N. Campaign account binding | IMPLEMENTED / LOCALLY TESTED | Nullable binding persisted with campaign creation and browser selection |
| O. Message Studio integration | PASS | Existing canonical renderer and MIME tests remain green |
| P. Recipient attachment isolation | PASS | Existing Message Studio regression suite remains green |
| Q. Eligibility/suppression | PASS | Existing policy and inbound review tests remain green |
| R. Post-DATA ambiguity | PASS | Existing SMTP ambiguity/no-retry tests plus controlled-send evidence |
| S. Inbound integration | PASS | Existing canonical IMAP/correlation/review path retained |
| T. Restart/recovery | PASS | Existing runtime recovery tests remain green |
| U. Browser account management | BROWSER VERIFIED (local build) | Packaged bundle rebuilt; browser route/accessibility suite passes |
| V. API/CLI authority | IMPLEMENTED / LOCALLY TESTED | Both boundaries call canonical provider operations and share grants |
| W. Security | PASS | Secret scan, redaction, CSRF/host/session, TLS, upload, and auth tests |
| X. Accessibility/responsive | PASS | 4 Playwright browser tests including axe and desktop/mobile contexts |
| Y. Existing capabilities | PASS | Full Python suite: 189 passed |
| Z. Complete synthetic acceptance | PASS (local scope) | Full suite, TLS fixtures, OAuth fixtures, inbound, recovery, backup/integrity |
| AA. Real-provider result | NOT PERFORMED | No owner authorization or real provider data supplied |
| AB. Independent review | NOT VERIFIED | No independent reviewer was available in this execution environment |
| AC. Documentation | PASS | Spec, provider guide, capability baseline, performance note, changelog |
| AD. Feature publication | PENDING FINAL PUSH | Exact branch/commit/push verification follows this review |
| AE. Main integration | NOT INTEGRATED | `origin/main` remains `3bd42f7`; no merge performed |

## Validation

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q`: **189 passed**,
  one existing Starlette/httpx deprecation warning.
- `cd web && PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run lint`: passed
  with the existing React `set-state-in-effect` warnings.
- `cd web && PATH=/opt/homebrew/opt/node@24/bin:$PATH npm run build`: passed;
  packaged assets refreshed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q src tests`:
  passed.
- `uv run ruff check src tests` and `uv run ruff format --check src tests`:
  passed.
- `uv lock --check`, `uv build`, `bash scripts/check-secrets.sh`, and
  `git diff --check`: passed.
- Synthetic service measurements are recorded in
  [`docs/performance/provider-operations-2026-10-28.md`](../performance/provider-operations-2026-10-28.md).

## Review limitations

- Local TLS and OAuth fixtures prove adapter behavior, not commercial-provider
  consent, tenant policy, quota, inbox arrival, deliverability, or production
  readiness.
- OAuth token exchange is represented by an injected local transport seam; no
  commercial token endpoint was contacted.
- The browser presents controlled-operation and external-qualification
  boundaries, while the current outbound campaign route remains simulation-only.
- The prior 011-S correction remains locally corrected but independently
  unapproved; no new approval is claimed here.
- No publication or merge is claimed until the final commit and remote branch
  verification are complete.
