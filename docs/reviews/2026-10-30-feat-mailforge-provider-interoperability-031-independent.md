# Review, feat/mailforge-provider-interoperability-031, 2026-10-30

**Reviewed by**: gpt-6.1-sol, independent reviewer agent (author on the parent Codex session, reported by that session as GPT-5; author identity not independently authenticated)
**Scope**: 27 files, branch vs origin/main, using supplied merge base `db373a251826ff8ccfeaedccfd74a7aa7532bff2`
**Verdict**: Blocked

## Summary

The change adds persisted operation-grant issuance, an OAuth refresh resolver, provider setup controls, explicit test-client teardown, and independent shell scrolling. The local-first service boundaries, symbolic credentials, conservative protocol evidence, and narrowly scoped ignore rules are well preserved. However, an issued one-submission grant is reusable, the browser reuses one idempotency key across distinct operations/accounts, fresh direct navigation breaks capability loading, and refresh material can lose the token needed for its next refresh.

## Blockers

### 🔴 Issued one-submission grants have no consumption or durable reservation, `src/mailforge/application/provider_operations.py:316`

**Problem**: `issue_grant` persists `max_submissions=1`, but `grant_for_authorization` only matches static fields and expiry. Neither it nor `controlled_test_send` consumes that allowance or associates a durable submission with the authorization. The new grant-gated HTTP path therefore accepts the same issued grant with any number of fresh idempotency keys. The existing submission implementation also checks for a prior result before SMTP and records the result afterward, leaving no reservation during concurrent calls or a process crash.

**Why it matters**: This violates the explicit one-submission authorization displayed to the operator and spec 0009 AC-8/AC-9. An independent synthetic reproduction issued a grant through `issue_grant`, resolved it through `grant_for_authorization`, and used it for two different keys: both invoked SMTP and created separate rows. A synchronized two-thread reproduction with the same key also invoked SMTP twice while returning one durable row. A crash after remote acceptance can similarly leave a retryable-looking absence of evidence. The underlying send implementation predates this merge base; the new server-issued authorization workflow inherits and exposes the weakness rather than enforcing the advertised count.

**Suggested fix**: Atomically reserve a submission and consume its issued grant before opening SMTP, linking authorization, account, immutable request identity, and submission state. Make retries return the existing reservation/result, reject a consumed grant for a different operation, and retain in-progress/interrupted outcomes for conservative reconciliation. Exercise fresh-key grant reuse, concurrent duplicate requests, crash/restart, and post-DATA ambiguity through the shared service and HTTP boundary.

## Major

### 🟠 A single immutable browser key is reused across different submissions and accounts, `web/src/App.tsx:869`

**Problem**: `testKey` is generated once for the lifetime of `Providers`, has no setter, and is now read-only. Preparing another authorization, editing the message/recipient, or selecting another account cannot create a new operation identity. Same-account attempts therefore return an earlier result without performing the newly requested test. With another account, the service's account-filtered precheck misses the prior key; SMTP executes, then the globally unique store lookup returns the first account's row.

**Why it matters**: The new browser workflow makes this collision routine. Independent synthetic verification of two accounts using the same key produced an SMTP call for account two, returned account one's result, and recorded no test row for account two. This loses account-specific evidence and misrepresents the operation that just happened.

**Suggested fix**: Generate a distinct key for each newly prepared, confirmed operation and retain it only for retries of that exact operation. Bind the server-side identity to account and immutable request content; reject mismatched reuse before network access. Cover consecutive tests, changed content, and two-account browser/API journeys.

### 🟠 Fresh direct visits load provider capabilities before the local session exists, `web/src/App.tsx:880`

**Problem**: The new child mount effect immediately fetches `/providers/capabilities`, while the parent separately bootstraps `/session`. On a fresh direct visit to `/providers`, the capability request reaches the session-protected endpoint first. Its rejected promise has no error handler, and this mount-only effect never retries after session establishment or the shell's Refresh action.

**Why it matters**: Independent Chromium inspection against the compiled local frontend recorded capabilities HTTP 401, zero profile options, and the uncaught page error `a local operator session is required`. The profile selector stays empty until a full reload or remount, breaking direct-route setup and the retryable error requirement of spec 0019. The added browser tests first visit Dashboard, which hides the race.

**Suggested fix**: Load capability data after the shared session bootstrap, using the existing loading/error/retry authority, or explicitly await a shared session-ready promise. Add a browser case that starts a fresh context directly at `/providers`, and an unavailable-capability response/retry case.

### 🟠 Refresh responses without a replacement refresh token discard the usable original, `src/mailforge/infrastructure/credentials.py:154`

