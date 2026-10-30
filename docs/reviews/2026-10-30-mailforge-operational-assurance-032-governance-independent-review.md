# Operational assurance 032 governance independent review

Date: 2026-10-30

Reviewer: **gpt-6.1-sol**, distinct from author **GPT-5/Codex**.
Author identity is supplied explicitly by the user; reviewer identity is supplied
by this session's model configuration. This is a separate review execution and
does not claim cryptographic authentication of historical authorship.

Branch: `feat/mailforge-operational-assurance-032`.
Reviewed base HEAD: `8233b0a25a9ab43e9705fb1fda1a679461696276`.
Final reviewed source: that HEAD plus the uncommitted G1 correction in
`src/mailforge/interfaces/cli.py` and regression in `tests/test_web.py`.
There is no committed `8233b0a..HEAD` delta; HEAD remains `8233b0a`.
The original review began with a clean worktree; the re-review began with those
two modified files and this untracked review artifact. The reviewer modified
no application source or tests.

**Final verdict: APPROVE WITH NITS.**

G1 is resolved in the reviewed working tree. No critical or major finding
remains in the inspected scope. The four r8 corrections remain intact. The
remaining nits concern evidence/documentation reconciliation and external
qualification provenance, not a required production qualification for the local
synthetic milestone. This final verdict supersedes the initial CHANGES REQUIRED
verdict below and applies to the recorded working-tree content, not the
uncorrected HEAD alone.

## Resolved major G1: a single custom-database server contended with itself

The following records the original finding at `8233b0a`. It is closed by the
re-review evidence in the next section.

Affected source: `src/mailforge/interfaces/cli.py:1112-1115`, interacting with
`src/mailforge/interfaces/process_lock.py:42-57`.

`serve` first acquires `DatabaseProcessLock(data.database)`. If the selected
database differs from the data directory's fixed `mailforge.sqlite3`, it then
acquires a second `DatabaseProcessLock(database)`. Both instances now open and
exclusively lock the same application-scoped directory, independently of their
database argument. The second acquisition fails while the first remains held.

For example, a sole invocation of `mailforge serve --database
<directory>/custom.sqlite3` exits with “another Mailforge process already owns
database ...” before `create_app` or `uvicorn.run`. The same occurs when an
explicit `--data-dir` accompanies a different selected database. This is an
availability regression in the supported CLI options at `cli.py:893-895`, not
the intentional rejection of a second simultaneous database runtime. The
two-lock structure predates the switch to a global lock and was not reconciled
with its new identity.

Evidence: an independent disposable probe invoked the actual CLI `main`, used
the real `DatabaseProcessLock`, and replaced only the default coordination path
with an isolated temporary path. Server/app creation was mocked to avoid
starting a listener. `mailforge.sqlite3` reached both mocked boundaries;
`custom.sqlite3` failed before either boundary with the ownership error. Thus
this is observed behavior, not an inferred filesystem portability concern.

Requested correction: acquire the global runtime authority once per server
lifetime, including custom database selections; preserve contention with other
processes and backup/restore commands. Add a CLI regression exercising the
custom-database startup path, with and without an explicit data directory.
The current lock-primitive tests and campaign-restore contention test do not
exercise this composition of two global lock instances.

## Final G1 correction and independent re-review

The complete working-tree delta from `8233b0a` consists of two deleted lines
in `cli.py` (the conditional second acquisition), plus a `patch` import and
`test_serve_custom_database_does_not_reacquire_global_lock` in `test_web.py`.
No other tracked paths differ. The single remaining global acquisition still
surrounds initialization, app creation and the blocking `uvicorn.run` lifetime
(`cli.py:1112-1120`); ownership identity remains independent of the selected
database. Removing the second acquisition therefore fixes self-contention
without relaxing exclusion of other runtimes or backup/restore operations.

Exact reviewed file identities (`git hash-object`):

| File | Working-tree Git blob ID |
| --- | --- |
| `src/mailforge/interfaces/cli.py` | `7294684a910c4e78bcc9d860b3aef1c47418ea6b` |
| `tests/test_web.py` | `522822e06395470a780d61b4debb205fa1bab849` |

SHA-256 of `git diff 8233b0a -- src tests`:
`bfe2c42377b58b34655b52011774ac034e3ac01c6a959b7d0b483a6f413db7b6`.

