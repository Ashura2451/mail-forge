# Visual workflow automation performance note

This note records the local synthetic target and the initial focused evidence
for task 016. It is not a hosted-scale or provider-performance claim.

## Method

- Runtime: CPython 3.15.2 environment, SQLite WAL, local disposable database.
- Workload: bounded graph validation and synthetic enrollment/run advancement;
  no network, provider, mailbox, or recipient data.
- Correctness gate: `tests/test_automation.py` must pass before any timing is
  considered meaningful.

## Target and limits

The first local slice bounds graphs at 100 nodes, 50 path depth, 20 condition
nodes, 64 KiB graph JSON, 8 KiB node configuration, and seven-day relative
waits. API lists cap at 200 workflow/run rows per page. The worker advances at
most 50 steps per request and uses two in-process automation workers in the web
runtime.

## Evidence

The focused automation suite exercises graph validation, event/contact
deduplication, immutable revision pinning, a list side effect, and a durable
wait across a synthetic clock advance. Full benchmark timing and restart
throughput qualification remain part of the operational acceptance run; no
claim is made here for multi-user or always-on scheduler capacity.

One disposable SQLite run on the same local environment measured:

- Workflow creation: 0.002641 s.
- 1,000 synthetic contact inserts: 0.529419 s.
- 1,000 enrollment operations: 1.470866 s (679.87/s).
- 1,000 default trigger-to-End advances: 2.686135 s (372.28/s).

These are single-process wall-clock observations, not service-level
objectives. They exclude browser and process-restart overhead and should not
be generalized to hosted, multi-user, or provider-connected workloads.
