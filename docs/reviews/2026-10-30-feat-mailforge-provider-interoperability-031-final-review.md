# Review, feat/mailforge-provider-interoperability-031, 2026-10-30

**Reviewed by**: gpt-6.1-sol, independent reviewer agent (author reported by parent as Codex/GPT-5; author identity not independently authenticated)
**Scope**: 34 files, branch vs origin/main `db373a251826ff8ccfeaedccfd74a7aa7532bff2`
**Reviewed revision**: `cad79e93cc5eb977ab0b20a5424d3ec788f45f32`, incremental re-review from `f6d160c56b17764701b17d70016b66b1f6beddd7`
**Verdict**: Approve with nits
**Merge review gate**: PASS; no unresolved Blocker or Major findings.

## Summary

The earlier grant-reuse, duplicate-send ownership, cross-account request identity, fresh-session capability, refresh-token preservation, and generic IMAP-default defects have been corrected. The implementation now reserves and consumes before SMTP in one immediate SQLite transaction, and the browser checks confirmation and its prepared request fingerprint before submission. Commit `cad79e9` adds the deterministic overlap, HTTP rejection, and failure/reopen regressions needed to resolve the previous Major coverage finding; independent focused checks pass. Two smaller browser-state defects remain nonblocking.

## Resolved Major finding

### Security and interrupted-reservation coverage, resolved by `cad79e9`

**Original finding**: The immediate fake transport did not deterministically establish overlap, and HTTP authorization mismatch and exception-finalization branches lacked regression coverage at `f6d160c`.

**Resolution**: `test_idempotency_reservation_ownership_is_proven_while_first_send_is_blocked` holds the first caller inside SMTP, proves the duplicate returns the existing `reserved` row, and verifies only one eventual transport call. `test_failed_provider_test_is_ambiguous_and_survives_reopen` injects pool-construction failure and verifies ambiguity and consumed authority survive durable readback/reopen. `test_provider_grant_http_boundary_rejects_changed_request_before_transport` issues a grant through HTTP and verifies altered-recipient and invented-authorization requests are rejected before any transport creation. These exercise the previously uncovered control-flow boundaries; existing tests separately cover consumed-grant reuse and request/account identity conflicts.

**Remaining limits**: The HTTP test is representative rather than a permutation of every bound field; actual process termination at the SMTP boundary is not simulated. Those limits do not leave the prior Major finding open, given the deterministic ownership test, durable failure test, existing grant-count/identity tests, and independent reservation backup/readback evidence below.

## Minor

### 🟡 Capability failures become a silent, unrecoverable empty selector, `web/src/App.tsx:887`

**Problem**: The capability effect catches every failure by clearing the options, without publishing an error or exposing retry. Its dependency is only `sessionReady`, so the shell Refresh action reloads account data without retrying capabilities.

**Why it matters**: An independent Chromium probe returned a synthetic 503 from `/providers/capabilities`; the profile selector contained zero options and no alert appeared. Clicking Refresh left it empty. The fresh-session race is fixed, but an ordinary transient failure still strands provider setup until navigation remounts the workspace or the page is reloaded.

**Suggested fix**: Surface a capability-specific error and retry, or load capabilities through the shell's existing error/refresh authority. Add a failed-response-then-retry browser case.

### 🟡 Submit appears enabled when its handler will silently refuse the request, `web/src/App.tsx:915`

**Problem**: `runTest` correctly refuses an unchecked confirmation or a changed request fingerprint through the new `testReady` predicate, but the Submit button at line 922 still uses only missing grant/key state for `disabled`. Changing the message or account, or clearing confirmation after grant preparation, leaves an enabled primary action whose click does nothing. Extracting `testReady` in `cad79e9` did not wire it into button availability.

**Why it matters**: Chromium verification prepared a grant, cleared confirmation, and separately edited the message: Submit remained enabled in both cases, while the intercepted test-send request count correctly remained unchanged. The safety boundary works, but the interface gives no explanation or recovery instruction for an apparently broken action.

**Suggested fix**: Derive button availability from the same confirmation/fingerprint predicate as the handler and show that authorization must be prepared again when the request changes. Keep the handler guard as well.

## Strengths