Independently executed on Darwin arm64 / Python 3.15.2:

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_web.py tests/test_operational_assurance_032.py -k 'database_process_lock or campaign_restore_cli or serve_custom_database or active_job or unknown_required_evidence or web_health_uses'
```

Result: **12 passed, 20 deselected**, exit 0, one existing Starlette/httpx
deprecation warning. This includes the new CLI startup regression, ownership
alias/lifecycle/contention tests, campaign restore contention, and heartbeat
and unknown-health regressions.

An additional disposable CLI probe independently exercised custom-database
startup both with and without explicit `--data-dir`. It used the real lock,
redirected only its default path into a resolved temporary directory, and
mocked app/listener creation. Inside the mocked server call, a contender for
a different database was rejected; after `main` returned, reacquisition
succeeded. Both configurations reached app creation and the server call with
the selected custom database. These observations verify startup composition,
ownership during the server lifetime and release after return; they do not
claim a new live-server or packaged-runtime qualification.

`git diff --check` passed. The user's reported Ruff pass is executor evidence;
Ruff was not independently rerun. No broader suite/build was repeated for
this two-line executable correction. The file identities above were checked
again after updating this artifact.

## Revision reconciliation and review eligibility

The complete `git diff 8a8cc41..8233b0a` adds exactly one file:
`docs/reviews/2026-10-30-mailforge-operational-assurance-032-post-fix-independent-review.md`
(122 added lines). It changes no executable source, tests, configuration,
security implementation, scripts, or runtime behavior. Excluding that document,
`git diff --exit-code 8a8cc41..8233b0a -- . <document-exclusion>` exited 0.
The `src`, `tests`, and `web` tree IDs also match pairwise across the revisions:

| Tree | Identical Git object at both revisions |
| --- | --- |
| `src` | `dfdb75c28a91647ac3a6434ce009fcf6c9ff6345` |
| `tests` | `b191d713764982d4c304514c71e2fa4cd864c6fd` |
| `web` | `726d7a5ce22ce1d0175eab5d4969f37ffd6f41b8` |

The local check contract requires a different reviewer model from the author
(`.agents/skills/check/SKILL.md:26` and `modes/review.md:3-9`), and a meaningfully
separate review execution/context (`SKILL.md:32-39`). The supplied author/model
declarations and this independent source inspection satisfy that model
distinction for the present review. The earlier artifact explicitly did not
claim it and is not retroactively upgraded. The inspected rules do not require
an independent human specifically. GitHub approval requirements and branch
protection were not queried, and this artifact grants no merge authorization.

## Spec and prior-finding assessment

Assessment is against `docs/specs/0021-operational-assurance.md`, the r8 review,
and the runtime-ownership contract. PASS below refers to the named source
property, not production qualification or complete milestone acceptance.

| Criterion | Status | Independently inspected evidence |
| --- | --- | --- |
| r8 cross-directory hard-link ownership bypass | PASS | `process_lock.py:15-57` uses a common application directory independent of database aliases; cross-directory symlink/hard-link regression passed. |
| r8 creation/replacement identity race | PASS | Directory authority remains held across database creation/replacement and marker replacement; lifecycle and cross-process contention tests passed. The separate G1 double-acquisition regression is now resolved. |
| r8 generic activity or supervisor state implies job liveness | PASS | `operational_assurance.py:245-283` classifies active work without authoritative heartbeat/progress as unknown regardless of supervisor status; service and web-path regressions passed. Stale `updated_at` is explicitly unverified activity. |
| r8 unknown evidence aggregates to healthy | PASS | `operational_assurance.py:392-400` orders unavailable, action-required, degraded, unknown, healthy; unknown-storage and real web-path tests passed. |
| Complete supported runtime startup | PASS for reviewed CLI composition | G1 is resolved; the new regression and independent probes reach app/server creation with custom databases, including explicit data-directory selection, while preserving lock lifetime. |
| Backup/restore ownership | PASS by source inspection and focused restore contention | CLI application backup/restore and campaign backup/restore surround their operation with the same lock; web operations run within the server lifetime. Campaign restore's process-level regression passed. |
| Incident recovery evidence | PASS for inspected paths | `operational_assurance.py:455-530` preserves unknown checks and acknowledged unchanged conditions; healthy observations close incidents, and omitted conditional checks represent recovery on the normal complete observation path. Source-failure recovery and unknown-storage preservation regressions passed. |
| Ambiguous external outcomes | PASS by source inspection; crash qualification assessed from prior evidence | `WebRuntime._recover_running_campaigns`, `CampaignStore.recover_inflight`, and provider submission failure paths retain ambiguity and pause campaigns; operational controls delegate to existing domain authorities. No new external replay mechanism was found. |
| Privacy, persistence, domain authority | PASS for inspected projections/boundaries | Assurance uses existing SQLite events, fixed error classifications, bounded job/export projections, and existing session/CSRF middleware; operational action routes delegate to domain services. API/redaction/durability regressions passed. |
| Keychain and unsigned-package claims | PASS for source/documentation boundary; native behavior NOT VERIFIED | Native credential resolution uses Security.framework and opaque references; the injected runner is test-only. Qualification docs explicitly limit unsigned/unnotarized evidence. Injected-resolver regression passed; native Keychain was not exercised. |
| Local milestone review gate | PASS WITH NITS | No critical/major finding remains. Spec still says In Progress; completion metadata and final qualification provenance remain for the executor/acceptance process to reconcile. This review does not itself ratify business acceptance. |

## Source independently inspected

The full documentation delta was read. Branch-level inspection focused on the
032 executable changes and relevant existing consumers: the complete process
lock and assurance service; CLI serve/backup/restore composition; web health,
supervisor, recovery, operational controls, session/CSRF and backup/restore
boundaries; local archive publication, validation and staged restore; native
and dispatched credential resolution; campaign store backup/restore and
in-flight recovery; provider ambiguity handling; and the relevant regression
tests. Maintenance health-label/evidence source changes and DESIGN.md were
assessed without a new rendered UI qualification.

The locally available merge base with `origin/main` is
`74d264b79901abda8b43ae613e5d5232c181e516`; the cumulative 032 path inventory
contains 31 changed files, including historical reviews and generated assets.
This is a focused governance/correctness review, not a claim that every
historical branch file or generated JavaScript byte was audited.

## Original checks independently executed before the G1 correction

Environment: Darwin arm64, repository `.venv`, Python 3.15.2, source at the
reviewed HEAD, disposable synthetic data only.

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operational_assurance_032.py tests/test_local_application.py tests/test_web.py -k 'operational or database_process_lock or campaign_restore_cli or runtime_setup_health or web_runtime_reconciles'
```

