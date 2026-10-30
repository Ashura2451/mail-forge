# Review, feat/mailforge-provider-interoperability-031, 2026-10-30

**Reviewed by**: gpt-6.1-sol, independent reviewer agent (author reported by parent as Codex/GPT-5; author identity not independently authenticated)
**Scope**: 33 files, branch vs origin/main `db373a251826ff8ccfeaedccfd74a7aa7532bff2`
**Reviewed revision**: `f6d160c56b17764701b17d70016b66b1f6beddd7`
**Verdict**: Changes requested

## Summary

The earlier grant-reuse, duplicate-send ownership, cross-account request identity, fresh-session capability, refresh-token preservation, and generic IMAP-default defects have been corrected. The implementation now reserves and consumes before SMTP in one immediate SQLite transaction, and the browser checks confirmation and its prepared request fingerprint before submission. Independent execution found no remaining runtime blocker in those corrected paths; the remaining merge concern is regression coverage of the security and interrupted-operation boundaries, with two smaller browser-state defects.

## Major

### 🟠 Security and interrupted-reservation branches lack reliable regression coverage, `tests/test_provider_operations_019.py:287`

**Problem**: The new concurrency test submits two tasks against an immediate fake SMTP implementation without holding the first request inside SMTP. It can therefore exercise sequential duplicate lookup rather than the critical case where the second request observes an existing, still-reserved row. Repository search also found no HTTP `/grants` → `/test-send` test, no negative test for the persisted-field mismatch branch at `provider_operations.py:376`, and no regression for the new exception-finalization/restart path at `provider_operations.py:631`.

**Why it matters**: These are the security and recovery boundaries added to prevent repeated consequential operations. The configured-test rubric treats uncovered security/error branching as Major. The current tests and code inspection provide useful positive evidence, but do not reliably protect reservation ownership during actual overlap or prove that rejected/failed/interrupted requests cannot regain submission authority. This is a coverage finding, not a claim that a second send was reproduced on this revision.

**Suggested fix**: Add a deterministic blocking fake transport: hold the first caller after reservation, run the duplicate while it is blocked, assert one SMTP invocation and a returned reserved row, then release and assert the final result. Add HTTP tests with a server-issued grant for altered account/purpose/sender/recipient/expiry, consumed-grant reuse, and malformed/invented authorization, asserting zero additional transport calls. Exercise a pool-construction failure and reopening a database with a reserved operation; verify the consumed grant and original identity remain durable and no automatic replay occurs.

## Minor

### 🟡 Capability failures become a silent, unrecoverable empty selector, `web/src/App.tsx:887`

**Problem**: The capability effect catches every failure by clearing the options, without publishing an error or exposing retry. Its dependency is only `sessionReady`, so the shell Refresh action reloads account data without retrying capabilities.

**Why it matters**: An independent Chromium probe returned a synthetic 503 from `/providers/capabilities`; the profile selector contained zero options and no alert appeared. Clicking Refresh left it empty. The fresh-session race is fixed, but an ordinary transient failure still strands provider setup until navigation remounts the workspace or the page is reloaded.

**Suggested fix**: Surface a capability-specific error and retry, or load capabilities through the shell's existing error/refresh authority. Add a failed-response-then-retry browser case.

### 🟡 Submit appears enabled when its handler will silently refuse the request, `web/src/App.tsx:913`

**Problem**: `runTest` correctly refuses an unchecked confirmation or a changed request fingerprint, but the Submit button at line 919 is disabled only for missing grant/key state. Changing the message or account, or clearing confirmation after grant preparation, leaves an enabled primary action whose click does nothing.

**Why it matters**: Chromium verification prepared a grant, cleared confirmation, and separately edited the message: Submit remained enabled in both cases, while the intercepted test-send request count correctly remained unchanged. The safety boundary works, but the interface gives no explanation or recovery instruction for an apparently broken action.

**Suggested fix**: Derive button availability from the same confirmation/fingerprint predicate as the handler and show that authorization must be prepared again when the request changes. Keep the handler guard as well.

