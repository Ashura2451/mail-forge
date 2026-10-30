# Review, feat/mailforge-operational-assurance-032, 2026-10-30

**Reviewed by**: gpt-6.1-terra (author on unknown; distinct model identity cannot be independently authenticated)
**Scope**: 82 files, branch vs base `e584c6b73cdb9ff1e55c631a4c294f75c0f273ee`
**Verdict**: Blocked

## Summary

The branch extends existing authorities with operational health, incidents, credential resolution, and Maintenance controls. The final ownership fix still permits multiple owners of the same database, including on initial creation; worker liveness and overall health also report healthy without sufficient evidence. The reviewed tip is `15b8df208be15cb25ab61f1c61153b97490e7641`, with source revision `327a98b` and a clean working tree before this findings file. The branch-wide diff is substantial: the review focused on 032 and the prior priority findings, and did not read every changed file in full or independently qualify all earlier milestones.

## Blockers

### 🔴 Hard links in different directories bypass ownership, `src/mailforge/interfaces/process_lock.py:27`

**Problem**: The inode-derived lock filename is placed in the database path's parent. Hard links to one inode in different directories therefore use different lock files. The existing hard-link test places both names in the same directory, so it misses this case. A disposable probe acquired both locks simultaneously.

**Why it matters**: Two servers with different data directories can own the same database and run competing schedulers and recovery against consequential operations. This leaves the previous cumulative review's ownership finding open; the qualification manifest's general hard-link claim is broader than the tested case.

**Suggested fix**: Use a stable shared lock authority for all aliases, or safely reject multiply linked databases, including during ownership acquisition. Exercise cross-directory aliases and live competing servers.

### 🔴 Database creation changes the lock identity while it is held, `src/mailforge/interfaces/process_lock.py:20`

**Problem**: A nonexistent database uses a pathname-based lock; once it is created the same path uses an inode-based lock. A disposable probe held the first lock, created the file, and successfully acquired the second simultaneously. Server startup acquires the lock before initialization creates the database (`src/mailforge/interfaces/cli.py:1112`).

**Why it matters**: A second startup during the first server's initialization, or after it has started on a fresh database, can bypass the owner lock even with the same pathname. The shared backup cleanup lock can likewise change identity, permitting cleanup during a backup.

**Suggested fix**: Establish a lock identity that stays stable across creation and for the complete server/backup lifetime, with atomic coordination for aliases. Verify fresh-directory startup and backup races using separate processes.

## Major

### 🟠 Normal API calls report active work healthy without a heartbeat, `src/mailforge/application/operational_assurance.py:264`

**Problem**: The unknown branch runs only when `worker_status` is falsy. Actual web health calls pass a nonempty dictionary containing scheduler thread status and counts, not per-job heartbeat or progress evidence (`src/mailforge/interfaces/web.py:893`, `:1218`). With an active recent job and both scheduler threads reported running, a disposable service probe returned workers `healthy` and the contradictory evidence “no active durable work requiring a liveness claim”. The added test calls `health()` without the dictionary and misses the normal path.

**Why it matters**: A running scheduler thread does not establish that an individual campaign, data job, or other durable operation is progressing. Operators can be falsely reassured about stuck work.

**Suggested fix**: Classify active work as unknown until authoritative, source-specific heartbeat/progress evidence exists; assess supervisor status separately. Exercise the actual web health path with active jobs and live scheduler status.

### 🟠 Unknown checks are omitted from the overall health state, `src/mailforge/application/operational_assurance.py:392`

**Problem**: Overall state accounts for unavailable, action-required and degraded, then defaults to healthy even when a check is unknown. With a current backup and an active job without heartbeat, a disposable service probe returned worker `unknown` but overall `healthy`. Unknown storage or Keychain evidence has the same aggregation problem when other checks are healthy.

**Why it matters**: The principal health indicator can assert a healthy system while a required source cannot be evaluated, contradicting the spec's truthful evidence requirement and the incident logic's preservation of unknown conditions.

**Suggested fix**: Define and propagate an explicit unknown or appropriately degraded overall state, preserving more severe states, and exercise unknown-only combinations at the API and UI boundaries.

## Strengths

- Job error text is mapped to fixed classifications, rather than copying stored arbitrary errors into diagnostic output.
- Source observation failures are surfaced, and the focused suite covers recovery of source incidents and preservation of acknowledged or unknown conditions.
- Cheap liveness is separated from the detailed assurance health path, and provider ambiguous outcomes are counted separately from explicit refusals.

## Test coverage and evidence

Independently ran `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_local_application.py tests/test_operational_assurance_032.py tests/test_web.py`: **31 passed**, one Starlette/httpx deprecation warning. `git diff --check e584c6b73cdb9ff1e55c631a4c294f75c0f273ee` passed. Independently ran disposable lock-primitive and service-health probes described above, without editing application code or tests. The supplied test signal is `none-yet`: no test-preferences configuration or explicit no-runner convention was identified; the existing focused tests are useful evidence but are not a full safety net.

Source inspected includes the operational assurance service, ownership lock and CLI call sites, web supervisor/health call sites, focused tests, relevant specs and design contract, earlier review rounds, and qualification/performance records. The complete branch diff was requested, but tool output was truncated; not all 82 changed files were read in full. Thus this is not a complete branch-wide code or UI design audit.

Priorities from the historical reviews: diagnostic error classification, inbound timestamp projection, workbench pathname routing, separate ambiguous/refused counts, cheap liveness, source failure/resolution, acknowledgment and unknown-incident handling, and two-lock CLI structure have source or focused-test evidence of improvement; this review does not independently establish every original acceptance condition. Ownership aliasing remains failed as above, and the later heartbeat follow-up fails on the real web call path. The initial absent-database race is a further ownership failure. Prior review findings and assertions should not be treated as globally closed solely from these targeted checks.

The committed qualification and performance documents report executor-performed synthetic crash/recovery, backup/restore, native and packaged Keychain, UI viewport, package and performance exercises. Those are executor evidence, not independent reruns here; their manifest does not supply complete replayable commands or retained visual artifacts. This review did not exercise live competing servers, process termination/recovery, restore, native Keychain, the packaged runtime, browser interactions, responsive appearance, accessibility, real providers, or production scale. Those dimensions remain not independently verified, and cross-model independence is unverified because the author model is unknown. Spec 0021 remains In Progress; this review is not release or business approval.
