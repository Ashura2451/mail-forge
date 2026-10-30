# Review, feat/mailforge-operational-assurance-032, 2026-10-30

**Reviewed by**: gpt-6.1-sol, separate reviewer execution context. Author reported by parent context as Codex/GPT-5; author identity is not independently authenticated.
**Scope**: 10 tracked paths, `74d264b79901abda8b43ae613e5d5232c181e516..4527ab59c03e881c504bdf0d9c80c86c7728352c`.
**Verdict**: Blocked

## Summary

The change adds a small observation service, several local APIs, a Maintenance counter panel, a standalone Keychain adapter, and focused corrections for the two 031 UI findings. The narrow service boundary and reuse of existing operational events are appropriate starting points. However, the revision exposes secrets through two new paths, can report healthy while work or schedulers have failed, and does not implement most of the operator journeys and recovery assurances required by the 032 execution packet. This revision does not satisfy the merge or milestone acceptance gates.

## Blockers

### 🔴 Keychain writes expose secret values through argv and exception chaining, `src/mailforge/infrastructure/credentials.py:248`

**Problem**: `store()` supplies the complete secret as the final command-line argument to `/usr/bin/security`. That value is accessible through the child process argument vector. `_run()` then raises `CredentialError` with the original `CalledProcessError` as its cause; that exception formats its original command, including the secret. A synthetic runner failure reproduced the secret in `traceback.format_exc()`.

**Why it matters**: The adapter is intended to protect credentials, but secret writes/rotations create a new process-observation and diagnostic-log leak. Capturing stdout/stderr and changing the outer exception message does not remove the secret from argv or the chained exception.

**Suggested fix**: Use native Keychain Services through a reviewed OS adapter with no secret-bearing arguments or environment. Ensure failures cannot retain secret-bearing commands, output, or causes. Add process-boundary and exception/redaction tests, plus real disposable Keychain qualification in the supported bundle.

### 🔴 Diagnostic export exposes unredacted source error text and private paths, `src/mailforge/application/operational_assurance.py:76`

**Problem**: `_job_rows()` copies the first 300 characters of domain `error_message`, `failure_reason`, or `last_error` directly into the API and diagnostic export. There is no safe-field projection or redaction. A disposable job with synthetic `password=SYNTHETIC_EXPORT_SECRET at /private/synthetic/customer-path` exported both the marker and full path. The only new export test checks an empty database for the word `password`.

**Why it matters**: Support exports are specifically intended to be shareable without credentials, private message data, or unrestricted paths. Stored errors can contain exactly those values, including errors from before this revision. Truncation is not redaction. The export is also available by GET without the required content preview/confirmation workflow.

**Suggested fix**: Export allowlisted classifications and safe source identities; apply an established sanitizer to any retained diagnostic text. Show a safe preview and obtain explicit operator confirmation for the export. Test contaminated historical errors, credentials, bodies, paths, and identifiers across every included subsystem.

## Major

### 🟠 Healthy state ignores failed schedulers, omitted subsystems, and failures outside a sample, `src/mailforge/application/operational_assurance.py:50`

**Problem**: Health derives counts from only the latest 100 rows in each of five tables. Campaigns, SMTP/provider tests, inbound/mailbox work, and scheduled-overdue work are absent. `worker_status` is copied into output but does not influence any check or the overall state. Query failures inside `_job_rows()` are silently skipped. Unknown storage state also falls through to overall `healthy` when other checks pass. Thresholds of five minutes, seven days, and five percent are hardcoded without configuration or published semantics.

**Why it matters**: A deterministic synthetic probe inserted one failed List Tools job followed by 100 newer completed jobs and supplied both schedulers as `stopped`; with a current backup observation, health reported `healthy` and zero failures. That directly contradicts the central operational-assurance outcome. An unchanged `updated_at` is also not a worker heartbeat, so a healthy long-running operation can be falsely marked stale.

**Suggested fix**: Aggregate complete authoritative counts in SQL independently of detail pagination. Include the required subsystem states, real supervisor progress/heartbeat evidence, overdue scheduling and provider suspension/quota observations. Distinguish unavailable/unknown evidence instead of swallowing it. Make thresholds documented and configurable, with tests for pagination boundaries, stopped/stalled workers, paused work, omitted sources and missing evidence.

