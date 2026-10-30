# Operational assurance 032 final review, revision 2

**Verdict: BLOCKED.** Reviewed committed HEAD `eee4fe7754874b40a643ef2d653942b07823546c` against `74d264b79901abda8b43ae613e5d5232c181e516`. The working tree was clean at the start and during the checks; this review artifact is the only subsequent change. This separate reviewer execution context did not implement the changes, but the author model identity and cross-model independence cannot be authenticated. The preceding review is historical evidence for `a8837fc`, not approval of this revision.

## Remaining findings

### Major: ambiguous provider outcomes are counted as refusals

`src/mailforge/application/operational_assurance.py:245-264` counts every controlled test status other than `provider_accepted` or `pending` as `refused`. `src/mailforge/application/provider_operations.py:631-640` explicitly records exceptions as `ambiguous`, including uncertain external effects. An ambiguous submission therefore appears as a refusal without evidence of refusal. The counter covers controlled tests rather than all provider usage; its scope should be explicit. This was not changed by `eee4fe7`.

### Major: liveness still performs a full SQLite integrity check

`src/mailforge/interfaces/web.py:903-917,1147-1148` now avoids operational assurance scans on `/healthz`, which removes the backup-validation and job-scan regression from that endpoint. However, `liveness()` still calls `application_status()`, which instantiates `CampaignStore` and invokes `integrity_check()` on each request (`src/mailforge/application/local_application.py:196-205`). This is not a bounded constant-cost liveness check and runs on the async event loop. Representative database size, contention, and the launcher timeout were not measured in this review; the precise operational impact is unverified. Detailed health and status requests still synchronously scan jobs and validate backups.

### Qualification remains incomplete

Native Keychain ABI, access denial and unlock behavior, restart continuity, and packaged identity were not exercised. Crash interruption, concurrent runtime ownership, fencing, checkpoint and ambiguous-effect recovery, backup interruption and staged restore were not exercised. The rendered Maintenance screen, controls, responsive behavior and accessibility were not inspected in this re-review. These are missing acceptance evidence, not assertions that those existing operations necessarily fail. Incident projection and unknown/error handling concerns from the previous review also remain in the implementation.

## Fix assessment and validation

- Error text now maps to fixed classifications rather than returning arbitrary source text (`operational_assurance.py:63-82`); the previously demonstrated arbitrary body and path leakage through job errors is addressed by inspection. Broader export fields and contaminated historical records were not independently exercised here.
- Inbound runs now project `started_at` and `COALESCE(finished_at, started_at)` (`operational_assurance.py:107-113`), matching the schema. The query still suppresses source exceptions; no populated inbound journey was run.
- Workbench controls now navigate to pathname routes, matching `viewFromPath` (`web/src/App.tsx:63-69,272`). This is code inspection, not rendered browser evidence.
- Native Keychain paths now release item references using `CFRelease` (`credentials.py:318-331,359-375,405-420,437-450`). Actual Security.framework execution and exception paths remain unverified.
- Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operational_assurance_032.py tests/test_web.py`: **20 passed**, one Starlette/httpx deprecation warning, 3.46 seconds. No application code or tests were edited.

The observed fixes improve the prior revision but do not close quota truthfulness, bounded liveness, or the required runtime qualification. This review provides no merge or business approval.
