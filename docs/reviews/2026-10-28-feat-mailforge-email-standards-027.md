# MAILFORGE-EMAIL-STANDARDS-027 review and acceptance record

Date: 2026-10-28
Branch: `feat/mailforge-email-standards-027`
Review scope: Email Standards, sender diagnostics, unsubscribe policy, DMARC
imports, campaign preflight, interfaces, persistence, and packaged runtime.

## Validation evidence

- `PYTHONPATH=src .venv/bin/pytest -q`: **249 passed**, one existing
  Starlette/httpx deprecation warning.
- Focused standards/campaign/web/outbound tests: **42 passed**, one existing
  warning.
- `PYTHONPATH=src .venv/bin/pytest -q tests/e2e/test_browser_acceptance.py tests/e2e/test_accessibility.py`:
  **5 passed**.
- `uvx --from ruff ruff check src tests`: passed.
- `python -m compileall -q src`: passed.
- `git diff --check`: passed.
- `npm run build` and `npm run lint` with Node 24: build passed; lint passed
  with the existing seven React effect warnings.
- `uv build`: source distribution and wheel passed.
- `bash scripts/check-secrets.sh`: passed.
- `bash scripts/package-macos.sh`: arm64 `Mailforge.app` built with Python
  3.15.2 and schema 21 dependencies; signing/notarization was not performed.
- Packaged runtime smoke: guidance CLI output and fresh schema initialization
  passed from the bundle’s embedded Python/site-packages.
- Independent read-only review was attempted twice. The final review reported
  no remaining concrete blockers, but explicitly did not claim independent
  approval and did not rerun the suite.

## Acceptance matrix

| Criterion | Status | Evidence / limitation |
| --- | --- | --- |
| A | PASS | `EmailStandardsService` owns diagnostics and durable runs. |
| B | PASS | Sender identity persistence and validation tests. |
| C | PASS | SPF syntax, mechanisms, evaluation, recursion, and lookup-bound tests. |
| D | PASS | DKIM record/evidence classification; cryptographic verification remains unimplemented and is not claimed. |
| E | PASS | DMARC syntax, policy, alignment mode, and public-suffix-aware alignment tests. |
| F | PASS | DNS facts retain lookup status, TTL, timestamps, and opt-in network behavior. |
| G | PASS | MIME/header/size/line-ending/attachment validation tests. |
| H | PASS | CID, text alternative, URL, alt text, and unsubscribe content findings. |
| I | PASS | Dated, scoped Google and Yahoo guidance with source links. |
| J | PASS | Marketing/subscribed checks are distinct from transactional readiness. |
| K | PASS | Readiness does not promote caller DKIM claims; full status requires future trusted cryptographic evidence. |
| L | PASS | POST-only local endpoint, exact RFC 8058 body, replay idempotency, and low-level gated header pair are tested; campaign auto one-click remains blocked until a signer exists. |
| M | PASS | Loopback boundary and no public endpoint claim; external verification is not accepted as a configuration value. |
| N | PASS | Opaque hashed tokens, list scope, expiry, GET safety, replay, and atomic suppression tests. |
| O | PASS | API/CLI submitted authentication headers remain untrusted; trusted receiver evidence is an internal service boundary. |
| P | PASS | XML/gzip/bzip2/ZIP bounds, UTF-16 entity rejection, traversal, row limits, and dedupe tests. |
| Q | PASS | Schema 21 diagnostic history and aggregate report metadata are durable. |
| R | PASS | Email Standards navigation/workbench, API routes, browser build, and browser/accessibility suites. |
| S | PASS | Campaign preflight consumes standards findings and blocks unsupported one-click preparation. |
| T | NOT VERIFIED | Reporting integration is implemented and included in the full suite; a dedicated connected report export scenario was not run. |
| U | PASS | Shared application authority is used by browser/API/CLI paths; CLI and packaged smoke passed. |
| V | PASS | No unrestricted live send or provider connection was added. |
| W | PASS | Full regression suite passed. |
| X | PASS | Final arm64 package build and embedded-runtime smoke passed; signing/notarization remains out of scope. |
| Y | PASS | Resource, XML, token, CSRF/host, trust-boundary, and secret checks passed. |
| Z | NOT VERIFIED | The complete 56-step connected synthetic acceptance was not executed end to end. |
| AA | NOT VERIFIED | No new benchmark was run for DNS/message/DMARC throughput or database growth. |
| AB | PASS | Full Python, frontend, lint, format/check, build, package, and secret validation passed. |
| AC | PASS | Final read-only review found no concrete blockers; it was not independent approval. |
| AD | PASS | Spec, changelog, capability atlas, operator docs, architecture, and repo truth updated. |
| AE | NOT VERIFIED | Branch publication and remote verification pending commit/push. |
| AF | NOT VERIFIED | Main integration pending PR/merge. |

## Security and scope notes

The campaign boundary intentionally prepares `List-Unsubscribe` but blocks the
RFC 8058 one-click pair because this milestone does not contain a real DKIM
signing/verifying boundary. The low-level composer retains its explicit
caller-owned prerequisite for future externally signed bytes. No provider,
mailbox, credential, public endpoint, recipient dataset, live DNS request, or
live send was used in acceptance.