### 🟠 Incidents have no durable occurrence history and acknowledgment suppresses future recurrence, `src/mailforge/application/operational_assurance.py:198`

**Problem**: Open incidents are regenerated from current aggregate checks, with an ID based only on subsystem and generic state. Their condition, timestamp, evidence, affected operation and resolution are never persisted. Only acknowledgment rows are durable. Any acknowledgment for an ID suppresses that condition indefinitely while it remains within the most recent 200 acknowledgment rows; a new incident with the same state is hidden too. Repeated acknowledgments and health checks append unlimited events. Arbitrary nonexistent incident IDs can be acknowledged.

**Why it matters**: An operator cannot inspect a recovered failure after restart, distinguish two affected jobs, or discover a recurrence. Conversely, when an acknowledgment falls outside the latest-200 window, the same active condition reappears as unacknowledged. In a disposable probe, acknowledging current findings removed all of them even though the source problems remained active.

**Suggested fix**: Persist a bounded incident occurrence/projection with source operation, first/last observation, evidence, transition/disposition and acknowledgment history. Acknowledgment should not erase current health or future occurrences. Validate the source identity; make repeated observation/acknowledgment idempotent and define explicit retention without deleting user data silently.

### 🟠 Native credential storage is disconnected from actual account operations, `src/mailforge/infrastructure/credentials.py:192`

**Problem**: Repository search found no production instantiation or consumer of `KeychainCredentialResolver`. Provider credential readiness, SMTP/IMAP operations, Data Connections, Integration Hub and CLI paths still use `EnvironmentCredentialResolver`. There is no application store/rotate/revoke workflow or account association check. In addition, `stdout.strip()` changes valid credentials with leading/trailing whitespace.

**Why it matters**: The test proves only an injected runner mapping. A `keychain:` reference cannot be used through the existing application backend path after restart, and the required browser/package credential journey is unavailable. A reference name alone does not prove that it belongs to the selected account, and silently altering secret bytes can make authentication fail.

**Suggested fix**: Integrate OS-backed resolution through the canonical resolver boundary while preserving safe existing references, bind opaque item identity to the intended account/resource, preserve exact material, and implement the approved operator workflow. Exercise missing/locked/denied storage, cross-account references, rotation/revocation and packaged restart with disposable synthetic Keychain items. Record unsigned-app identity limitations.

### 🟠 Maintenance exposes counters without the required diagnosis and intervention workflow, `web/src/App.tsx:258`

**Problem**: The new panel displays four aggregate counters and two actions. It does not render check details, job records, incidents, source workbench links, subsystem filters, acknowledgments, blocked/ambiguous action inspection, pause/resume/cancel/retry/suspension controls, quota evidence, credential-store readiness, or diagnostic-export preview/confirmation. No UI consumer calls the new jobs/incidents endpoints. A fresh workspace displays `action_required` without showing the failed backup check or the reason for action.

**Why it matters**: The execution packet explicitly requires completion of those operations through the actual application. Chromium inspection confirmed the available new actions were only Run health check and Open diagnostic export. A green existing browser suite and a health-check toast cannot establish journeys A–E or criteria F–T. The current badge also exposes internal `action_required` text instead of an explanatory operator state.

**Suggested fix**: Complete the Maintenance journey with progressive disclosure of evidence and canonical-domain controls. Show the cause and next action for every attention state, and verify durable results through connected browser scenarios at desktop/tablet/mobile widths. Keep the 031 scroll ownership behavior.

### 🟠 Public liveness checks now perform full backup verification synchronously, `src/mailforge/interfaces/web.py:907`

**Problem**: Every `runtime.status()` call invokes operational health, which calls `list_backups()`. That function hashes/extracts every archive and runs SQLite integrity checks for each one. `/healthz` is unauthenticated and uses this path; `/operations/health` runs it twice through `runtime.status()` and `health()`. Diagnostic export runs health three times. The async routes perform the work synchronously on the server event loop.

**Why it matters**: Routine health polling now scales with the total backup corpus and can block the entire interface. The existing launcher uses a 0.4-second `/healthz` timeout and discards its runtime record when that probe fails, so a normal growing backup history can make a healthy application appear absent and interfere with start/stop ownership handling. This is also an expensive unauthenticated loopback endpoint.

