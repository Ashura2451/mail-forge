# Operational assurance 032 final review

**Verdict: BLOCKED.** Reviewed `74d264b79901abda8b43ae613e5d5232c181e516..a8837fc9d73037c3b4114d1a79e91be27f04b399`; working tree was clean at start. This is a separate reviewer execution context that did not implement the changes. The implementing model's identity and a different reviewer model cannot be independently established here, so cross-model independence is not claimed. This review does not grant business or merge approval.

## Findings

### Blocker: diagnostic content is not safely redacted

`src/mailforge/application/operational_assurance.py:63-70,103,438-451` exports arbitrary stored error strings after a keyword and path-prefix filter. A direct synthetic probe returned `message body: private text; /opt/customer/records.csv; abc123` unchanged. Real errors may include message text, recipients, unlabeled secrets, and paths outside the enumerated roots. These values flow to both jobs and the shareable export. The test at `tests/test_operational_assurance_032.py:95` covers a Bearer marker and one `/Volumes` path only. Use an allowlisted safe classification rather than relying on identifying all possible sensitive strings; exercise contaminated historical records.

### Major: inbound runs are silently omitted from job health and detail

`src/mailforge/application/operational_assurance.py:82,89-95` queries `created_at` and `updated_at` for every table and suppresses all query exceptions. `inbound_runs` has `started_at` and `finished_at` instead (`src/mailforge/infrastructure/campaign_store.py:284`). A disposable schema probe confirmed those two missing columns. Therefore inbound runs never appear in job details, active/stale counts, or job-level failures; separate status aggregates do not replace those records. Expose observation failures and project each source's real schema.

### Major: liveness and routine status perform unbounded synchronous assurance work

`src/mailforge/interfaces/web.py:912,1149-1151,1198-1200` calls assurance health from public liveness and again for the health API. Health loads all source job rows (`operational_assurance.py:181`) and validates backup archives (`:119-131`) on the async request thread. This continues the previous review's launcher latency and event-loop contention regression; costs grow with retained data, with duplicate work on other operations. Keep liveness bounded and separate expensive verification from request handling. Representative latency and launcher ownership were not measured in this review.

### Major: workbench links route to Dashboard

`web/src/App.tsx:272` opens `/?view=<workbench>`, but routing at `:63-69` reads only `window.location.pathname`. Consequently every new Open workbench control lands on Dashboard instead of the source workbench. This conclusion is from code inspection; this review did not execute a browser journey.

### Major: provider usage labels outcomes as refusals without evidence

`src/mailforge/application/operational_assurance.py:217-233` calls every test submission except `provider_accepted` and `pending` a refusal. `provider_operations.py:631-640` records exception outcomes as `ambiguous`, including uncertain external effects, so those are incorrectly counted as refused. The counter also counts only controlled test submissions, not all outbound activity; it is not an account-wide usage/quota measurement. Preserve distinct outcome states and make the observation's scope explicit.

### Major: native Keychain and recovery qualification remain incomplete

The Security.framework production path avoids secret-bearing child arguments, but `credentials.py:297-316,342-354,370-424` returns retained Keychain item references without `CFRelease`; repeated resolve, store/rotate, or revoke calls can leak native objects. `backend_available()` at `:253-254` only checks OS identity, not unlocked/access-authorized storage. The injected-runner test does not exercise the native ABI, access denial, restart, or bundle identity. Existing CLI/default consumers still instantiate `EnvironmentCredentialResolver`; actual supported consumer coverage requires qualification. No native Keychain operation was performed in this review.

The branch adds no process-level crash, fencing, concurrent ownership, interrupted backup, restore/restart or ambiguous-effect recovery evidence. Existing domain controls are delegated at `web.py:1206-1224`, but only a subset of subsystems/actions is exposed and no recovery journey was exercised here. These acceptance dimensions remain blocked on actual runtime evidence; absence of evidence is not evidence that existing recovery is broken.

### Major: incident projection and health can misstate evidence

`operational_assurance.py:284-293` treats unknown checks as healthy unless another check is worse. `_all_counts` and job queries swallow errors, and `_persist_incidents` resolves only checks present in the latest result (`:347-391`); a failed source query can therefore disappear without an unavailable indication while an earlier incident remains stale. Acknowledgment accepts any syntactically valid ID (`:414-434`). The UI renders aggregate findings rather than source operation incidents, and the 500-event projection can lose older incident state. These are incomplete and potentially misleading operational evidence, not durable occurrence history per source operation.

## Evidence and acceptance disposition

| Criterion | State | Evidence / limit |
| --- | --- | --- |
| Authority boundaries | NOT VERIFIED | Code inspection shows job actions delegate to existing domain services; authorization and all action transitions were not independently exercised. |
| Crash and recovery | NOT VERIFIED | No real process interruption, concurrent runtime, checkpoint, uncertain effect, restart or recovery probe run. |
| Keychain | BLOCKED | Native path inspected; ABI, leak behavior, access denial, restart, and packaged runtime not exercised. |
| Diagnostic redaction | FAIL | Synthetic sanitizer probe above; arbitrary source text survives. |
| Quota truthfulness | FAIL | SQL classifies ambiguous outcomes as refused and covers only test submissions. |
| Backup/restore | NOT VERIFIED | Existing staged restore code inspected indirectly; no restore, corruption, interruption or restart exercise. |
| Operator controls | BLOCKED | Delegation inspected; incomplete coverage and broken workbench routing; no interactive control journey run. |
| Rendered Maintenance UI | NOT VERIFIED | Source inspected only; no browser, responsive, accessibility or visual validation run. |
| Regression risk | BLOCKED | Liveness synchronous work and omitted inbound records identified; no representative scale/performance or full regression run. |

Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operational_assurance_032.py tests/test_web.py`: **20 passed**, one Starlette/httpx deprecation warning. Ran a disposable `DataDirectory`/SQLite PRAGMA probe of seven job sources and the synthetic sanitizer example; inbound timestamp mismatch was the only missing projection among those seven. These tests do not replace real application, browser, native Keychain or recovery qualification. No application code or tests were edited. The preceding review at `4527ab5` provides separate historical evidence, not proof for this revised HEAD.