## Strengths

- Reservation creation and grant consumption share `BEGIN IMMEDIATE`; the explicit ownership result prevents a duplicate caller from opening SMTP. The reservation binds account, authorization, sender, recipient, and message digest, and failures retain durable evidence rather than automatically releasing authority.
- Runtime-only OAuth resolution is shared by SMTP and IMAP; refreshes serialize per reference and preserve the previous refresh token when a response omits rotation.
- Independent runtime inspection confirmed four capability profiles on a fresh direct route, IMAP port 993, new browser identities for newly prepared grants, fingerprint/confirmation refusal, independent rail/main scrolling, mobile keyboard wrapping, and Escape focus restoration. The viewed desktop and mobile captures retain legible operational hierarchy and visible focus.

## Test coverage and verification evidence

The source remained at the exact revision above. At review start, the parent packaging process had temporarily removed the three current compiled assets; they were restored byte-for-byte to committed content before browser inspection, and the working tree was clean before this findings file was created. No application code or tests were edited by this reviewer.

- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/e2e`: **261 passed**, one Starlette/httpx deprecation warning, 28.06 seconds.
- Independently ran an AST comparison using `git show db373a2:<path>` and `git show f6d160c:<path>` with Python `ast.parse`/`ast.dump`. The broad formatting changes in automation, data connections, Data Studio, Integration Hub, mailbox operations, and programmable operations are semantically equivalent; inbound-source behavior changes only at the OAuth-resolution seam. Actual provider/store/credential/SMTP/web changes were inspected directly against their surrounding contracts.
- Independently ran inline Playwright/Chromium probes against FastAPI serving the committed frontend, with a disposable synthetic database. Observed fresh `/providers` loading **4 profiles**, no page errors, Custom IMAP port **993**, account creation, grant preparation, changed-message and unchecked-confirmation refusal, and a different key after preparing another grant. Test-send HTTP responses were intercepted for this frontend check; it did **not** contact a provider or prove delivery. Observed desktop main scroll **1725px** with rail top **0**, mobile focus wrap to Maintenance, and restored menu focus after Escape. Also reproduced the two Minor findings above. Captures `/tmp/mailforge-final-review-desktop.png` and `/tmp/mailforge-final-review-mobile.png` were actually viewed.
- Independently ran an inline migration/backup probe that loaded the merge-base `CampaignStore` implementation into an isolated module, created a populated schema-23 database, then reopened it with current code: schema **23 → 24**, historical submission identity preserved. Created a current grant/reservation, performed `backup_to` and `restore_from`, and observed schema **24**, successful integrity check, consumed count **1**, and preserved `reserved` state. An initial probe omitted the legacy method's required `message_revision_id`; the corrected probe above passed. This is independent manual evidence; no committed populated-schema-23 regression exists.
- Independently compared all **15 existing changed Python/frontend files** under `src/mailforge` with their installed counterparts inside `dist/Mailforge.app`: byte-for-byte matches. Launched a disposable FastAPI server using the bundle's own embedded Python and installed package, then visited its `/providers` route in Chromium: **4 capability options** rendered. This verifies the packaged embedded runtime and assets, not the macOS launcher/window lifecycle, signing, notarization, or external-provider qualification.
- The initial inline browser probe omitted `frontend_dir`, so it reached an API-only app and could not locate the form; it was corrected to serve the committed frontend before relying on browser evidence. No full E2E rerun, axe sweep, frontend rebuild, or lint/typecheck was performed by this reviewer. The passing non-browser suite includes existing protocol, application backup, policy, web-security, and recovery checks; it does not establish a measured memory bound.

The controlled-send foundation's canonical Message Studio rendering and eligibility/suppression integration remain a pre-existing spec-0009 AC-9 qualification gap, unchanged from the supplied merge base. Optional message revision IDs are still caller-supplied metadata rather than proof of canonical rendering. This PR must not be cited as qualification of those inherited policy/revision requirements or of real providers.