**Suggested fix**: Keep liveness and launcher identity probes cheap. Persist/cache timestamped verification results, perform explicit bounded verification through the existing worker authority, and refresh only changed archives. Avoid duplicate health evaluations per request and move expensive work off the async event loop. Measure latency and contention with representative backup history.

### 🟠 Required supervision, recovery, quota and backup acceptance remain unimplemented/unverified, `docs/specs/0021-operational-assurance.md:43`

**Problem**: The 032 diff changes no launcher ownership, scheduler checkpoint, heartbeat, fencing, crash reconciliation, domain recovery/control, quota observation, resource measurement, or backup/restore behavior. It adds no process-level failure injection or tests for these requirements. The new three-test file exercises an empty-health path, a representative API request, and a mocked Keychain CLI. The capability atlas nevertheless claims operational incident evidence is durable/bounded and that Keychain resolution is implemented as product capability.

**Why it matters**: Packet sections 8–25 require evidence for existing behavior and implementation of confirmed gaps across all named subsystems, including real process interruption, simultaneous operations, stale ownership, uncertain effects, paused state, backup interruption/restored integrations, performance measurements, native Keychain and packaged browser journeys. No such acceptance evidence is present in this revision. Existing canonical worker behavior may satisfy parts, but must be inspected and qualified; it cannot be inferred from the unchanged suite.

**Suggested fix**: Reconcile the full packet against an actual behavior inventory and criterion-level matrix. Finish the authorized controls and qualification or return the exact incomplete scope for Planner disposition. Preserve the spec's In Progress status and mark atlas capability claims partial until the required evidence exists. Do not merge or declare milestone completion based on these counters/endpoints alone.

## Strengths

- The façade remains in the Python application layer, reuses the SQLite authority and existing event table, and does not create a competing queue or scheduler.
- The provider setup failure now has an explicit retry action, and controlled submission uses the existing readiness predicate for button availability while retaining the handler/server boundary.
- New mutation endpoints inherit the existing loopback, session and CSRF middleware.
- Detail queries have explicit limits and avoid selecting message/recipient content directly; this is a useful foundation once error fields are projected safely.

## Verification evidence and limitations

- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operational_assurance_032.py tests/test_web.py`: **19 passed**, one existing Starlette/httpx deprecation warning, 3.29 seconds.
- Independently executed inline disposable-database probes: raw diagnostic secret/path exposure reproduced; older failed work disappeared behind 100 newer completed jobs; both stopped schedulers coexisted with a `healthy` result; acknowledging active conditions removed them without resolving their source facts.
- Independently executed injected Keychain runner probes: a failed write's formatted traceback included its synthetic secret; leading/trailing secret whitespace was removed. No real Keychain or user credential was accessed.
- Independently launched FastAPI with committed compiled assets and a disposable database, visited `/maintenance` in Chromium, enumerated visible actions and captured/viewed `/tmp/mailforge-032-review-desktop.png` and `/tmp/mailforge-032-review-mobile.png`. The desktop panel showed `action_required`, zero active/failed/stale counts and four checks, without check causes or record/control surfaces. These captures establish the current panel, not full responsive or accessibility acceptance. The browser's fresh-runtime `Stopped` badge and database-filename status discrepancy predate this diff; they are observed limitations rather than new findings here.
- Parent was modifying `tests/e2e/test_browser_acceptance.py` during review. This review scopes implementation to committed `4527ab5`; the uncommitted addition only checks the new heading, attention badge, health-check toast and export button. It does not resolve the findings or complete operational journeys.
- No source code or tests were edited by this reviewer. No full regression rerun, package rebuild, real process interruption, real Keychain exercise, production dataset, provider contact, or live email occurred in this independent review. Earlier parent test/build claims are separate evidence and do not demonstrate the missing business operations.

## Acceptance disposition

The service-ownership direction is supported by inspection. The two 031 corrections have implementation evidence and still require their focused failure/retry browser qualification. Secret protection and accurate/explainable health fail the probes above. Durable incidents, operator intervention, native Keychain integration, quota/resource observations, process recovery, operational backup/restore, completed UI journeys, measured performance and packaged workflow qualification are incomplete or not verified. Independent review has occurred with a **Blocked** verdict; it provides no acceptance or merge approval.