- Reservation creation and grant consumption share `BEGIN IMMEDIATE`; the explicit ownership result prevents a duplicate caller from opening SMTP. The reservation binds account, authorization, sender, recipient, and message digest, and failures retain durable evidence rather than automatically releasing authority.
- Runtime-only OAuth resolution is shared by SMTP and IMAP; refreshes serialize per reference and preserve the previous refresh token when a response omits rotation.
- Independent runtime inspection confirmed four capability profiles on a fresh direct route, IMAP port 993, new browser identities for newly prepared grants, fingerprint/confirmation refusal, independent rail/main scrolling, mobile keyboard wrapping, and Escape focus restoration. The viewed desktop and mobile captures retain legible operational hierarchy and visible focus.

## Test coverage and verification evidence

The initial full review and observations below used `f6d160c`; the final incremental re-review used exact HEAD `cad79e9` with a clean starting working tree. No application code or tests were edited by this reviewer. At initial review start, the parent packaging process had temporarily removed the three current compiled assets; they were restored byte-for-byte to committed content before initial browser inspection.

- **Final revision checks, independently rerun at `cad79e9`**: `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_provider_operations_019.py tests/test_provider_interoperability.py tests/test_web.py`: **44 passed**, one Starlette/httpx deprecation warning, 13.05 seconds. `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/e2e/test_browser_acceptance.py::test_direct_providers_route_bootstraps_capabilities`: **1 passed**, 1.65 seconds. Inspected the complete `git diff f6d160c cad79e9`; application changes are confined to extracting the existing capability loader and a stricter shared handler predicate, plus corresponding compiled assets. The earlier migration/backend review remains applicable because backend implementation did not change. The earlier packaged-runtime observation applies to `f6d160c`; the package was not independently requalified against `cad79e9`.

- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/e2e`: **261 passed**, one Starlette/httpx deprecation warning, 28.06 seconds.
- Independently ran an AST comparison using `git show db373a2:<path>` and `git show f6d160c:<path>` with Python `ast.parse`/`ast.dump`. The broad formatting changes in automation, data connections, Data Studio, Integration Hub, mailbox operations, and programmable operations are semantically equivalent; inbound-source behavior changes only at the OAuth-resolution seam. Actual provider/store/credential/SMTP/web changes were inspected directly against their surrounding contracts.
- Independently ran inline Playwright/Chromium probes against FastAPI serving the committed frontend, with a disposable synthetic database. Observed fresh `/providers` loading **4 profiles**, no page errors, Custom IMAP port **993**, account creation, grant preparation, changed-message and unchecked-confirmation refusal, and a different key after preparing another grant. Test-send HTTP responses were intercepted for this frontend check; it did **not** contact a provider or prove delivery. Observed desktop main scroll **1725px** with rail top **0**, mobile focus wrap to Maintenance, and restored menu focus after Escape. Also reproduced the two Minor findings above. Captures `/tmp/mailforge-final-review-desktop.png` and `/tmp/mailforge-final-review-mobile.png` were actually viewed.
- Independently ran an inline migration/backup probe that loaded the merge-base `CampaignStore` implementation into an isolated module, created a populated schema-23 database, then reopened it with current code: schema **23 → 24**, historical submission identity preserved. Created a current grant/reservation, performed `backup_to` and `restore_from`, and observed schema **24**, successful integrity check, consumed count **1**, and preserved `reserved` state. An initial probe omitted the legacy method's required `message_revision_id`; the corrected probe above passed. This is independent manual evidence; no committed populated-schema-23 regression exists.
- Independently compared all **15 existing changed Python/frontend files** under `src/mailforge` with their installed counterparts inside `dist/Mailforge.app`: byte-for-byte matches. Launched a disposable FastAPI server using the bundle's own embedded Python and installed package, then visited its `/providers` route in Chromium: **4 capability options** rendered. This verifies the packaged embedded runtime and assets, not the macOS launcher/window lifecycle, signing, notarization, or external-provider qualification.
- The initial inline browser probe omitted `frontend_dir`, so it reached an API-only app and could not locate the form; it was corrected to serve the committed frontend before relying on browser evidence. No full E2E rerun, axe sweep, frontend rebuild, or lint/typecheck was performed by this reviewer. The passing non-browser suite includes existing protocol, application backup, policy, web-security, and recovery checks; it does not establish a measured memory bound.

The controlled-send foundation's canonical Message Studio rendering and eligibility/suppression integration remain a pre-existing spec-0009 AC-9 qualification gap, unchanged from the supplied merge base. Optional message revision IDs are still caller-supplied metadata rather than proof of canonical rendering. This PR must not be cited as qualification of those inherited policy/revision requirements or of real providers.