Result: **20 passed, 18 deselected**, one existing Starlette/httpx deprecation
warning, exit 0. The filter selects assurance/API, ownership, restore contention
and runtime recovery checks; it does not imply all local backup/restore tests
were rerun. No full suite, frontend build, browser suite, package build, or Ruff
run was repeated for this documentation-only delta.

Reproducer used for G1 (executed with `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -`):

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from mailforge.interfaces.cli import main

with TemporaryDirectory(prefix="mailforge-governance-") as temporary:
    root = Path(temporary).resolve()
    for filename in ("mailforge.sqlite3", "custom.sqlite3"):
        with (
            patch("mailforge.interfaces.process_lock.default_runtime_lock_path",
                  return_value=root / "coordination" / "runtime.lock"),
            patch("uvicorn.run") as run,
            patch("mailforge.interfaces.web.create_app") as app,
        ):
            try:
                main(["serve", "--database", str(root / "data" / filename)])
                outcome = "returned"
            except SystemExit as error:
                outcome = str(error).replace(str(root), "<temporary>")
            print(filename, outcome, run.called, app.called)
```

Observed: canonical filename returned with both calls true; custom filename
reported another owner with both calls false. The first probe used an unresolved
macOS temporary path and was rejected by symlink protection; resolving that
temporary path yielded the conclusive results above. No user data was touched.
`git diff --check 74d264b..8233b0a` also passed.

## Prior review and executor evidence assessed

The prior post-fix review reports 49 focused checks, 280 non-E2E checks and 8
browser checks at `fea478a`, frontend build/lint and secret checks, then 8
restore/ownership checks at `8a8cc41`. These are historical review evidence,
not independent executions in this review. Its broad no-material-blocker claim
was contradicted by G1 at the uncorrected revision despite the valid
documentation-only revision equivalence; the working-tree correction now closes G1.

The qualification manifest identifies `327a98b` as its source revision and
reports synthetic crash recovery, atomic backup interruption/concurrency,
staged restore, viewport observations, package smoke and native/packaged
Keychain exercises. That older package/manifest is not proof of final-source
package qualification. The performance record contains small local samples
(including empty-state queries and a 5,000-row workload), without retained raw
measurement output or a complete reproduction harness; its values were not
rerun and establish no production limits. Preserve these provenance limitations
when reconciling documentation after the source correction.

## Unverified qualifications and handoff

No real SMTP/IMAP access, production credentials, verified provider quota,
production-scale performance, production recovery objectives, signed/notarized
release, signed Keychain identity continuity, Windows package, or native
Keychain access-denial behavior was qualified here. These are explicit external
qualification boundaries and do not block approval of the local synthetic scope.
No new browser screenshots or packaged-runtime observations are claimed.

Remaining documentation/evidence nits: reconcile the spec's In Progress status
through the authorized acceptance process, and keep the older qualification
manifest/package revision distinct from final-source evidence. Production and
external qualifications remain explicitly outstanding. No application source,
tests, specs, atlas, local truth, commits, remote state, or PR state were changed
by the reviewer. Only this review artifact was updated. No `docs/scope/`
directory exists, so no matching review checkbox was updated.