**Problem**: The wrapper replaces cached material with the callback result verbatim. `OAuthTokenMaterial.refresh_token` is optional, so a successful callback returning a new access token and expiry without a replacement refresh token drops the original token. At the following expiry, the cached value is selected and line 141 refuses to refresh, even though the original refresh token remains usable.

**Why it matters**: The advertised automatic refresh works only once for that valid response shape, causing an avoidable reauthorization failure in both SMTP and IMAP. An independent synthetic reproduction returned access material with a short expiry and no new refresh token; the next call raised `CredentialError: OAuth refresh token is unavailable; reauthorization required`, with only one exchange attempted. The current test always returns the previous refresh token explicitly and misses this branch.

**Suggested fix**: Preserve the prior refresh token when the exchange omits a replacement, while respecting explicitly returned rotations. Verify consecutive refreshes, rotation, and invalid/failed exchanges without persisting token material.

## Minor

### 🟡 Custom IMAP preset retains the SMTP port, `web/src/App.tsx:889`

**Problem**: The UI correctly reads `inbound_defaults` for IMAP, but the generic IMAP capability descriptor stores port 993 in `outbound_defaults` and puts only the mailbox in `inbound_defaults`. Selecting Custom IMAP from the initial form therefore retains port 465; selecting it after an SMTP preset can retain 587.

**Why it matters**: The supposedly helpful protocol preset creates a misconfigured account by default. Independent Chromium verification selected Custom IMAP and observed port `465`.

**Suggested fix**: Put the generic IMAP port in its inbound defaults and verify preset/protocol switching independently of previously selected SMTP settings.

## Strengths

- Credential and OAuth material remain runtime-only, secret values are excluded from dataclass representations, and both SMTP and IMAP reuse the same resolution seam.
- The new fixture finalizer enters and exits the FastAPI lifespan, which closes the runtime's worker/scheduler services. The full non-browser Python suite completed successfully during independent review.
- Shared rail/main scrolling retains the existing application-service architecture. Desktop, tablet, mobile drawer, and packaged screenshots were visually inspected; the observed hierarchy, focus indicator, and independent rail placement are coherent. A live narrow-browser wheel check kept background scrolling stationary while the drawer was open.
- The ignore additions name AI-specific paths rather than hiding all of `.github` or general project configuration. No covered local-only artifact was returned by the tracked-path check.

## Test coverage

Independent checks on clean starting revision `5f1a7f39014207ae7b16aa3ab3d5e1c78e8aceab`:

- `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests --ignore=tests/e2e`: **256 passed**, one Starlette/httpx deprecation warning, 27.44 seconds. The focused provider/interoperability/web run separately passed **36 tests**. An initial invocation without `PYTHONPATH=src` failed collection and was corrected.
- `uvx --offline ruff check --no-cache src tests`: **passed**. The venv itself does not install Ruff. AST comparison against the supplied merge base confirmed that the six formatting-only application files and the reformatted migration methods retain their prior semantics.
- Explicit Node 24 TypeScript `tsc -b --noEmit`: **passed**. Oxlint under the same Node version exited **0** with existing React/ref/effect/dependency warnings. The first generic Node invocation selected Node 18 and failed before checking TypeScript; it was rerun with the required runtime.
- Independent synthetic adapter probes reproduced grant reuse, concurrent duplicate SMTP calls, cross-account evidence collision, and loss of refresh material. Independent Chromium interaction against the compiled FastAPI-served assets reproduced the fresh-session capability failure and incorrect IMAP default, and checked mobile background scroll behavior. No real credentials, provider, or recipient was contacted.
- Existing provider/shell desktop, tablet, narrow-drawer, and packaged screenshots under `.orchestra/evidence/031/` were actually viewed. Those are supplied artifact observations, not an independently launched macOS package qualification.

Coverage does not currently exercise the negative authorization/count, concurrent submission, cross-account key, fresh-session direct route, or omitted-refresh-token scenarios above. No full E2E suite, axe sweep, production asset rebuild, independent package launch, signing/notarization, or external-provider qualification was performed by this reviewer. No memory profiler was run; successful non-browser suite completion and lifespan teardown support the fixture remediation, without establishing a measured memory bound.

The inherited controlled-send foundation also still accepts raw message content and an optional revision identifier without invoking the canonical Message Studio renderer or recipient eligibility/suppression authority. This is unchanged relative to the supplied merge base, so it is recorded as a governing spec 0009 AC-9 qualification gap rather than attributed as a newly introduced regression. The new grant work must not be cited as evidence that those policy/revision requirements are satisfied. No schema version is changed by this diff; migration formatting is AST-equivalent, and the independent Python suite covers the existing persistence/backup behavior without claiming real-data recovery qualification.
